from datetime import datetime, timedelta

import pytest

from app import db
from app.models import ApiRequestLog, PublishMode, QuotaUsage, Upload, UploadStatus, Video, VideoStatus
from app.services import quota, scheduling, uploader, validation, workflow
from app.services.metadata_rules import build_upload_body, clean_hashtags, clean_tags, clean_title
from app.services.quota import get_schedule
from app.timeutil import parse_local, quota_day, to_rfc3339, utcnow
from tests.conftest import GOOD_PROBE, create_video, make_file


# ------------------------------------------------------------------ time
def test_karachi_to_utc_and_rfc3339():
    when = parse_local("2026-11-20T18:00", "Asia/Karachi")  # PKT = UTC+5
    assert when == datetime(2026, 11, 20, 13, 0)
    assert to_rfc3339(when) == "2026-11-20T13:00:00.000Z"
    assert parse_local("2026-11-20T13:00:00Z", "Asia/Karachi") == datetime(2026, 11, 20, 13, 0)


def test_quota_day_resets_at_pacific_midnight():
    # November: PST = UTC-8 -> midnight Pacific is 08:00 UTC
    assert quota_day(datetime(2026, 11, 16, 7, 59)).isoformat() == "2026-11-15"
    assert quota_day(datetime(2026, 11, 16, 8, 0)).isoformat() == "2026-11-16"
    # July: PDT = UTC-7
    assert quota_day(datetime(2026, 7, 1, 6, 59)).isoformat() == "2026-06-30"
    assert quota_day(datetime(2026, 7, 1, 7, 0)).isoformat() == "2026-07-01"


# ------------------------------------------------------------------ quota
def test_quota_limits_and_logging(env):
    with db.session_scope() as s:
        get_schedule(s).max_daily_upload_calls = 2
    a = quota.begin_request("videos.insert")
    quota.finish_request(a, True, 200, upload_succeeded=True)
    b = quota.begin_request("videos.insert")
    quota.finish_request(b, False, 503, "backendError")
    with pytest.raises(quota.QuotaBlocked):
        quota.begin_request("videos.insert")
    # other bucket is independent
    quota.finish_request(quota.begin_request("videos.list"), True, 200)
    with db.session_scope() as s:
        st = quota.status(s)
        assert st["buckets"]["upload"]["used"] == 2
        assert st["buckets"]["upload"]["successful_uploads"] == 1
        assert st["buckets"]["upload"]["failed_requests"] == 1
        assert st["buckets"]["other"]["used"] == 1
        assert st["uploads_allowed"] is False
        logs = s.query(ApiRequestLog).all()
        assert [l.method for l in logs] == ["videos.insert", "videos.insert", "videos.list"]


def test_quota_exhausted_and_uncertain_block(env):
    quota.mark_exhausted(quota.UPLOAD, "quotaExceeded")
    with pytest.raises(quota.QuotaBlocked, match="exhausted"):
        quota.check_allowed("videos.insert")
    quota.check_allowed("videos.list")  # other bucket unaffected
    quota.mark_uncertain(quota.OTHER, "crash")
    with pytest.raises(quota.QuotaBlocked, match="uncertain"):
        quota.check_allowed("videos.list")
    quota.clear_uncertain()
    quota.check_allowed("videos.list")


def test_pending_requests_flag_uncertain_on_restart(env):
    quota.begin_request("videos.insert")  # never finished: simulated crash
    assert quota.reconcile_pending_requests() == 1
    with pytest.raises(quota.QuotaBlocked, match="uncertain"):
        quota.check_allowed("videos.insert")


def test_previous_day_usage_does_not_count(env):
    with db.session_scope() as s:
        s.add(QuotaUsage(quota_date=quota_day() - timedelta(days=1), quota_type="upload", units_used=999, exhausted=True))
    quota.check_allowed("videos.insert")


# ------------------------------------------------------------------ validation
def _probe(**changes):
    import copy
    p = copy.deepcopy(GOOD_PROBE)
    for k, v in changes.items():
        if k == "video":
            p["streams"][0].update(v)
        elif k == "format":
            p["format"].update(v)
        elif k == "no_audio":
            p["streams"] = p["streams"][:1]
    return p


@pytest.mark.parametrize("probe,expect", [
    (_probe(), None),
    (_probe(video={"width": 1920, "height": 1080}), "Horizontal"),
    (_probe(format={"duration": "200"}), "3-minute"),
    (_probe(no_audio=True), "No audio"),
    (_probe(video={"codec_name": "mpeg4"}), "Unsupported video codec"),
    (_probe(format={"format_name": "matroska,webm"}), "not MP4"),
])
def test_validation(env, monkeypatch, tmp_path, probe, expect):
    monkeypatch.setattr(validation, "ffprobe_runner", lambda p: probe)
    f = make_file(tmp_path / "v.mp4")
    res = validation.validate_video(f)
    if expect is None:
        assert res.ok, res.errors
        assert (res.width, res.height, res.duration) == (1080, 1920, 42.0)
    else:
        assert not res.ok
        assert any(expect in e for e in res.errors), res.errors


def test_validation_rejects_fresh_or_partial_files(env, tmp_path):
    fresh = make_file(tmp_path / "fresh.mp4", age=0)
    assert any("rendering" in e for e in validation.validate_video(fresh).errors)
    partial = make_file(tmp_path / "clip.partial.mp4")
    assert not validation.validate_video(partial).ok
    assert not validation.validate_video(tmp_path / "missing.mp4").ok


def test_rotated_video_counts_as_vertical(env, monkeypatch, tmp_path):
    monkeypatch.setattr(validation, "ffprobe_runner", lambda p: _probe(video={"width": 1920, "height": 1080, "side_data_list": [{"rotation": -90}]}))
    res = validation.validate_video(make_file(tmp_path / "r.mp4"))
    assert res.ok and (res.width, res.height) == (1080, 1920)


# ------------------------------------------------------------------ metadata rules
def test_metadata_cleaning():
    assert clean_title("  <b>GTA 6</b>   is   here ") == "bGTA 6/b is here"
    assert len(clean_title("x" * 300)) == 100
    assert clean_hashtags(["GTA6", "#gta6", "#GTA 5", "a", "b", "c", "d"]) == ["#GTA6", "#GTA5", "#a", "#b", "#c"]
    tags = clean_tags(["GTA 6"] + [f"tag{i}" * 10 for i in range(40)])
    assert sum(len(t) for t in tags) <= 500 and tags[0] == "GTA 6"


def test_upload_body_scheduled_is_private_with_publish_at():
    v = Video(title="t", description="d", category_id="20", file_path="x")
    v.hashtags = ["#GTA6"]
    v.tags = []
    up = Upload(publish_mode=PublishMode.SCHEDULED, scheduled_at=datetime(2026, 12, 1, 13, 0))
    body = build_upload_body(v, up)
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["publishAt"] == "2026-12-01T13:00:00.000Z"
    assert "#GTA6" in body["snippet"]["description"]
    body2 = build_upload_body(v, Upload(publish_mode=PublishMode.UNLISTED))
    assert body2["status"]["privacyStatus"] == "unlisted" and "publishAt" not in body2["status"]


# ------------------------------------------------------------------ scheduling
def test_next_free_slots_respect_limit_and_times(env):
    with db.session_scope() as s:
        sched = get_schedule(s)
        sched.publishing_times = ["13:00", "18:00", "21:00"]
        sched.daily_upload_limit = 2
        slots = scheduling.next_free_slots(s, sched, count=4)
    assert len(slots) == 4
    days = {}
    for sl in slots:
        local = sl + timedelta(hours=5)
        assert local.strftime("%H:%M") in {"13:00", "18:00"}
        days.setdefault(local.date(), 0)
        days[local.date()] += 1
    assert all(n <= 2 for n in days.values())
    assert all(sl > utcnow() for sl in slots)


def test_publish_time_validation(env):
    with db.session_scope() as s:
        sched = get_schedule(s)
        with pytest.raises(scheduling.ScheduleError, match="past"):
            scheduling.validate_publish_time(s, sched, utcnow() - timedelta(minutes=5))
        with pytest.raises(scheduling.ScheduleError, match="at least"):
            scheduling.validate_publish_time(s, sched, utcnow() + timedelta(minutes=5))
        scheduling.validate_publish_time(s, sched, utcnow() + timedelta(days=1))


# ------------------------------------------------------------------ workflow
def test_unapproved_video_cannot_be_queued(env, account):
    vid = create_video(approved=False)
    with db.session_scope() as s:
        with pytest.raises(workflow.WorkflowError, match="approved"):
            workflow.enqueue_upload(s, s.get(Video, vid), PublishMode.PRIVATE, None, "dashboard")


def test_approval_rules(env, account):
    vid = create_video(approved=False)
    with db.session_scope() as s:
        v = s.get(Video, vid)
        with pytest.raises(workflow.WorkflowError, match="Agents cannot approve"):
            workflow.approve(s, v, "agent", True)
        with pytest.raises(workflow.WorkflowError, match="Confirm"):
            workflow.approve(s, v, "dashboard", rights_confirmed=False)
        v.permission_status = "unknown"
        with pytest.raises(workflow.WorkflowError, match="permission status"):
            workflow.approve(s, v, "dashboard", True)
        v.permission_status = "original"
        v.validation_ok = False
        with pytest.raises(workflow.WorkflowError, match="validation"):
            workflow.approve(s, v, "dashboard", True)
        v.validation_ok = True
        workflow.approve(s, v, "dashboard", True)
        assert v.status == VideoStatus.APPROVED and v.rights_review_status == "cleared"


def test_public_and_scheduled_blocked_until_enabled(env, approved_video):
    with db.session_scope() as s:
        v = s.get(Video, approved_video)
        for mode in (PublishMode.PUBLIC, PublishMode.SCHEDULED):
            with pytest.raises(workflow.WorkflowError, match="disabled"):
                workflow.enqueue_upload(s, v, mode, utcnow() + timedelta(days=1), "dashboard")
        get_schedule(s).allow_public_publishing = True
        with pytest.raises(scheduling.ScheduleError):
            workflow.enqueue_upload(s, v, PublishMode.SCHEDULED, utcnow() - timedelta(hours=1), "dashboard")
        up = workflow.enqueue_upload(s, v, PublishMode.SCHEDULED, None, "dashboard")  # next free slot
        assert up.scheduled_at > utcnow() and v.status == VideoStatus.QUEUED


# ------------------------------------------------------------------ upload engine
def _queue(vid, mode=PublishMode.PRIVATE, when=None):
    with db.session_scope() as s:
        if mode in (PublishMode.SCHEDULED, PublishMode.PUBLIC):
            get_schedule(s).allow_public_publishing = True
        return workflow.enqueue_upload(s, s.get(Video, vid), mode, when, "test").id


def _state(up_id):
    with db.session_scope() as s:
        up = s.get(Upload, up_id)
        return up.upload_status, up.youtube_video_id, up.video.status, up.resumable_uri, up.error_kind


def test_private_upload_success(env, approved_video):
    yt = env
    up_id = _queue(approved_video)
    out = uploader.run_upload(up_id)
    assert out.status == UploadStatus.SUCCEEDED and out.youtube_video_id == "yt1"
    assert _state(up_id)[:3] == (UploadStatus.SUCCEEDED, "yt1", VideoStatus.UPLOADED)
    assert yt.inserted[0]["status"]["privacyStatus"] == "private"
    assert yt.list_calls == 1  # post-upload verification
    with db.session_scope() as s:
        st = quota.status(s)
    assert st["buckets"]["upload"]["used"] == 1 and st["buckets"]["upload"]["successful_uploads"] == 1


def test_network_error_resumes_same_session(env, approved_video):
    yt = env
    yt.script = ["chunk", "net", "net", "chunk", "done"]
    up_id = _queue(approved_video)
    out = uploader.run_upload(up_id)
    assert out.status == UploadStatus.SUCCEEDED
    assert yt.initiations == 1          # never started a second videos.insert
    assert yt.status_queries >= 1       # queried committed range before resuming
    with db.session_scope() as s:
        assert quota.status(s)["buckets"]["upload"]["used"] == 1


def test_retryable_5xx_then_success(env, approved_video):
    env.script = ["500", "500", "done"]
    assert uploader.run_upload(_queue(approved_video)).status == UploadStatus.SUCCEEDED
    assert env.initiations == 1


def test_resume_after_restart_uses_saved_session(env, approved_video, monkeypatch):
    yt = env
    monkeypatch.setattr(uploader.settings, "upload_max_retries", 2)
    yt.script = ["chunk", "net", "net", "net"]
    up_id = _queue(approved_video)
    out = uploader.run_upload(up_id)
    status, yt_id, vstatus, uri, kind = _state(up_id)
    assert status == UploadStatus.PAUSED and uri and kind == "network" and yt_id is None
    assert vstatus == VideoStatus.QUEUED  # never marked as success
    # "restart": a new run picks up the persisted session URI
    yt.script = ["chunk", "done"]
    out = uploader.run_upload(up_id)
    assert out.status == UploadStatus.SUCCEEDED
    assert yt.initiations == 1
    with db.session_scope() as s:
        st = quota.status(s)["buckets"]["upload"]
    assert st["used"] == 1 and st["successful_uploads"] == 1


def test_quota_error_pauses_and_marks_exhausted(env, approved_video):
    env.script = ["quota"]
    up_id = _queue(approved_video)
    out = uploader.run_upload(up_id)
    assert out.error_kind == "quota"
    assert _state(up_id)[0] == UploadStatus.QUEUED
    with pytest.raises(quota.QuotaBlocked):
        quota.check_allowed("videos.insert")


def test_permanent_error_not_retried(env, approved_video):
    env.script = ["bad", "done"]
    up_id = _queue(approved_video)
    out = uploader.run_upload(up_id)
    assert out.status == UploadStatus.FAILED and out.error_kind == "permanent"
    assert env.initiations == 1 and env.inserted == []
    assert _state(up_id)[2] == VideoStatus.FAILED


def test_expired_session_starts_new_insert(env, approved_video):
    env.script = ["chunk", "expired", "done"]
    up_id = _queue(approved_video)
    assert uploader.run_upload(up_id).status == UploadStatus.SUCCEEDED
    assert env.initiations == 2
    with db.session_scope() as s:
        assert quota.status(s)["buckets"]["upload"]["used"] == 2


def test_upload_limit_blocks_new_uploads(env, approved_video):
    with db.session_scope() as s:
        get_schedule(s).max_daily_upload_calls = 0
    up_id = _queue(approved_video)
    out = uploader.run_upload(up_id)
    assert out.error_kind == "quota" and env.initiations == 0


def test_validation_failure_blocks_upload(env, approved_video, monkeypatch):
    monkeypatch.setattr(validation, "ffprobe_runner", lambda p: _probe(video={"width": 1920, "height": 1080}))
    up_id = _queue(approved_video)
    out = uploader.run_upload(up_id)
    assert out.status == UploadStatus.FAILED and out.error_kind == "validation" and env.initiations == 0


def test_cancel_queued_upload(env, approved_video):
    up_id = _queue(approved_video)
    uploader.request_cancel(up_id)
    assert _state(up_id)[0] == UploadStatus.CANCELLED
    assert uploader.run_upload(up_id).status == UploadStatus.CANCELLED
    assert env.initiations == 0


def test_cancel_during_upload(env, approved_video, monkeypatch):
    yt = env
    yt.script = ["chunk", "chunk", "chunk", "done"]
    up_id = _queue(approved_video)
    real = uploader._cancel_flag
    calls = {"n": 0}

    def flag(i):
        calls["n"] += 1
        return calls["n"] >= 2 or real(i)

    monkeypatch.setattr(uploader, "_cancel_flag", flag)
    assert uploader.run_upload(up_id).status == UploadStatus.CANCELLED
    assert yt.inserted == []


def test_duplicate_upload_blocked(env, approved_video):
    up_id = _queue(approved_video)
    uploader.run_upload(up_id)
    with db.session_scope() as s:
        v = s.get(Video, approved_video)
        v.status = VideoStatus.APPROVED
        with pytest.raises(workflow.WorkflowError, match="duplicate"):
            workflow.enqueue_upload(s, v, PublishMode.PRIVATE, None, "dashboard")
    # identical file content under a different video record is also blocked at upload time
    other = create_video(title="Another title")
    with db.session_scope() as s:
        s.get(Video, other).file_sha256 = s.get(Video, approved_video).file_sha256
    up2 = _queue(other)
    out = uploader.run_upload(up2)
    assert out.status == UploadStatus.FAILED and "Duplicate" in out.error


def test_scheduled_upload_sets_publish_at(env, approved_video):
    when = utcnow().replace(microsecond=0) + timedelta(days=2)
    up_id = _queue(approved_video, PublishMode.SCHEDULED, when)
    assert uploader.run_upload(up_id).status == UploadStatus.SUCCEEDED
    st = env.inserted[0]["status"]
    assert st["privacyStatus"] == "private" and st["publishAt"] == to_rfc3339(when)
    assert _state(up_id)[2] == VideoStatus.SCHEDULED


def test_scheduled_time_passed_before_upload_is_refused(env, approved_video):
    up_id = _queue(approved_video, PublishMode.SCHEDULED, utcnow() + timedelta(days=1))
    with db.session_scope() as s:
        s.get(Upload, up_id).scheduled_at = utcnow() + timedelta(minutes=2)
    out = uploader.run_upload(up_id)
    assert out.status == UploadStatus.FAILED and "too close" in out.error and env.initiations == 0


def test_auth_failure_pauses(env, approved_video, monkeypatch):
    from app.services import oauth
    from app.services.youtube_errors import AuthError

    def boom(acct):
        raise AuthError("revoked", status="revoked")

    monkeypatch.setattr(oauth, "get_credentials", boom)
    up_id = _queue(approved_video)
    out = uploader.run_upload(up_id)
    assert out.error_kind == "auth" and _state(up_id)[0] == UploadStatus.PAUSED and env.initiations == 0
