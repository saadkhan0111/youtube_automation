"""Rendering stage: turn the approved storyboard + voiceover + assets into a finished video.

Pipeline (all FFmpeg, nothing leaves the machine):
  1. timeline.build()            exact frame timing for every scene and shot
  2. one segment per shot        cover-crop, camera move (zoompan), per-shot effects; cached by content hash,
                                 so a re-render after small edits only redoes the shots that changed
  3. audio mix                   narration at its scene times, music bed ducked under the voice, sound effects,
                                 loudness-normalised to -14 LUFS (YouTube's playback level)
  4. final pass                  transitions (xfade), optional avatar presenter, captions + graphics (ASS), encode
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from pathlib import Path

from ..clipper import avatar as clip_avatar
from ..clipper import effects as clip_effects
from ..clipper import media
from ..clipper import settings as clip_settings
from ..db import session_scope
from ..services.validation import validate_video
from . import music, timeline, worker
from . import settings as ss
from .models import StudioAsset, StudioProject, StudioRender

log = logging.getLogger("ytshorts.studio.editor")

FPS = timeline.FPS
SEGMENT_VERSION = 4            # bump to invalidate cached segments when the look changes
MUSIC_LUFS = -27               # music bed level before ducking (narration is -16)

# camera move -> (zoom start, zoom end, x start, x end, y start, y end); x/y = position of the window in the slack 0..1
CAMERA = {
    "static": (1.03, 1.03, .5, .5, .5, .5), "slow_push_in": (1.0, 1.12, .5, .5, .5, .5),
    "slow_pull_out": (1.12, 1.0, .5, .5, .5, .5), "pan_left": (1.12, 1.12, 1, 0, .5, .5),
    "pan_right": (1.12, 1.12, 0, 1, .5, .5), "tilt_up": (1.12, 1.12, .5, .5, 1, 0), "tilt_down": (1.12, 1.12, .5, .5, 0, 1),
    "zoom_punch": (1.0, 1.2, .5, .5, .5, .5), "parallax": (1.06, 1.14, .3, .7, .5, .5),
    "handheld_drift": (1.08, 1.08, .5, .5, .5, .5),
}

# extra synthesized sound effects (the clipper already provides whoosh, boom, pop)
EXTRA_SFX = {
    "impact": "sine=f=70:d=0.5,volume=1.4,afade=t=out:d=0.5:curve=exp[s];anoisesrc=color=white:d=0.08:a=0.8,"
              "highpass=f=1500,afade=t=out:d=0.08[n];[s][n]amix=inputs=2:normalize=0,volume=2,alimiter=limit=0.85",
    "riser": "anoisesrc=color=pink:d=1.3:a=0.7,highpass=f=300,lowpass=f=6000,afade=t=in:d=1.2:curve=qsin,"
             "afade=t=out:st=1.2:d=0.1,volume=4,alimiter=limit=0.6",
    "ding": "sine=f=1318:d=0.9,afade=t=out:d=0.9:curve=exp,volume=3[a];sine=f=2637:d=0.5,afade=t=out:d=0.5:curve=exp,"
            "volume=1[b];[a][b]amix=inputs=2:normalize=0,alimiter=limit=0.6",
    "swoosh_soft": "anoisesrc=color=pink:d=0.6:a=0.6,highpass=f=500,lowpass=f=2500,afade=t=in:d=0.3:curve=qsin,"
                   "afade=t=out:st=0.3:d=0.3:curve=qsin,volume=5,alimiter=limit=0.5",
    "glitch": "anoisesrc=color=white:d=0.3:a=0.6,highpass=f=2000,tremolo=f=28:d=1,acrusher=bits=4:mode=log,"
              "afade=t=out:st=0.2:d=0.1,volume=1.5,alimiter=limit=0.6",
    "click": "sine=f=2000:d=0.025,afade=t=out:d=0.025,volume=6,alimiter=limit=0.6",
}


class RenderError(RuntimeError):
    pass


def ff() -> str:
    return clip_settings.ffmpeg_path()


def dims(p: StudioProject) -> tuple[int, int]:
    return (1080, 1920) if p.video_format == "short" else (1920, 1080)


def _run(cmd: list[str], cwd: Path, log_name: str, timeout: int = 900) -> None:
    log_path = cwd / log_name
    with log_path.open("w", encoding="utf-8") as errf:
        rc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=errf, timeout=timeout).returncode
    if rc != 0:
        raise RenderError(f"ffmpeg failed ({log_name}): {log_path.read_text(encoding='utf-8', errors='replace')[-1200:]}")


# --------------------------------------------------------------------------- sound effects
def ensure_sfx(out_dir: Path) -> dict[str, Path]:
    paths = clip_effects.ensure_sfx(ff(), out_dir)
    for name, graph in EXTRA_SFX.items():
        p = out_dir / f"studio_{name}_v1.wav"
        if not p.exists():
            tmp = p.with_suffix(".tmp.wav")
            subprocess.run([ff(), "-y", "-v", "error", "-filter_complex", graph + ",aresample=48000,aformat=channel_layouts=stereo",
                            "-t", "1.6", str(tmp)], check=True)
            tmp.replace(p)
        paths[name] = p
    return paths


# --------------------------------------------------------------------------- one segment per shot
def _effects_chain(effects: list[str]) -> list[str]:
    out = []
    for e in effects:
        if e == "color_pop":
            out.append("eq=saturation=1.28:contrast=1.07")
        elif e == "vignette":
            out.append("vignette=angle=PI/4.6")
        elif e == "film_grain":
            out.append("noise=alls=9:allf=t+u")
        elif e == "chromatic_aberration":
            out.append("rgbashift=rh=-9:bh=9:enable='lt(t,0.35)'")
        elif e == "letterbox":
            out.append("drawbox=x=0:y=0:w=iw:h=ih*0.07:color=black:t=fill,drawbox=x=0:y=ih*0.93:w=iw:h=ih*0.07:color=black:t=fill")
        elif e == "light_leak":
            out.append("colorbalance=rm=0.07:gm=0.02:bm=-0.05:rh=0.05")
        elif e == "blur_in":
            out.append("boxblur=18:enable='lt(t,0.2)'")
    return out


def segment_plan(shot: timeline.Shot, neighbours: list[timeline.Shot], p: StudioProject, board: dict) -> dict:
    """Everything that determines a segment's pixels (also its cache key)."""
    W, H = dims(p)
    n = shot.render_frames
    sh = shot.shot
    graphic = sh.get("visual_type") == "graphic"
    src, kind, start = None, None, 0.0
    a = shot.asset if (shot.asset and shot.asset.get("status") == "ready" and shot.asset.get("has_file")) else None
    if graphic or a is None:
        # graphics sit on a blurred, darkened version of a neighbouring shot (or a gradient in the video's colours)
        nb = next((x.asset for x in neighbours if x.asset and x.asset.get("has_file") and x.asset.get("status") == "ready"), None)
        if nb:
            src, kind, start = nb["path"], nb["kind"], float(nb["meta"].get("start") or 0)
        mode = "backdrop"
    else:
        src, kind, start = a["path"], a["kind"], float(a["meta"].get("start") or 0)
        mode = "full"
    cam = sh.get("camera_movement", "static") if mode == "full" else "slow_push_in"
    effects = [e for e in sh.get("effects", []) if mode == "full"]
    return {"v": SEGMENT_VERSION, "W": W, "H": H, "frames": n, "src": src, "kind": kind, "start": start, "mode": mode,
            "camera": cam, "effects": effects, "shake": "shake" in sh.get("effects", []) and mode == "full",
            "mtime": Path(src).stat().st_mtime if src else None,
            "grade": board["overall"].get("color_grade", "") if not src else ""}


def render_segment(plan: dict, out: Path, work: Path) -> None:
    W, H, n = plan["W"], plan["H"], plan["frames"]
    z0, z1, x0, x1, y0, y1 = CAMERA.get(plan["camera"], CAMERA["static"])
    N = max(1, n - 1)
    if plan["camera"] == "zoom_punch":
        zexpr = f"if(lt(on,6),1+0.2*on/6,1.2-0.06*(on-6)/{N})"
    else:
        zexpr = f"{z0}+({z1 - z0})*on/{N}"
    if plan["camera"] == "handheld_drift":
        xf, yf = "(0.5+0.18*sin(on/23))", "(0.5+0.14*cos(on/29))"
    else:
        xf, yf = f"({x0}+({x1 - x0})*on/{N})", f"({y0}+({y1 - y0})*on/{N})"
    shake = "+if(lt(on,12),22*sin(on*2.3),0)" if plan["shake"] else ""
    shake_y = "+if(lt(on,12),18*cos(on*2.9),0)" if plan["shake"] else ""
    W2, H2 = W * 2, H * 2                                   # 2x oversampling keeps slow moves smooth
    zoom = (f"zoompan=z='{zexpr}':x='(iw-iw/zoom)*{xf}{shake}':y='(ih-ih/zoom)*{yf}{shake_y}'"
            f":d=1:s={W}x{H}:fps={FPS}")
    cover = f"scale={W2}:{H2}:force_original_aspect_ratio=increase:flags=lanczos,crop={W2}:{H2},setsar=1"
    if plan["src"] is None:
        inputs = ["-f", "lavfi", "-i", f"gradients=s={W}x{H}:c0=0x101820:c1=0x3a1450:c2=0x0d3b4f:x0=0:y0=0:x1={W}:y1={H}:speed=0.004:r={FPS}"]
        chain = ["setsar=1"]
    elif plan["kind"] == "image":
        inputs = ["-loop", "1", "-framerate", str(FPS), "-i", plan["src"]]
        chain = [cover, zoom]
    else:
        inputs = ["-stream_loop", "-1", "-ss", f"{plan['start']:.3f}", "-i", plan["src"]]
        chain = [f"fps={FPS}", cover, zoom]
    if plan["mode"] == "backdrop" and plan["src"] is not None:
        chain += ["gblur=sigma=16", "eq=brightness=-0.24:saturation=1.15", "vignette=angle=PI/3.5"]
    chain += _effects_chain(plan["effects"])
    chain += [f"fps={FPS}", "format=yuv420p", "setsar=1"]
    tmp = out.with_suffix(".tmp.mp4")
    cmd = [ff(), "-y", "-hide_banner", "-nostats", *inputs, "-vf", ",".join(chain), "-frames:v", str(n), "-an",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", "-r", str(FPS), str(tmp)]
    _run(cmd, work, "segment.log", timeout=600)
    tmp.replace(out)


# --------------------------------------------------------------------------- audio
def mix_audio(tl: timeline.Timeline, audio_dir: Path, track: dict | None, sfx: dict[str, Path], out: Path, work: Path) -> None:
    D = tl.duration
    inputs, fc, narr, extra = [], [], [], []
    idx = 0
    for sc in tl.scenes:
        if not sc.audio:
            continue
        inputs += ["-i", str(audio_dir / sc.audio)]
        ms = int(sc.narration_at * 1000)
        fc.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms}[n{idx}]")
        narr.append(f"[n{idx}]")
        idx += 1
    if not narr:
        raise RenderError("No narration audio. Run Voiceover again.")
    fc.append("".join(narr) + f"amix=inputs={len(narr)}:normalize=0:duration=longest,apad,atrim=0:{D:.3f},asplit=2[voice][key]")
    mixed = ["[voice]"]
    if track:
        inputs += ["-stream_loop", "-1", "-i", track["path"]]
        fc.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{D:.3f},asetpts=N/SR/TB,"
                  f"loudnorm=I={MUSIC_LUFS}:TP=-3:LRA=11,afade=t=in:d=0.6,afade=t=out:st={max(0.0, D - 1.8):.3f}:d=1.8[mus]")
        fc.append("[mus][key]sidechaincompress=threshold=0.02:ratio=6:attack=20:release=400:makeup=1[bed]")
        mixed.append("[bed]")
        idx += 1
    else:
        fc.append("[key]anullsink")
    for k, (name, at, vol) in enumerate(tl.sfx):
        if name not in sfx:
            continue
        inputs += ["-i", str(sfx[name])]
        ms = int(at * 1000)
        fc.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,volume={vol},adelay={ms}|{ms}[x{k}]")
        mixed.append(f"[x{k}]")
        idx += 1
    fc.append("".join(mixed) + f"amix=inputs={len(mixed)}:normalize=0:duration=first,"
              f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,atrim=0:{D:.3f}[a]")
    (work / "audio_filter.txt").write_text(";\n".join(fc), encoding="utf-8")
    _run([ff(), "-y", "-hide_banner", "-nostats", *inputs, "-/filter_complex", "audio_filter.txt", "-map", "[a]",
          "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", out.name], work, "audio.log")


# --------------------------------------------------------------------------- final pass
def video_filter(tl: timeline.Timeline, n_inputs: int, avatar: bool, W: int) -> str:
    fc = [f"[{i}:v]fps={FPS},format=yuv420p,setsar=1,settb=AVTB[s{i}]" for i in range(n_inputs)]
    shots = tl.shots
    cur = "[s0]"
    for i in range(1, len(shots)):
        xf, d = shots[i].trans_in
        if xf is None:
            fc.append(f"{cur}[s{i}]concat=n=2:v=1:a=0,settb=AVTB[c{i}]")
        else:
            offset = timeline.secs(shots[i].start - d // 2)
            fc.append(f"{cur}[s{i}]xfade=transition={xf}:duration={timeline.secs(d)}:offset={offset},settb=AVTB[c{i}]")
        cur = f"[c{i}]"
    if avatar:
        fc.append(f"[{n_inputs}:v]format=yuva420p[av];{cur}[av]overlay=x={22 if W < 1500 else 40}:y={110 if W < 1500 else 40}"
                  ":format=yuv420:eof_action=pass[ov]")
        cur = "[ov]"
    fc.append(f"{cur}subtitles=subtitles.ass:fontsdir=fonts,trim=end_frame={tl.total_frames},setpts=PTS-STARTPTS[v]")
    return ";\n".join(fc)


def _encoders() -> list[str]:
    from ..clipper import render as clip_render

    return clip_render._encoders()


def _encoder_args(name: str) -> list[str]:
    from ..clipper import render as clip_render

    return clip_render._encoder_args(name)


def _fonts(dest: Path) -> None:
    dest.mkdir(exist_ok=True)
    for name in ("seguibl.ttf", "seguisb.ttf", "segoeui.ttf", "ariblk.ttf", "seguisym.ttf"):
        src = Path("C:/Windows/Fonts") / name
        if src.exists() and not (dest / name).exists():
            shutil.copy2(src, dest / name)


# --------------------------------------------------------------------------- stage
def _inputs(project_id: int) -> tuple[StudioProject, dict, dict, dict]:
    with session_scope() as s:
        p = s.get(StudioProject, project_id)
        s.expunge(p)
        rows = s.query(StudioAsset).filter_by(project_id=project_id).all()
        assets = {}
        for a in rows:
            if a.scene_id is None:
                continue
            m = json.loads(a.meta_json or "{}")
            assets[(a.scene_id, a.shot_index)] = {"id": a.id, "status": a.status, "kind": a.kind, "origin": a.origin,
                                                  "has_file": bool(a.local_path and Path(a.local_path).is_file()),
                                                  "path": a.local_path, "meta": m}
    if not p.storyboard_json:
        raise RenderError("No storyboard.")
    voice = json.loads(p.voice_json or "{}").get("summary")
    if not voice:
        raise RenderError("No voiceover. Run Voiceover first.")
    return p, json.loads(p.storyboard_json), voice, assets


@worker.register("render")
def run(project_id: int, report) -> None:
    p, board, voice, assets = _inputs(project_id)
    W, H = dims(p)
    pdir = ss.project_dir(project_id)
    work = pdir / "render"
    seg_dir = work / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    tl = timeline.build(board, voice, assets, p.editing_style)
    style = timeline.caption_style_of(board, p.editing_style)
    warnings = []
    missing = [f"{s.scene_id} shot {s.index + 1}" for s in tl.shots
               if s.shot.get("visual_type") != "graphic" and not (s.asset and s.asset.get("has_file"))]
    if missing:
        raise RenderError("These shots have no visual: " + ", ".join(missing) + ". Fix them in Assets.")

    # 1. segments (cached)
    shots = tl.shots
    seg_files, reused = [], 0
    for i, sh in enumerate(shots):
        report(0.03 + 0.5 * i / len(shots), f"Building shot {i + 1}/{len(shots)}")
        same_scene = [x for x in shots if x.scene_id == sh.scene_id and x is not sh]
        others = sorted(shots, key=lambda x: abs(x.start - sh.start))
        plan = segment_plan(sh, same_scene + others, p, board)
        key = hashlib.sha1(json.dumps(plan, sort_keys=True).encode()).hexdigest()[:16]
        out = seg_dir / f"{i:03d}_{key}.mp4"
        if out.exists():
            reused += 1
        else:
            render_segment(plan, out, work)
        seg_files.append(out)
    keep = {f.name for f in seg_files}
    for f in seg_dir.glob("*.mp4"):
        if f.name not in keep:
            f.unlink(missing_ok=True)

    # 2. audio
    report(0.56, "Mixing narration, music and sound effects")
    mood = board["overall"].get("music_mood", "energetic")
    track = music.pick(mood, tl.duration, f"{project_id}-{mood}")
    if track is None:
        warnings.append(f"No background music: add tracks to backend/data/music/{mood}/ or set JAMENDO_CLIENT_ID in backend/.env.")
    sfx = ensure_sfx(ss.studio_dir() / "sfx")
    mix_audio(tl, pdir / "audio", track, sfx, work / "mix.wav", work)

    # 3. captions, graphics, avatar
    report(0.62, "Captions and graphics")
    use_avatar = bool(p.use_avatar)
    (work / "subtitles.ass").write_text(timeline.build_ass(tl, style, p.video_format, use_avatar), encoding="utf-8")
    _fonts(work / "fonts")
    if use_avatar:
        n = tl.total_frames + 1
        env, zcr, mask = [0.0] * n, [0.0] * n, [False] * n
        for sc in tl.scenes:
            e, z = media.pcm_features(pdir / "audio" / sc.audio, FPS)
            off = int(sc.narration_at * FPS)
            for k, (v, zc) in enumerate(zip(e, z)):
                if off + k < n:
                    env[off + k], zcr[off + k], mask[off + k] = v, zc, True
        clip_avatar.render_avatar_video(work / "avatar.mov", tl.duration, FPS, env, mask, ff(), zcr=zcr, seed=project_id)

    # 4. final encode
    report(0.68, "Rendering the final video")
    (work / "video_filter.txt").write_text(video_filter(tl, len(seg_files), use_avatar, W), encoding="utf-8")
    renders = pdir / "renders"
    renders.mkdir(exist_ok=True)
    with session_scope() as s:
        version = s.query(StudioRender).filter_by(project_id=project_id).count() + 1
        r = StudioRender(project_id=project_id, aspect="9:16" if W < H else "16:9", status="rendering")
        s.add(r)
        s.flush()
        rid = r.id
    out = renders / f"v{version}_{'9x16' if W < H else '16x9'}.mp4"
    inputs = []
    for f in seg_files:
        inputs += ["-i", str(f.relative_to(work))]
    if use_avatar:
        inputs += ["-i", "avatar.mov"]
    inputs += ["-i", "mix.wav"]
    audio_idx = len(seg_files) + (1 if use_avatar else 0)
    last = ""
    try:
        for enc in _encoders():
            cmd = [ff(), "-y", "-hide_banner", "-nostats", "-progress", "pipe:1", *inputs, "-/filter_complex", "video_filter.txt",
                   "-map", "[v]", "-map", f"{audio_idx}:a", *_encoder_args(enc), "-r", str(FPS), "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
                   "-t", f"{tl.duration:.3f}", str(out.resolve())]
            log_path = work / f"final_{enc}.log"
            with log_path.open("w", encoding="utf-8") as errf:
                proc = subprocess.Popen(cmd, cwd=work, stdout=subprocess.PIPE, stderr=errf, text=True, encoding="utf-8", errors="replace")
                for line in proc.stdout:
                    if line.startswith("out_time_us="):
                        try:
                            report(0.68 + 0.28 * min(1.0, int(line.split("=")[1]) / 1e6 / tl.duration), "Rendering the final video")
                        except ValueError:
                            pass
                rc = proc.wait()
            if rc == 0 and out.exists():
                break
            last = f"{enc}: {log_path.read_text(encoding='utf-8', errors='replace')[-1500:]}"
            log.warning("studio encoder %s failed: %s", enc, last[-300:])
        else:
            raise RenderError(f"Final render failed: {last}")
        vres = validate_video(out, check_stable=False)
        if not vres.ok:
            raise RenderError("Rendered video failed validation: " + "; ".join(vres.errors))
        poster = renders / f"v{version}_poster.jpg"
        subprocess.run([ff(), "-y", "-v", "error", "-ss", "1.0", "-i", str(out), "-frames:v", "1", "-vf", "scale=360:-2", str(poster)],
                       check=False)
        info = media.probe(out)
    except Exception as exc:
        with session_scope() as s:
            r = s.get(StudioRender, rid)
            r.status, r.error = "failed", str(exc)[:2000]
        raise
    summary = {
        "render_id": rid, "file": str(out), "poster": str(poster) if poster.exists() else None, "version": version,
        "duration": round(info["duration"], 2), "width": info["width"], "height": info["height"],
        "size_mb": round(out.stat().st_size / 1e6, 1), "shots": len(shots), "segments_reused": reused,
        "caption_style": style, "music": track, "avatar": use_avatar, "warnings": warnings,
        "transitions": sorted({s.trans_in[0] or "cut" for s in shots[1:]}),
        "sfx": len(tl.sfx),
    }
    with session_scope() as s:
        r = s.get(StudioRender, rid)
        r.status, r.progress, r.file_path = "done", 1.0, str(out)
        r.qa_json = json.dumps({"summary": summary})
        pr = s.get(StudioProject, project_id)
        outputs = json.loads(pr.outputs_json or "{}")
        outputs["render"] = summary
        pr.outputs_json = json.dumps(outputs)
