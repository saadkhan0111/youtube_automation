"""Test fixtures. Everything Google-related is faked: no real API calls, no quota used."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httplib2
import pytest
from googleapiclient.errors import HttpError

os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["ANTHROPIC_API_KEY"] = ""  # empty (not unset) so backend/.env cannot fill it in
os.environ["OPENAI_API_KEY"] = ""  # never call real OpenAI from tests

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import PermissionStatus, RightsReview, Video, VideoStatus, YouTubeAccount  # noqa: E402
from app.services import metadata, oauth, uploader, validation  # noqa: E402
from app.timeutil import utcnow  # noqa: E402

GOOD_PROBE = {
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "42.0"},
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920,
         "avg_frame_rate": "30/1", "r_frame_rate": "30/1", "duration": "42.0"},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
}


def http_error(status: int, reason: str | None = None, message: str = "error") -> HttpError:
    resp = httplib2.Response({"status": str(status)})
    resp.status = status
    body = {"error": {"code": status, "message": message, "errors": [{"reason": reason}] if reason else []}}
    return HttpError(resp, json.dumps(body).encode())


class FakeStatus:
    def __init__(self, p):
        self._p = p

    def progress(self):
        return self._p


class FakeInsertRequest:
    """Simulates googleapiclient's resumable HttpRequest.

    `script` is a list of steps consumed by next_chunk():
      "chunk"   -> progress, not done
      "done"    -> upload complete
      "net"     -> network error (ConnectionError)
      "500"     -> HTTP 503 backendError
      "quota"   -> 403 quotaExceeded
      "bad"     -> 400 invalid
      "expired" -> 404 (resumable session gone)
    """

    def __init__(self, yt, body):
        self.yt = yt
        self.body = body
        self.resumable_uri = None
        self.resumable_progress = 0
        self._in_error_state = False

    def next_chunk(self):
        if self.resumable_uri is None:
            self.yt.initiations += 1
        if self._in_error_state and self.resumable_uri:
            self.yt.status_queries += 1
        step = self.yt.script.pop(0) if self.yt.script else "done"
        if step == "net":
            if self.resumable_uri is None:
                self.resumable_uri = f"https://upload.example/session/{self.yt.initiations}"
            raise ConnectionError("simulated network drop")
        if self.resumable_uri is None:
            self.resumable_uri = f"https://upload.example/session/{self.yt.initiations}"
        self._in_error_state = False
        if step == "500":
            self._in_error_state = True
            raise http_error(503, "backendError")
        if step == "quota":
            raise http_error(403, "quotaExceeded", "quota exceeded")
        if step == "bad":
            raise http_error(400, "invalidTitle", "bad title")
        if step == "expired":
            raise http_error(404, "notFound", "session not found")
        if step == "chunk":
            self.resumable_progress += 1000
            return FakeStatus(0.5), None
        self.yt.inserted.append(self.body)
        return None, {"id": f"yt{len(self.yt.inserted)}", "status": {"privacyStatus": self.body["status"]["privacyStatus"]}}


class _Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeYouTube:
    def __init__(self):
        self.script: list[str] = []
        self.initiations = 0
        self.status_queries = 0
        self.inserted: list[dict] = []
        self.updated: list[dict] = []
        self.list_calls = 0

    # service.videos() / service.channels()
    def videos(self):
        return self

    def channels(self):
        yt = self

        class C:
            def list(self, **kw):
                return _Exec(lambda: {"items": [{"id": "UC123", "snippet": {"title": "Test Channel", "thumbnails": {"default": {"url": "http://x/t.jpg"}}}}]})

        return C()

    def insert(self, part, body, media_body, **kw):
        return FakeInsertRequest(self, body)

    def list(self, **kw):
        self.list_calls += 1
        last = self.inserted[-1] if self.inserted else {"status": {"privacyStatus": "private"}}
        return _Exec(lambda: {"items": [{"status": {"privacyStatus": last["status"]["privacyStatus"], "publishAt": last["status"].get("publishAt"), "uploadStatus": "uploaded"}, "snippet": {"title": "t"}}]})

    def update(self, part, body):
        self.updated.append(body)
        return _Exec(lambda: body)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(settings, "data_dir", data)
    monkeypatch.setattr(settings, "credentials_dir", tmp_path / "credentials")
    monkeypatch.setattr(settings, "incoming_dir", data / "incoming")
    monkeypatch.setattr(settings, "library_dir", data / "library")
    monkeypatch.setattr(settings, "client_secret_file", tmp_path / "credentials" / "client_secret.json")
    monkeypatch.setattr(settings, "agent_api_token", "agent-secret")
    monkeypatch.setattr(settings, "dashboard_token", "")
    settings.ensure_dirs()
    db.init_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}", sqlite_timeout=3)
    db.create_all()

    yt = FakeYouTube()
    monkeypatch.setattr(oauth, "service_factory", lambda creds: yt)
    monkeypatch.setattr(oauth, "get_credentials", lambda acct: object())
    monkeypatch.setattr(validation, "ffprobe_runner", lambda path: json.loads(json.dumps(GOOD_PROBE)))
    monkeypatch.setattr(uploader, "sleep", lambda s: None)
    monkeypatch.setattr(metadata, "ai_generator", None)
    yield yt
    from app.events import flush_events

    flush_events()


def make_file(path: Path, size: int = 4096, age: int = 60) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))
    t = time.time() - age
    os.utime(path, (t, t))
    return path


@pytest.fixture()
def account(env):
    with db.session_scope() as s:
        a = YouTubeAccount(channel_id="UC123", channel_name="Test Channel", connection_status="connected",
                           credential_reference="UC123.json", is_active=True,
                           granted_scopes="https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly")
        s.add(a)
        s.flush()
        return a.id


@pytest.fixture()
def approved_video(env, account):
    return create_video(approved=True)


def create_video(approved: bool = True, title: str = "GTA 6 vs GTA 5 water physics", name: str | None = None) -> int:
    name = name or f"clip_{time.time_ns()}.mp4"
    path = make_file(settings.library_dir / name)
    with db.session_scope() as s:
        v = Video(file_path=str(path), title=title, description="desc", duration=42.0, width=1080, height=1920,
                  validation_ok=True, permission_status=PermissionStatus.ORIGINAL, file_sha256=name)
        v.tags = ["GTA 6"]
        v.hashtags = ["#GTA6"]
        if approved:
            v.status = VideoStatus.APPROVED
            v.reviewed_at = utcnow()
            v.rights_review_status = RightsReview.CLEARED
        s.add(v)
        s.flush()
        return v.id
