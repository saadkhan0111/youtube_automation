"""Render a chosen moment into a finished 1080x1920 Short and hand it to the publishing pipeline.

Edit:
  * smart vertical framing (framing.py): a tall gameplay panel that follows the action, on a dark
    blurred, vignetted background (or a wider panel when the action is spread out)
  * context-aware effects (effects.py): whip-zoom + flash + whoosh intro; on detected impacts only:
    punch zoom, camera shake, flash, boom and an avatar reaction; build-up push-in before the
    strongest impact, optional slow motion on a very strong one; outro push-in and fade
  * opening hook card with the headline (pops in, leaves after the spoken hook), then
    word-by-word animated captions (game audio + avatar voice)
  * AI voice (OpenAI TTS) hook at the start and outro at the end; game audio ducked while it speaks
  * animated round avatar (avatar.py) overlapping the panel's top-left corner like a facecam: breathes,
    sways, blinks, talks with the voice and reacts to the loudest impact
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from ..config import settings
from ..db import session_scope
from ..events import record_event
from ..models import CandidateStatus, ClipCandidate, ClipProject
from ..services import ingest
from ..services.validation import validate_video
from . import ai, avatar, captions, effects, framing, media, voice
from . import settings as cs
from .analysis import load_transcript

log = logging.getLogger("ytshorts.clipper.render")

FPS = 30
W, H = 1080, 1920
AVATAR_X = 22
CAPTION_LOWER_Y = 1230    # lower-middle third, above YouTube's bottom UI
HOOK_AT = 0.25


class _NoEffects(Exception):
    pass


def _update(cid: int, **fields) -> None:
    with session_scope() as s:
        c = s.get(ClipCandidate, cid)
        for k, v in fields.items():
            setattr(c, k, v)


def _encoder_args(name: str) -> list[str]:
    if name == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "21", "-look_ahead", "0"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high"]


def _encoders() -> list[str]:
    if cs.VIDEO_ENCODER in ("libx264", "h264_qsv"):
        return [cs.VIDEO_ENCODER] + (["libx264"] if cs.VIDEO_ENCODER != "libx264" else [])
    return ["h264_qsv", "libx264"]


def _copy_fonts(dest: Path) -> None:
    dest.mkdir(exist_ok=True)
    for name in ("seguibl.ttf", "ariblk.ttf", "impact.ttf"):
        src = Path("C:/Windows/Fonts") / name
        if src.exists():
            shutil.copy2(src, dest / name)


def render_candidate(cid: int) -> None:
    try:
        _render(cid)
    except Exception as exc:
        log.exception("render failed")
        _update(cid, status=CandidateStatus.FAILED, error=str(exc)[:2000])
        record_event("clipper", f"Render of moment #{cid} failed: {exc}", level="error")


def render_preview(cid: int) -> Path:
    """Render a moment WITHOUT adding it to the Videos library (used to tune the look).
    The candidate's status is restored afterwards. Returns the path of the rendered MP4."""
    with session_scope() as s:
        c = s.get(ClipCandidate, cid)
        saved = {"status": c.status, "render_progress": c.render_progress, "video_id": c.video_id, "error": c.error}
    try:
        return _render(cid, preview=True)
    finally:
        _update(cid, **saved)


def _render(cid: int, preview: bool = False) -> Path | None:
    with session_scope() as s:
        c = s.get(ClipCandidate, cid)
        p = s.get(ClipProject, c.project_id)
        start, end = c.start_time, c.end_time
        headline, hook, outro = (c.headline or "").strip(), (c.hook_line or "").strip(), (c.outro_line or "").strip()
        project = {k: getattr(p, k) for k in ("id", "name", "source_path", "source_url", "source_creator",
                                              "permission_status", "permission_notes", "topic_hint", "width", "height", "voice",
                                              "plain")}
        cat, reason, excerpt = c.category, c.reason, c.excerpt
    source = Path(project["source_path"])
    if not source.is_file():
        raise RuntimeError(f"Source video not found: {source}")
    D = round(end - start, 3)
    if D < 5:
        raise RuntimeError("Moment is shorter than 5 seconds.")
    if D > 180:
        raise RuntimeError("Moment is longer than 3 minutes (Shorts limit).")

    work = cs.project_dir(project["id"]) / ("previews" if preview else "renders") / str(cid)
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    ff = cs.ffmpeg_path()
    _update(cid, status=CandidateStatus.RENDERING, render_progress=0.02, error=None)

    # ---------- voice (hook now; the outro is placed once the output length is known)
    hook_voice, outro_voice = None, None   # (path, duration, text)
    plain = bool(project["plain"])        # quick mode: just the clip (+ your title card if you typed one)
    if plain:
        hook = outro = ""
    if hook:
        hp = voice.synth(hook, work / "hook.wav", project["voice"])
        hook_voice = (hp, media.media_duration(hp), hook)
    if outro:
        op = voice.synth(outro, work / "outro.wav", project["voice"])
        outro_voice = (op, media.media_duration(op), outro)
    _update(cid, render_progress=0.15)

    # ---------- effects plan: impacts in the clip decide where punches/shake/booms/slow motion go
    transcript = ({"words": []} if plain else load_transcript(project["id"])) or {"words": []}
    clip_words = [{"start": w["start"] - start, "end": w["end"] - start, "word": w["word"]}
                  for w in transcript.get("words", []) if w["end"] > start and w["start"] < end]
    try:
        if plain:
            raise _NoEffects
        # loud jumps, then the transcript separates real impacts (no speech) from shouts and dialogue
        raw = effects.detect_impacts(effects.clip_loudness(ff, source, start, D), D)
        impacts = effects.classify(raw, clip_words, effects.scene_cuts(ff, source, start, D) if raw else [])
    except _NoEffects:
        impacts = []
    except Exception as exc:
        log.warning("impact detection failed: %s", exc)
        impacts = []
    protected = []                                         # avatar voice windows (input time): no slow motion there
    if hook_voice:
        protected.append((HOOK_AT, HOOK_AT + hook_voice[1]))
    if outro_voice:
        protected.append((D - outro_voice[1] - 1.0, D))
    ep = effects.plan(impacts, D, protected, allow_slowmo=not plain)
    tm = ep.timemap
    DO = round(tm.duration(D), 3)                          # output duration (longer if slow motion)

    voices = []  # (path, at, duration, text) on the output timeline
    if hook_voice:
        voices.append((hook_voice[0], HOOK_AT, hook_voice[1], hook_voice[2]))
    if outro_voice:
        hook_end = HOOK_AT + hook_voice[1] if hook_voice else 0
        at = DO - outro_voice[1] - 0.6
        if at > hook_end + 2.0:
            voices.append((outro_voice[0], at, outro_voice[1], outro_voice[2]))
    (work / "effects.json").write_text(json.dumps({
        "impacts": [[i.t, i.strength, i.kind] for i in ep.impacts], "slowmo": tm.slow, "output_duration": DO}), encoding="utf-8")

    # ---------- avatar animation (output timeline)
    n = int(DO * FPS) + 1
    env, zcr, mask = [0.0] * n, [0.0] * n, [False] * n
    for path, at, dur, _ in voices:
        e, z = media.pcm_features(path, FPS)
        off = int(at * FPS)
        for i, (v, zc) in enumerate(zip(e, z)):
            if off + i < n:
                env[off + i], zcr[off + i], mask[off + i] = v, zc, True
    if not plain:
        avatar.render_avatar_video(work / "avatar.mov", DO, FPS, env, mask, ff, zcr=zcr,
                                   impacts=[(t, 1.0) for t in ep.impact_times_out()]
                                   + [(t, 0.55) for t in ep.emphasis_times_out()], seed=cid)
    _update(cid, render_progress=0.3)

    # ---------- framing (where the gameplay goes and how the crop follows the action)
    sw, sh = project["width"] or 1920, project["height"] or 1080
    try:
        motion_cols, motion_rows = framing.scan(ff, source, start, D, sw, sh)
        fr = framing.plan(motion_cols, sw, sh)
    except Exception as exc:  # never fail a render because of the analysis: fall back to a centred crop
        log.warning("framing analysis failed, using centred crop: %s", exc)
        motion_cols, motion_rows = [], []
        fr = framing.plan([], sw, sh)
    x_expr = fr.x_expr
    if tm.slow and fr.crop_w:                              # crop path on the output timeline
        x_expr = framing.path_expr([(tm.out(t), x) for t, x in fr.path], sw, fr.crop_w)
    (work / "framing.json").write_text(json.dumps({"layout": fr.layout, "score": fr.score, "crop_w": fr.crop_w,
                                                   "path": fr.path}), encoding="utf-8")
    log.info("framing: %s (score %.2f)", fr.layout, fr.score)
    _update(cid, render_progress=0.36)

    # ---------- layout + captions
    # The avatar overlaps the panel's top-left corner like a facecam (in full-screen it sits just below
    # YouTube's top UI). Captions go in the lower-middle third, where viewers look and above YouTube's
    # own title/channel UI, unless the motion analysis says the subject/action is there; then they move
    # to the upper-middle.
    full_frame = fr.panel_y == 0 and fr.panel_h == H
    avatar_y = 110 if full_frame else max(10, fr.panel_y - int(avatar.CANVAS * 0.36))
    caption_y = CAPTION_LOWER_Y
    band = ((caption_y - 60 - fr.panel_y) / fr.panel_h, (caption_y + 60 - fr.panel_y) / fr.panel_h)
    if framing.band_is_busy(motion_cols, motion_rows, band):
        caption_y = max(avatar_y + avatar.CANVAS + 120, fr.panel_y + int(fr.panel_h * 0.36))
    voice_windows = [(at, at + dur) for _, at, dur, _ in voices]
    game_words = []
    for w in transcript.get("words", []):
        if w["end"] <= start or w["start"] >= end:
            continue
        ws, we = tm.out(max(0.0, w["start"] - start)), tm.out(min(D, w["end"] - start))
        if any(a - 0.2 <= ws <= b + 0.2 for a, b in voice_windows):
            continue  # avatar voice captions take priority
        game_words.append({"start": ws, "end": we, "word": w["word"]})
    voice_words = []
    for _, at, dur, text in voices:
        voice_words += captions.spread_words(text, at, dur)
    # Opening hook card: on screen while the avatar speaks the hook, then it leaves for good.
    hook_end = HOOK_AT + hook_voice[1] if hook_voice else None
    card_end = captions.card_duration(hook_end)
    card_y = max(avatar.CANVAS + 240, fr.panel_y + int(fr.panel_h * 0.34))
    (work / "subs.ass").write_text(captions.build_ass(headline, DO, game_words, voice_words, caption_y,
                                                      card_end=card_end, card_y=card_y), encoding="utf-8")
    _copy_fonts(work / "fonts")

    # ---------- filter graph (everything after the retime step runs on the output timeline)
    outro_window = next(((at, at + dur) for path, at, dur, _ in voices if outro_voice and path == outro_voice[0]), None)
    zoom = "1" if plain else effects.zoom_expr(ep, DO, outro_window)
    shake_x, shake_y = ("0", "0") if plain else effects.shake_terms(ep)
    duck = "+".join(f"between(t,{a - 0.1:.2f},{b + 0.15:.2f})" for a, b in voice_windows) or "0"
    pw, ph, py = fr.panel_w, fr.panel_h, fr.panel_y
    if fr.layout == "fill":
        fg_chain = f"scale={pw}:{ph}:force_original_aspect_ratio=increase,crop={pw}:{ph}"
    else:
        fg_chain = f"crop={fr.crop_w}:{fr.crop_h}:x='{x_expr}':y=0,scale={pw}:{ph}:flags=lanczos"

    fc = []
    if tm.slow:
        a, b = tm.slow
        f = tm.factor
        # video: normal | half speed (motion-interpolated so it stays smooth) | normal
        fc.append(f"[0:v]setpts=PTS-STARTPTS,fps={FPS},split=3[p0][p1][p2]")
        fc.append(f"[p0]trim=0:{a:.3f},setpts=PTS-STARTPTS[q0]")
        fc.append(f"[p1]trim={a:.3f}:{b:.3f},setpts=(PTS-STARTPTS)*{f},"
                  f"minterpolate=fps={FPS}:mi_mode=mci:mc_mode=aobmc:vsbmc=1[q1]")
        fc.append(f"[p2]trim={b:.3f},setpts=PTS-STARTPTS[q2]")
        fc.append(f"[q0][q1][q2]concat=n=3:v=1:a=0,fps={FPS}[vin]")
        # audio: the slowed part is pitched down (deep, cinematic)
        fc.append("[0:a]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,asplit=3[r0][r1][r2]")
        fc.append(f"[r0]atrim=0:{a:.3f},asetpts=PTS-STARTPTS[t0]")
        fc.append(f"[r1]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS,asetrate={int(48000 / f)},aresample=48000[t1]")
        fc.append(f"[r2]atrim={b:.3f},asetpts=PTS-STARTPTS[t2]")
        fc.append("[t0][t1][t2]concat=n=3:v=0:a=1[ain]")
    else:
        fc.append(f"[0:v]setpts=PTS-STARTPTS,fps={FPS}[vin]")
        fc.append("[0:a]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[ain]")

    fc += [
        "[vin]split=2[s1][s2]",
        # background: darker and more stylised than before, so the gameplay panel reads as the hero
        f"[s1]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},gblur=sigma=10,"
        f"scale={W}:{H},eq=brightness=-0.30:saturation=1.30,vignette=angle=PI/3.2[bg]",
        # gameplay: smart crop, then zoom (intro whip, impact punches, build-up/outro push-ins) + impact shake
        f"[s2]{fg_chain},zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)+({shake_x})':y='ih/2-(ih/zoom/2)+({shake_y})'"
        f":d=1:s={pw}x{ph}:fps={FPS},unsharp=5:5:0.7:5:5:0,setsar=1[fg]",
        # soft shadow above/below the panel lifts it off the background
        (f"[bg]drawbox=x=0:y={py - 18}:w={W}:h=18:color=black@0.45:t=fill,"
         f"drawbox=x=0:y={py + ph}:w={W}:h=18:color=black@0.45:t=fill[bgs]") if not full_frame else "[bg]null[bgs]",
        f"[bgs][fg]overlay=0:{py}[b1]",
        # converting the avatar to yuva420p first keeps the overlay cheap (format=auto converts every frame)
        "[b1]null[b2]" if plain else "[1:v]format=yuva420p[avy]",
        "" if plain else f"[b1][avy]overlay={AVATAR_X}:{avatar_y}:format=yuv420[b2]",
        "[b2]subtitles=subs.ass:fontsdir=fonts[b3]",
        # flash-in at the start and a short flash on each impact; fade out at the end
        f"[b3]eq=brightness='{'0' if plain else effects.brightness_expr(ep)}':eval=frame,"
        f"fade=t=out:st={DO - 0.45:.2f}:d=0.45,format=yuv420p[v]",
        f"[ain]volume='if({duck},0.22,1)':eval=frame[ga]",
    ]
    mix_inputs = ["[ga]"]
    extra_inputs: list[Path] = []
    base = 1 if plain else 2                                # input 1 is the avatar track (not in quick mode)
    for path, at, _, _ in voices:
        idx = base + len(extra_inputs)
        extra_inputs.append(path)
        ms = int(at * 1000)
        fc.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms},volume=1.7[vo{idx}]")
        mix_inputs.append(f"[vo{idx}]")
    sfx = effects.ensure_sfx(ff, settings.data_dir / "sfx")
    for name, at, vol in ([] if plain else effects.sfx_cues(ep, card_end if headline else None)):
        idx = base + len(extra_inputs)
        extra_inputs.append(sfx[name])
        ms = int(at * 1000)
        fc.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms},volume={vol}[sx{idx}]")
        mix_inputs.append(f"[sx{idx}]")
    fc.append("".join(mix_inputs) + f"amix=inputs={len(mix_inputs)}:normalize=0:duration=first:dropout_transition=0,"
              f"afade=t=in:d=0.15,afade=t=out:st={DO - 0.45:.2f}:d=0.45,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]")
    filter_complex = ";\n".join(x for x in fc if x)
    (work / "filter.txt").write_text(filter_complex, encoding="utf-8")

    out = work / "short.mp4"
    inputs = ["-ss", f"{start:.3f}", "-t", f"{D:.3f}", "-i", str(source)] + ([] if plain else ["-i", "avatar.mov"])
    for path in extra_inputs:
        inputs += ["-i", path.name if path.parent == work else str(path)]

    last_err = ""
    for enc in _encoders():
        cmd = [ff, "-y", "-hide_banner", "-nostats", "-progress", "pipe:1", *inputs,
               "-/filter_complex", "filter.txt", "-map", "[v]", "-map", "[a]",
               *_encoder_args(enc), "-r", str(FPS), "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
               "-movflags", "+faststart", "-t", f"{DO:.3f}", out.name]
        log_path = work / f"ffmpeg_{enc}.log"
        with log_path.open("w", encoding="utf-8") as errf:  # file, not a pipe: avoids a stdout/stderr deadlock
            proc = subprocess.Popen(cmd, cwd=work, stdout=subprocess.PIPE, stderr=errf, text=True,
                                    encoding="utf-8", errors="replace")
            for line in proc.stdout:
                if line.startswith("out_time_us="):
                    try:
                        done = int(line.split("=")[1]) / 1e6
                        _update(cid, render_progress=round(0.3 + 0.62 * min(1.0, done / DO), 3))
                    except ValueError:
                        pass
            rc = proc.wait()
        err = log_path.read_text(encoding="utf-8", errors="replace")
        if rc == 0 and out.exists():
            break
        last_err = f"{enc}: {err.strip()[-1500:]}"
        log.warning("encoder %s failed, trying next: %s", enc, last_err[-300:])
    else:
        raise RuntimeError(f"ffmpeg render failed: {last_err}")

    vres = validate_video(out, check_stable=False)
    if not vres.ok:
        raise RuntimeError("Rendered Short failed validation: " + "; ".join(vres.errors))
    _update(cid, render_progress=0.95)
    if preview:
        return out

    # ---------- hand off to the publishing pipeline (pending review)
    sidecar = {
        "source_url": project["source_url"] or f"local file: {source.name}",
        "source_creator": project["source_creator"],
        "permission_status": project["permission_status"],
        "permission_notes": project["permission_notes"],
        "footage_type": "original" if project["permission_status"] == "original" else "third_party",
        "clip_start": start, "clip_end": end,
        "topic": project["topic_hint"] or "GTA",
        "context_notes": f"On-screen headline: {headline}. Why it works: {reason}",
        "transcript": excerpt,
        "original_commentary": " / ".join(v[3] for v in voices) or None,
    }
    if plain:
        # quick mode: no AI metadata. Title = your title card text (or the project name); the description credits
        # the creator and links the original stream.
        title = headline or f"{project['name']}"[:95]
        credit = f"Clip from {project['source_creator']}'s stream" if project["source_creator"] else "Clip"
        sidecar.update({"title": title, "hashtags": ["#Shorts"], "tags": [],
                        "description": f"{credit}: {project['source_url']}" if project["source_url"] else credit})
    final = work / f"short_p{project['id']}_m{cid}.mp4"
    out.rename(final)
    side = final.with_suffix(".json")
    side.write_text(json.dumps(sidecar), encoding="utf-8")
    try:
        video_id = ingest.import_clip(final, side, generate=True, check_stable=False)
    except ingest.DuplicateClip as exc:
        video_id = exc.existing_id
    _update(cid, status=CandidateStatus.RENDERED, render_progress=1.0, video_id=video_id)
    record_event("clipper", f"Rendered moment #{cid} → video #{video_id} (pending review)", video_id=video_id)
    # keep the work folder small: remove intermediates
    for f in work.glob("*"):
        if f.suffix in (".mov",):
            f.unlink(missing_ok=True)
