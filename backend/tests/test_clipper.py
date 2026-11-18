"""Clipping engine tests. OpenAI is mocked; FFmpeg is real (tests are skipped without it)."""
import json
import re
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import db
from app.clipper import ai, analysis, captions, worker
from app.clipper import settings as cs
from app.models import CandidateStatus, ClipCandidate, ClipProject, ProjectStatus, Video, VideoStatus

try:
    FF = cs.ffmpeg_path()
except RuntimeError:
    FF = None
needs_ffmpeg = pytest.mark.skipif(FF is None, reason="ffmpeg not installed")
H = {"X-Requested-With": "ytshorts"}


def make_source(path, seconds=40):
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
                    "-af", "volume='if(between(t,20,22),1,0.05)':eval=frame",  # loud spike at 20-22 s
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True)
    return path


def fake_transcribe(path):
    segs, words = [], []
    for i in range(0, 1200, 4):  # one sentence every 4 s (chunk-relative)
        segs.append({"start": i, "end": i + 3.5, "text": f"Sentence {i} what the fuck is that boat."})
        for j, w in enumerate(["what", "the", "fuck", "is", "that", "boat"]):
            words.append({"start": i + j * 0.5, "end": i + j * 0.5 + 0.4, "word": w})
    return {"segments": segs, "words": words, "text": ""}


def fake_moments(system, user, schema):
    assert "Loud moments" in user and "Transcript" in user
    return {"moments": [
        {"start_seconds": 13, "end_seconds": 31, "score": 9, "category": "action", "headline": "Boat goes boom",
         "hook_line": "Watch this boat.", "outro_line": "Would you jump?", "reason": "loud payoff"},
        {"start_seconds": 14, "end_seconds": 30, "score": 5, "category": "funny", "headline": "dup", "hook_line": "x",
         "outro_line": "y", "reason": "overlaps the first"},
        {"start_seconds": 2, "end_seconds": 1, "score": 9, "category": "funny", "headline": "bad", "hook_line": "x",
         "outro_line": "y", "reason": "end before start"},
    ]}


def fake_tts(text, out):
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=500:duration=1.5",
                    "-af", "volume='0.5+0.5*sin(2*PI*6*t)':eval=frame", str(out)], check=True)


@pytest.fixture()
def clip_env(env, monkeypatch):
    monkeypatch.setattr(ai, "transcribe_hook", fake_transcribe)
    monkeypatch.setattr(ai, "json_hook", fake_moments)
    monkeypatch.setattr(ai, "tts_hook", fake_tts)
    monkeypatch.setattr(cs, "openai_available", lambda: True)
    monkeypatch.setattr(worker, "synchronous", True)
    yield env


# ------------------------------------------------------------------ pure functions
def test_find_moments_snaps_dedupes_and_validates():
    segs = [{"start": float(i), "end": i + 3.5, "text": f"s{i}"} for i in range(0, 100, 4)]
    ai.json_hook = fake_moments
    try:
        out = analysis.find_moments(segs, [(20, 8.0)], 100, 10, 60, 5)
    finally:
        ai.json_hook = None
    assert len(out) == 1  # overlap removed, invalid dropped
    m = out[0]
    assert m["start"] == 11.7 and m["end"] == pytest.approx(31.9)  # snapped to sentence start/end (+padding)
    assert m["headline"] == "Boat goes boom" and "s12" in m["excerpt"]


def test_captions_mask_profanity_and_highlight():
    words = [{"start": 0.0, "end": 0.3, "word": "what"}, {"start": 0.35, "end": 0.6, "word": "the"},
             {"start": 0.65, "end": 0.9, "word": "fuck"}, {"start": 2.0, "end": 2.3, "word": "boat"}]
    ass = captions.build_ass("Fucking boat!", 5, words, captions.spread_words("Watch this", 3, 1), caption_y=1200)
    assert "FUCK" not in ass.upper().replace("F***", "")
    assert "F***" in ass and "F******" in ass  # word + headline masked
    assert ass.count("Style: Caption") == 1 and "\\pos(540,1200)" in ass
    assert "WATCH" in ass and captions.ts(61.5) == "0:01:01.50"
    groups = captions.group_words(words)
    assert [len(g) for g in groups] == [3, 1]  # gap > 0.6 s starts a new group


# ------------------------------------------------------------------ pipeline
@needs_ffmpeg
def test_analysis_and_render_end_to_end(clip_env, tmp_path):
    src = make_source(tmp_path / "stream.mp4")
    with db.session_scope() as s:
        p = ClipProject(name="t", source_path=str(src), permission_status="original", min_clip_seconds=10,
                        max_clip_seconds=60, max_candidates=5)
        s.add(p)
        s.flush()
        pid = p.id
    analysis.run_analysis(pid)
    with db.session_scope() as s:
        p = s.get(ClipProject, pid)
        assert p.status == ProjectStatus.READY, p.error
        assert p.duration == pytest.approx(40, abs=0.5) and (p.width, p.height) == (1280, 720)
        cands = list(p.candidates)
        assert len(cands) == 1
        cid = cands[0].id
    peaks = json.loads((cs.project_dir(pid) / "loudness.json").read_text())["peaks"]
    assert any(19 <= t <= 22 for t, _ in peaks)

    from app.clipper import render

    render.render_candidate(cid)
    with db.session_scope() as s:
        c = s.get(ClipCandidate, cid)
        assert c.status == CandidateStatus.RENDERED, c.error
        v = s.get(Video, c.video_id)
        assert v.status == VideoStatus.PENDING_REVIEW
        assert (v.width, v.height) == (1080, 1920)
        assert v.permission_status == "original" and v.clip_start_time == c.start_time
        assert "Watch this boat." in (v.original_commentary or "")
    out = subprocess.run([cs.settings.ffprobe_path, "-v", "error", "-show_entries", "stream=codec_name,width,height",
                          "-of", "json", v.file_path], capture_output=True, text=True)
    streams = json.loads(out.stdout)["streams"]
    assert {"h264", "aac"} <= {st["codec_name"] for st in streams}
    assert any(st.get("width") == 1080 and st.get("height") == 1920 for st in streams)


# ------------------------------------------------------------------ API
@pytest.fixture()
def client(clip_env, monkeypatch):
    from app import jobs, main

    monkeypatch.setattr(jobs, "trigger_queue", lambda: None)
    with TestClient(main.create_app()) as c:
        yield c


def test_project_requires_existing_video(client, tmp_path):
    r = client.post("/api/clipper/projects", json={"source_path": str(tmp_path / "missing.mp4")}, headers=H)
    assert r.status_code == 400
    (tmp_path / "x.txt").write_text("x")
    r = client.post("/api/clipper/projects", json={"source_path": str(tmp_path / "x.txt")}, headers=H)
    assert r.status_code == 400
    assert client.post("/api/clipper/projects", json={"source_path": "x"}).status_code == 403  # CSRF


def test_missing_openai_key_is_reported(client, monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "openai_available", lambda: False)
    f = tmp_path / "a.mp4"
    f.write_bytes(b"0")
    r = client.post("/api/clipper/projects", json={"source_path": str(f)}, headers=H)
    assert r.status_code == 400 and "OPENAI_API_KEY" in r.json()["detail"]


@needs_ffmpeg
def test_api_flow(client, tmp_path):
    src = make_source(tmp_path / "s.mp4", seconds=30)
    r = client.post("/api/clipper/projects", json={"source_path": str(src), "permission_status": "original",
                                                   "min_clip_seconds": 10, "voice": "nova"}, headers=H)
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    proj = client.get(f"/api/clipper/projects/{pid}").json()
    assert proj["status"] == "ready" and len(proj["candidates"]) == 1
    cid = proj["candidates"][0]["id"]
    # edit + validation
    assert client.patch(f"/api/clipper/candidates/{cid}", json={"start_time": 10, "end_time": 12}, headers=H).status_code == 400
    r = client.patch(f"/api/clipper/candidates/{cid}", json={"start_time": 12, "end_time": 24, "headline": "New"}, headers=H)
    assert r.json()["headline"] == "New" and r.json()["duration"] == 12
    # manual moment
    r = client.post(f"/api/clipper/projects/{pid}/candidates", json={"start_time": 2, "end_time": 9}, headers=H)
    manual = r.json()["id"]
    assert client.post(f"/api/clipper/candidates/{manual}/discard", headers=H).status_code == 200
    # render
    r = client.post(f"/api/clipper/projects/{pid}/render", json={"candidate_ids": [cid]}, headers=H)
    assert r.json()["queued"] == [cid]
    c = next(x for x in client.get(f"/api/clipper/projects/{pid}").json()["candidates"] if x["id"] == cid)
    assert c["status"] == "rendered", c["error"]
    assert client.get(f"/api/videos/{c['video_id']}").json()["status"] == "pending_review"
    assert client.get(f"/api/clipper/projects/{pid}/source").status_code == 200
    # linked source file is never deleted with the project
    assert client.delete(f"/api/clipper/projects/{pid}", headers=H).status_code == 200
    assert src.exists()


def test_restart_recovery(clip_env, monkeypatch):
    queued = []
    monkeypatch.setattr(worker, "enqueue_render", lambda cid: queued.append(cid))
    with db.session_scope() as s:
        p = ClipProject(name="r", source_path="x", status=ProjectStatus.TRANSCRIBING)
        s.add(p)
        s.flush()
        c = ClipCandidate(project_id=p.id, start_time=0, end_time=10, status=CandidateStatus.RENDERING)
        s.add(c)
        s.flush()
        pid, cid = p.id, c.id
    worker.recover_on_startup()
    with db.session_scope() as s:
        assert s.get(ClipProject, pid).status == ProjectStatus.FAILED
        assert s.get(ClipCandidate, cid).status == CandidateStatus.QUEUED
    assert queued == [cid]


# ------------------------------------------------------------------ smart framing
from app.clipper import framing


def _eval_expr(expr: str, t: float) -> float:
    env = {"gte": lambda a, b: float(a >= b), "lt": lambda a, b: float(a < b), "min": min, "max": max, "t": t}
    return eval(expr, {"__builtins__": {}}, env)


def _samples(n, fn):
    return [fn(i) for i in range(n)]


def test_framing_vertical_source_fills():
    f = framing.plan([], 1080, 1920)
    assert f.layout == "fill" and (f.panel_w, f.panel_h, f.panel_y) == (1080, 1920, 0)


def test_framing_holds_centre_during_camera_motion():
    # motion everywhere (camera turning, third-person chase): full-screen crop, held in the centre
    cols = _samples(40, lambda i: [100.0] * framing.SCAN_W)
    f = framing.plan(cols, 1280, 720)
    assert f.layout == "full" and (f.panel_w, f.panel_h, f.panel_y) == (1080, 1920, 0) and f.crop_w == 404
    for t in (0, 3, 7, 9.9):
        assert _eval_expr(f.x_expr, t) == pytest.approx((1280 - 404) / 2, abs=1)
    # low-resolution source: never full-screen (too soft), the panel is used instead
    assert framing.plan(cols, 854, 480).layout == "focus"


def test_framing_follows_localized_action_within_range():
    # after 2 s, action appears on the left third of the frame
    def cols(i):
        c = [1.0] * framing.SCAN_W
        if i >= 8:
            for x in range(20, 50):
                c[x] = 400.0
        return c
    f = framing.plan(_samples(60, cols), 1280, 720)
    assert f.layout == "focus"                            # action off to one side: not centred enough for full-screen
    start_x, end_x = _eval_expr(f.x_expr, 0.5), _eval_expr(f.x_expr, 14.9)
    assert start_x == pytest.approx(352, abs=2)          # centred at first
    assert end_x < start_x - 100                          # moved left towards the action...
    min_x = (0.5 - framing.FOLLOW_RANGE) * 1280 - 288
    assert end_x >= min_x - 1                             # ...but not beyond the follow range
    xs = [_eval_expr(f.x_expr, t / 10) for t in range(150)]
    assert all(abs(b - a) <= framing.MAX_SPEED * 1280 / 10 + 1 for a, b in zip(xs, xs[1:]))  # smooth, capped speed
    assert all(0 <= x <= 1280 - 576 for x in xs)


def test_framing_ignores_brief_flicker():
    def cols(i):
        c = [1.0] * framing.SCAN_W
        if i == 10:  # a single 0.25 s blip far left
            for x in range(0, 20):
                c[x] = 500.0
        return c
    f = framing.plan(_samples(40, cols), 1280, 720)
    centre = (1280 - f.crop_w) / 2
    assert {round(_eval_expr(f.x_expr, t / 4)) for t in range(40)} == {round(centre)}


# ------------------------------------------------------------------ caption design
def _words(text, step=0.3, gaps=()):
    out, t = [], 0.0
    for i, w in enumerate(text.split()):
        if i in gaps:
            t += 1.0
        out.append({"start": t, "end": t + 0.25, "word": w})
        t += step
    return out


def _texts(groups):
    return [" ".join(w["word"] for w in g) for g in groups]


def test_caption_groups_avoid_dangling_function_words():
    groups = _texts(captions.group_words(_words("I can't do this from here get me close")))
    assert all(not g.split()[-1].lower() in {"to", "the", "get", "can't"} for g in groups), groups
    assert "get me close" in groups
    assert all(1 <= len(g.split()) <= 3 for g in groups)


def test_caption_groups_split_at_pauses_and_punctuation():
    groups = _texts(captions.group_words(_words("Hold on. Nah homie wait", gaps=(4,))))
    assert groups[0] == "Hold on." and "wait" in groups[-1]
    assert not any("on. Nah" in g for g in groups)


def test_caption_groups_respect_line_length():
    for g in captions.group_words(_words("absolutely unbelievable spectacular performance tonight")):
        assert len(g) == 1 or captions._chars(g) <= captions.MAX_CHARS


def test_keyword_highlight_picks_important_words():
    g = [{"start": 0, "end": 1, "word": w} for w in ["the", "damn", "boat!"]]
    assert captions.pick_keyword(g, "to") == 2
    assert captions.pick_keyword([{"start": 0, "end": 1, "word": w} for w in ["I", "can't", "do"]], None) is None
    assert captions.pick_keyword([{"start": 0, "end": 1, "word": w} for w in ["catch", "Jimmy"]], "gotta") == 1


def test_caption_events_animate_and_highlight():
    ass = captions.build_ass("", 5, _words("get me to the damn boat!"), [], caption_y=771)
    events = [l for l in ass.splitlines() if l.startswith("Dialogue: 1")]
    assert len(events) == 6                                  # one event per spoken word
    n_groups = len(captions.group_words(_words("get me to the damn boat!")))
    assert sum(r"\move(540,797,540,771,0,130)" in e for e in events) == n_groups   # each group bounces in from below
    assert sum(r"\pos(540,771)" in e for e in events) == len(events) - n_groups
    assert sum(r"\alpha&H60&" in e for e in events) == n_groups
    assert any(r"\t(0,70,\fscx122" in e for e in events)    # stronger spoken-word pop
    assert "Segoe UI Black,92" in ass


# ------------------------------------------------------------------ opening hook card
def test_hook_card_only_during_opening():
    words = _words("early words then later words", step=0.6)          # 0.0 .. 2.4 s
    ass = captions.build_ass("Engine Shot! Boat Trouble!", 20, words, [], caption_y=771, card_end=1.5, card_y=744)
    dialogue = [l for l in ass.splitlines() if l.startswith("Dialogue")]
    card = [l for l in dialogue if l.startswith(("Dialogue: 4", "Dialogue: 5"))]
    assert len(card) == 2 and all(",0:00:00.00,0:00:01.50," in l for l in card)   # box + text, opening only
    assert not any(",Title," in l for l in dialogue)                               # no permanent headline
    assert r"\p1}m " in card[0] and "ENGINE SHOT!" in card[1]
    shown = " ".join(l for l in dialogue if l.startswith("Dialogue: 1"))
    assert "EARLY" not in shown and "LATER" in shown                               # captions start after the card


def test_hook_card_layout_fits_and_balances():
    lines, size, width = captions.layout_card_text("ENGINE SHOT! BOAT TROUBLE!")
    assert lines == ["ENGINE SHOT!", "BOAT TROUBLE!"] and size == 100 and width <= captions.CARD_MAX_W
    lines, size, width = captions.layout_card_text("THIS BOAT HEIST GOES HORRIBLY WRONG IN THE WORST POSSIBLE WAY")
    assert 2 <= len(lines) <= 3 and size >= 60 and width <= captions.CARD_MAX_W
    assert captions.card_duration(None) == captions.CARD_MIN
    assert captions.card_duration(10) == captions.CARD_MAX
    assert captions.card_duration(2.5) == pytest.approx(2.65)


def test_hook_card_shadow_keeps_its_own_opacity():
    ev = captions.hook_card_events("Boom", 2.0, 744)
    assert r"\1a&H00&\4a&H70&" in ev[0] and r"\alpha" not in ev[0]


# ------------------------------------------------------------------ avatar character
from app.clipper import avatar, media


def test_blink_schedule_is_natural_and_deterministic():
    b = avatar.blink_schedule(30, seed=3)
    assert b == avatar.blink_schedule(30, seed=3) and b[0] >= 1.2
    assert all(2.6 <= y - x <= 4.8 for x, y in zip(b, b[1:]))
    assert [avatar._blink_state(1.0 + d, [1.0]) for d in (0.0, 0.05, 0.1, 0.14, 0.2)] == [1, 2, 2, 1, 0]


def test_avatar_frames_animate():
    idle_a = avatar.frame(0.0, 0, 1, False)
    idle_b = avatar.frame(1.5, 0, 1, False)
    talk = avatar.frame(1.5, 0.9, 2, True)
    blink = avatar.frame(1.5, 0, 1, False, blink=2)
    hit = avatar.frame(1.5, 0, 1, False, impact=1.0)
    enter = avatar.frame(0.05, 0, 1, False, entrance=0.1)
    assert idle_a.size == (avatar.CANVAS, avatar.CANVAS)
    assert idle_a.tobytes() != idle_b.tobytes()                  # breathes/sways when idle
    for other in (talk, blink, hit):
        assert other.tobytes() != idle_b.tobytes()
    alpha_sum = lambda im: sum(i * n for i, n in enumerate(im.getchannel("A").histogram()))
    assert alpha_sum(enter) < alpha_sum(idle_a) * 0.5            # fading in
    assert alpha_sum(hit) > alpha_sum(idle_b)                    # pop + glow flash


@needs_ffmpeg
def test_voice_features_separate_round_and_bright_sounds(tmp_path):
    low, high = tmp_path / "low.wav", tmp_path / "high.wav"
    for f, hz in ((low, 200), (high, 3500)):
        subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={hz}:duration=1", str(f)], check=True)
    env_l, zcr_l = media.pcm_features(low)
    env_h, zcr_h = media.pcm_features(high)
    assert len(env_l) in (30, 31) and max(env_l) == pytest.approx(1.0)  # one value per video frame (+ partial tail)
    assert sum(zcr_h) / len(zcr_h) > 0.22 > 0.08 > sum(zcr_l) / len(zcr_l)


@needs_ffmpeg
def test_avatar_track_renders_with_alpha(tmp_path):
    out = tmp_path / "a.mov"
    n = 45
    avatar.render_avatar_video(out, 1.5, 30, [0.6] * n, [i > 15 for i in range(n)], FF, zcr=[0.3] * n, impacts=[1.0])
    info = subprocess.run([cs.settings.ffprobe_path, "-v", "error", "-show_entries", "stream=width,height,pix_fmt,nb_frames",
                           "-of", "json", str(out)], capture_output=True, text=True)
    st = json.loads(info.stdout)["streams"][0]
    assert (st["width"], st["height"]) == (avatar.CANVAS, avatar.CANVAS) and st["pix_fmt"] == "argb"


# ------------------------------------------------------------------ context-aware effects
from app.clipper import effects


def _curve(seconds, spikes=()):
    c = [-30.0] * int(seconds * effects.RATE)
    for t, lu in spikes:
        for k in range(3):
            c[int(t * effects.RATE) + k] = -30.0 + lu
    return c


def test_detect_impacts_rules():
    imps = effects.detect_impacts(_curve(30, [(0.5, 15), (5, 12), (6.5, 10), (12, 9), (20, 5)]), 30)
    assert [i.t for i in imps] == [5.0, 12.0]          # not in first second, 4 s apart, weak jump ignored
    imps = effects.detect_impacts(_curve(40, [(3, 9), (8, 10), (13, 11), (18, 12), (25, 13)]), 40)
    assert len(imps) == effects.MAX_IMPACTS and [i.t for i in imps] == [13.0, 18.0, 25.0]   # strongest kept


def test_classify_uses_cuts_and_transcript():
    imps = [effects.Impact(5.0, 10), effects.Impact(10.0, 10), effects.Impact(15.0, 10), effects.Impact(20.0, 10)]
    words = [{"start": 9.9, "end": 10.2, "word": "What"}, {"start": 10.2, "end": 10.5, "word": "the"},
             {"start": 14.9, "end": 15.2, "word": "We"}, {"start": 15.2, "end": 15.4, "word": "got"}]
    out = effects.classify(imps, words, cuts=[20.3])
    assert [(i.t, i.kind) for i in out] == [(5.0, "impact"), (10.0, "emphasis"), (20.0, "transition")]


def test_timemap_and_slowmo_rules():
    tm = effects.TimeMap((10.0, 10.8))
    assert tm.out(5) == 5 and tm.out(10.4) == pytest.approx(10.8) and tm.out(12) == pytest.approx(12.8)
    assert tm.duration(20) == pytest.approx(20.8)
    strong = [effects.Impact(10.15, 12, "impact")]
    assert effects.plan(strong, 20, []).timemap.slow == (10.0, 10.8)
    assert effects.plan(strong, 20, [(9.5, 11)]).timemap.slow is None                  # never during the voice
    assert effects.plan([effects.Impact(10.15, 7.5, "impact")], 20, []).timemap.slow is None   # not strong enough
    assert effects.plan([effects.Impact(10.15, 12, "emphasis")], 20, []).timemap.slow is None  # shouts: no slow-mo
    assert effects.plan([effects.Impact(2.0, 12, "impact")], 20, []).timemap.slow is None      # too early
    assert effects.plan(strong, 20, [], allow_slowmo=False).timemap.slow is None


def _ev(expr, **vars):
    env = {"gte": lambda a, b: float(a >= b), "gt": lambda a, b: float(a > b), "lt": lambda a, b: float(a < b),
           "between": lambda x, a, b: float(a <= x <= b),
           "exp": __import__("math").exp, "sin": __import__("math").sin, "cos": __import__("math").cos,
           "max": max, "min": min, "pow": pow, **vars}
    return eval(expr, {"__builtins__": {}}, env)


def test_effect_expressions_are_context_aware():
    ep = effects.plan([effects.Impact(10.0, 9.5, "impact"), effects.Impact(4.0, 9, "emphasis"),
                       effects.Impact(16.0, 9, "transition")], 20, [(0, 3)], allow_slowmo=False)
    z = effects.zoom_expr(ep, 20, (17.0, 19.0))
    assert _ev(z, on=0) == pytest.approx(1.12)                       # whip-zoom intro
    assert _ev(z, on=60) == pytest.approx(1.0, abs=0.01)             # calm in between
    assert _ev(z, on=300) > _ev(z, on=120) + 0.1                     # big punch on the impact (+ build-up)
    # the punch itself is an instant cut-in on the hit frame (300); before and after it the zoom moves smoothly
    assert all(abs(_ev(z, on=f + 1) - _ev(z, on=f)) < 0.03 for f in list(range(250, 299)) + list(range(300, 340)))
    assert 1.04 < _ev(z, on=120) < 1.08                              # small punch on the shout
    assert _ev(z, on=480) == pytest.approx(1.0, abs=0.01)            # cut: no punch
    sx, sy = effects.shake_terms(ep)
    assert _ev(sx, on=300 + 2) != 0 and _ev(sx, on=120 + 2) == 0 and _ev(sx, on=480 + 2) == 0   # shake: impacts only
    b = effects.brightness_expr(ep)
    assert _ev(b, t=10.02) > 0.1 and _ev(b, t=4.02) == 0 and 0 < _ev(b, t=16.02) < _ev(b, t=10.02)
    cues = effects.sfx_cues(ep, card_end=2.5)
    assert ("boom", pytest.approx(9.98), 0.9) in cues and not any(n == "boom" and abs(t - 4) < 0.1 for n, t, _ in cues)
    assert any(n == "whoosh" and abs(t - 15.7) < 0.01 for n, t, _ in cues)   # cut gets a whoosh


@needs_ffmpeg
def test_sfx_are_generated_and_audible(tmp_path):
    paths = effects.ensure_sfx(FF, tmp_path)
    for name, p in paths.items():
        r = subprocess.run([FF, "-hide_banner", "-i", str(p), "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True)
        peak = float(re.search(r"max_volume: (-?[\d.]+) dB", r.stderr).group(1))
        assert -8 < peak <= 0, (name, peak)


@needs_ffmpeg
def test_render_with_slow_motion_keeps_everything_in_sync(clip_env, tmp_path, monkeypatch):
    src = make_source(tmp_path / "s.mp4", seconds=30)
    with db.session_scope() as s:
        p = ClipProject(name="t", source_path=str(src), permission_status="original", width=1280, height=720)
        s.add(p)
        s.flush()
        c = ClipCandidate(project_id=p.id, start_time=2.0, end_time=22.0, headline="Big crash", hook_line="Watch this.",
                          outro_line="Wow.")
        s.add(c)
        s.flush()
        pid, cid = p.id, c.id
    analysis.transcript_path(pid).write_text(json.dumps({"segments": [], "words": [
        {"start": 15.0, "end": 15.4, "word": "after"}]}), encoding="utf-8")
    monkeypatch.setattr(effects, "detect_impacts", lambda curve, d: [effects.Impact(10.0, 12.0)])
    from app.clipper import render

    out = render.render_preview(cid)
    meta = json.loads((out.parent / "effects.json").read_text())
    assert meta["slowmo"] == [9.85, 10.65] and meta["output_duration"] == pytest.approx(20.8)
    info = json.loads(subprocess.run([cs.settings.ffprobe_path, "-v", "error", "-show_entries", "stream=codec_type,duration",
                                      "-of", "json", str(out)], capture_output=True, text=True).stdout)
    for st in info["streams"]:
        assert float(st["duration"]) == pytest.approx(20.8, abs=0.1)
    ass = (out.parent / "subs.ass").read_text(encoding="utf-8")
    assert "0:00:13.80" in ass            # word at 13.0 s clip time is shifted +0.8 s by the slow motion
    with db.session_scope() as s:
        assert s.get(ClipCandidate, cid).status == CandidateStatus.PROPOSED   # preview leaves the moment untouched


# ------------------------------------------------------------------ smart caption placement
def test_caption_band_rules():
    moving = [[100.0] * framing.SCAN_W for _ in range(20)]                 # third-person chase: camera always moving
    flat_rows = [[1.0] * 90 for _ in range(20)]
    assert framing.band_is_busy(moving, flat_rows, (0.65, 0.75))            # lower band holds the player/car
    assert not framing.band_is_busy(moving, flat_rows, (0.30, 0.40))        # upper band is free
    still = [[1.0] * 70 + [300.0] * 20 + [1.0] * 70 for _ in range(20)]     # static camera, localized action
    action_high = [[50.0] * 60 + [5.0] * 30 for _ in range(20)]
    action_low = [[5.0] * 60 + [50.0] * 30 for _ in range(20)]
    assert not framing.band_is_busy(still, action_high, (0.70, 0.80))       # lower band free: captions can go low
    assert framing.band_is_busy(still, action_low, (0.70, 0.80))            # action is low: captions move up


def test_avatar_accepts_reaction_strengths():
    alpha = lambda im: sum(i * n for i, n in enumerate(im.getchannel("A").histogram()))
    big = avatar.frame(1.5, 0, 1, False, impact=1.0)
    small = avatar.frame(1.5, 0, 1, False, impact=0.55)
    assert alpha(big) > alpha(small) > alpha(avatar.frame(1.5, 0, 1, False))


@needs_ffmpeg
def test_voice_synth_processes_and_previews_cache(clip_env, monkeypatch):
    from app.clipper import voice

    calls = []
    real = ai.tts_hook
    monkeypatch.setattr(ai, "tts_hook", lambda text, out: (calls.append(text), real(text, out))[1])
    p1 = voice.preview("cedar")
    p2 = voice.preview("cedar")
    assert p1 == p2 and len(calls) == 1                     # generated once, then cached
    info = json.loads(subprocess.run([cs.settings.ffprobe_path, "-v", "error", "-show_entries", "stream=sample_rate,channels",
                                      "-of", "json", str(p1)], capture_output=True, text=True).stdout)["streams"][0]
    assert info["sample_rate"] == "48000" and info["channels"] == 2
    # the slight speed-up shortens the line (pitch is kept by atempo)
    assert media.media_duration(p1) == pytest.approx(1.5 / cs.VOICE_TEMPO, abs=0.06)
    with pytest.raises(ValueError):
        voice.preview("not-a-voice")


@needs_ffmpeg
def test_voice_api(client):
    r = client.get("/api/clipper/voices/marin/preview")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert client.get("/api/clipper/voices/nope/preview").status_code == 404
    cfg = client.get("/api/clipper/config").json()
    assert cfg["default_voice"] == "cedar" and cfg["voices"][:2] == ["cedar", "marin"]
    pid = None
    with db.session_scope() as s:
        p = ClipProject(name="v", source_path="x")
        s.add(p)
        s.flush()
        pid = p.id
    assert client.patch(f"/api/clipper/projects/{pid}", json={"voice": "marin"}, headers=H).json()["voice"] == "marin"
    assert client.patch(f"/api/clipper/projects/{pid}", json={"voice": "nope"}, headers=H).status_code == 400
