import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.models import Video, VideoStatus
from app.services import ingest, metadata
from app.timeutil import utcnow
from tests.conftest import create_video, make_file

H = {"X-Requested-With": "ytshorts"}


@pytest.fixture()
def client(env, monkeypatch):
    from app import jobs, main

    monkeypatch.setattr(jobs, "trigger_queue", lambda: None)
    with TestClient(main.create_app()) as c:
        yield c


def test_csrf_header_required(client):
    assert client.put("/api/settings", json={"daily_upload_limit": 2}).status_code == 403
    assert client.put("/api/settings", json={"daily_upload_limit": 2}, headers=H).status_code == 200
    bad_origin = {**H, "Origin": "https://evil.example"}
    assert client.put("/api/settings", json={"daily_upload_limit": 2}, headers=bad_origin).status_code == 403


def test_dashboard_token(client, monkeypatch):
    monkeypatch.setattr(settings, "dashboard_token", "s3cret")
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.post("/api/login", json={"token": "s3cret"}).status_code == 200
    assert client.get("/api/overview").status_code == 200  # cookie session


def test_settings_cannot_disable_approval(client):
    r = client.put("/api/settings", json={"approval_required": False}, headers=H)
    assert r.status_code == 400


def test_settings_times_validated(client):
    assert client.put("/api/settings", json={"publishing_times": ["25:00"]}, headers=H).status_code == 400
    r = client.put("/api/settings", json={"publishing_times": ["21:00", "13:00"]}, headers=H)
    assert r.json()["publishing_times"] == ["13:00", "21:00"]


def test_no_secrets_in_auth_status(client, account):
    body = client.get("/api/auth/status").text
    assert "refresh_token" not in body and "client_secret\"" not in body
    assert json.loads(body)["channel"]["name"] == "Test Channel"


def test_review_and_upload_flow(client, account, env):
    vid = create_video(approved=False)
    assert client.post(f"/api/videos/{vid}/upload", json={"mode": "private"}, headers=H).status_code == 400
    r = client.patch(f"/api/videos/{vid}", json={"title": "New title", "hashtags": ["GTA6", "Gaming"]}, headers=H)
    assert r.json()["title"] == "New title" and r.json()["hashtags"] == ["#GTA6", "#Gaming"]
    assert client.post(f"/api/videos/{vid}/approve", json={"rights_confirmed": False}, headers=H).status_code == 400
    assert client.post(f"/api/videos/{vid}/approve", json={"rights_confirmed": True}, headers=H).json()["status"] == "approved"
    r = client.post(f"/api/videos/{vid}/upload", json={"mode": "public"}, headers=H)
    assert r.status_code == 400 and "disabled" in r.json()["detail"]
    r = client.post(f"/api/videos/{vid}/upload", json={"mode": "private"}, headers=H)
    assert r.status_code == 200 and r.json()["upload_status"] == "queued"
    from app.jobs import process_queue

    process_queue()
    v = client.get(f"/api/videos/{vid}").json()
    assert v["status"] == "uploaded" and v["uploads"][0]["youtube_video_id"] == "yt1"
    assert client.delete(f"/api/videos/{vid}", headers=H).status_code == 400  # uploaded -> keep record


def test_schedule_in_karachi_time(client, approved_video):
    client.put("/api/settings", json={"allow_public_publishing": True}, headers=H)
    local = (utcnow() + timedelta(days=2, hours=5)).replace(hour=19, minute=30, second=0, microsecond=0)
    r = client.post(f"/api/videos/{approved_video}/upload", json={"mode": "scheduled", "publish_at": local.strftime("%Y-%m-%dT%H:%M")}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["scheduled_at_local"].endswith("+05:00")
    assert r.json()["scheduled_at_local"][11:16] == "19:30"
    cal = client.get("/api/calendar").json()
    assert cal["timezone"] == "Asia/Karachi" and len(cal["entries"]) == 1
    # reschedule before upload
    new_local = local + timedelta(days=1)
    r2 = client.post(f"/api/uploads/{r.json()['id']}/reschedule", json={"publish_at": new_local.strftime("%Y-%m-%dT%H:%M")}, headers=H)
    assert r2.status_code == 200 and r2.json()["scheduled_at_local"][:10] == new_local.strftime("%Y-%m-%d")


def test_past_schedule_rejected(client, approved_video):
    client.put("/api/settings", json={"allow_public_publishing": True}, headers=H)
    r = client.post(f"/api/videos/{approved_video}/upload", json={"mode": "scheduled", "publish_at": "2020-01-01T10:00"}, headers=H)
    assert r.status_code == 400 and "past" in r.json()["detail"]


# ------------------------------------------------------------------ agent API
A = {"Authorization": "Bearer agent-secret"}


def test_agent_requires_token(client):
    assert client.get("/api/agent/quota").status_code == 401
    assert client.get("/api/agent/quota", headers=A).status_code == 200


def test_agent_cannot_upload_unapproved(client, account):
    vid = create_video(approved=False)
    pend = client.get("/api/agent/pending-clips", headers=A).json()
    assert [p["id"] for p in pend] == [vid]
    r = client.post(f"/api/agent/videos/{vid}/upload", json={"mode": "private"}, headers=A)
    assert r.status_code == 400
    assert client.post(f"/api/agent/videos/{vid}/request-approval", json={"message": "looks good"}, headers=A).status_code == 200
    # Agent can't use dashboard endpoints with its token either (no CSRF header / approval is human-only)
    assert client.post(f"/api/videos/{vid}/approve", json={"rights_confirmed": True}, headers=A).status_code == 403


def test_agent_can_queue_approved(client, approved_video):
    r = client.post(f"/api/agent/videos/{approved_video}/upload", json={"mode": "private"}, headers=A)
    assert r.status_code == 200
    r = client.post(f"/api/agent/videos/{approved_video}/upload", json={"mode": "scheduled"}, headers=A)
    assert r.status_code == 400  # already queued / public disabled


# ------------------------------------------------------------------ ingest + metadata
def _drop(name, meta):
    mp4 = make_file(settings.incoming_dir / f"{name}.mp4")
    sc = settings.incoming_dir / f"{name}.json"
    sc.write_text(json.dumps(meta))
    import os, time
    t = time.time() - 60
    os.utime(sc, (t, t))
    return mp4


def test_ingest_imports_and_generates_metadata(env, monkeypatch):
    seen = {}

    def fake_ai(prompt):
        seen["prompt"] = prompt
        return {"title": "GTA 6 police chase AI reacts differently than GTA 5", "description": "A look at the chase.",
                "hashtags": ["GTA6", "GTA5"], "tags": ["GTA 6", "GTA 6 police"], "category_id": "20", "notes": "none"}

    monkeypatch.setattr(metadata, "ai_generator", fake_ai)
    monkeypatch.setattr(settings, "metadata_ai_enabled", True)
    _drop("clip1", {"source_url": "https://example.com/v", "source_creator": "Streamer", "permission_status": "authorized",
                    "clip_start": 12.5, "clip_end": 54, "topic": "GTA 6 police", "context_notes": "Police chase comparison"})
    ids = ingest.scan_incoming()
    assert len(ids) == 1
    with db.session_scope() as s:
        v = s.get(Video, ids[0])
        assert v.status == VideoStatus.PENDING_REVIEW and v.metadata_source == "ai"
        assert v.title.startswith("GTA 6 police chase") and v.hashtags == ["#GTA6", "#GTA5"]
        assert v.permission_status == "authorized" and v.rights_review_status == "pending"
        assert v.clip_start_time == 12.5 and v.width == 1080 and v.validation_ok
        assert str(settings.library_dir) in v.file_path
        assert v.suggested_publish_at is not None
    assert "Police chase comparison" in seen["prompt"]
    assert not list(settings.incoming_dir.glob("*.mp4"))


def test_ingest_skips_files_still_rendering_and_duplicates(env):
    mp4 = make_file(settings.incoming_dir / "fresh.mp4", age=0)
    (settings.incoming_dir / "fresh.json").write_text("{}")
    assert ingest.scan_incoming() == []
    assert mp4.exists()  # left in place for the next scan

    a = _drop("dupA", {})
    data = a.read_bytes()
    assert len(ingest.scan_incoming()) == 1
    b = make_file(settings.incoming_dir / "dupB.mp4")
    b.write_bytes(data)
    import os, time
    t = time.time() - 60
    os.utime(b, (t, t))
    (settings.incoming_dir / "dupB.json").write_text("{}")
    os.utime(settings.incoming_dir / "dupB.json", (t, t))
    assert ingest.scan_incoming() == []
    assert (settings.incoming_dir / "duplicates" / "dupB.mp4").exists()


def test_template_metadata_without_ai_and_unique_titles(env, account):
    create_video(title="GTA 6: Police chase")
    vid = create_video(approved=False, title=None)
    with db.session_scope() as s:
        v = s.get(Video, vid)
        v.topic = "GTA 6"
        v.context_notes = "Police chase. More text."
        res = metadata.generate_metadata(s, v)
        assert res["source"] == "template"
        assert v.title == "GTA 6: Police chase (Part 2)"


def test_oauth_start_url(client, env):
    settings.client_secret_file.write_text(json.dumps({"installed": {
        "client_id": "abc.apps.googleusercontent.com", "client_secret": "x", "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}}))
    r = client.post("/api/auth/start", headers=H)
    url = r.json()["auth_url"]
    assert "access_type=offline" in url and "prompt=consent" in url and "code_challenge=" in url
    assert "youtube.upload" in url and "youtube.readonly" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Fapi%2Fauth%2Fcallback" in url
    # bad state -> redirect to dashboard with error, no crash
    r = client.get("/api/auth/callback?state=nope&code=x", follow_redirects=False)
    assert r.status_code == 307 and "oauth=error" in r.headers["location"]
    r = client.get("/api/auth/callback?error=access_denied", follow_redirects=False)
    assert "denied" in r.headers["location"]


def test_oauth_start_without_client_secret(client):
    r = client.post("/api/auth/start", headers=H)
    assert r.status_code == 400 and "client_secret" in r.json()["detail"].lower() or "OAuth client file" in r.json()["detail"]


def test_dashboard_import_video(client):
    import io, os
    data = os.urandom(5000)
    form = {"topic": "GTA 6", "context_notes": "Police chase", "permission_status": "original", "source_creator": "Me"}
    r = client.post("/api/videos/import", data=form, files={"file": ("my clip.mp4", io.BytesIO(data), "video/mp4")}, headers=H)
    assert r.status_code == 200, r.text
    v = client.get(f"/api/videos/{r.json()['id']}").json()
    assert v["status"] == "pending_review" and v["validation_ok"] and v["source"]["permission_status"] == "original"
    assert v["title"]  # template metadata generated
    r2 = client.post("/api/videos/import", data=form, files={"file": ("again.mp4", io.BytesIO(data), "video/mp4")}, headers=H)
    assert r2.status_code == 409
    r3 = client.post("/api/videos/import", files={"file": ("x.mov", io.BytesIO(b"1"), "video/quicktime")}, headers=H)
    assert r3.status_code == 400
    assert client.post("/api/videos/import", files={"file": ("x.mp4", io.BytesIO(data), "video/mp4")}).status_code == 403
