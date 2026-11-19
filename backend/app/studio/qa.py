"""Quality check stage: inspects the latest render before it can be published.

Three groups of checks, each result: pass | info | warn | fail (fail blocks approval), with a pointer to the stage
that fixes it.
  technical  FFmpeg measurements of the actual file: format, length, loudness, peaks, silences, black/frozen frames
  content    script/storyboard/voice warnings still open, uncertain facts, credits, music
  visual     one frame per shot from the render, reviewed by a vision model for misspelled or unrequested text,
             real-person likeness, logos/watermarks, caption problems, glitches and AI images that look like real photos
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from ..clipper import media
from ..clipper import settings as clip_settings
from ..config import settings as app_settings
from ..db import session_scope
from . import llm, script, timeline, worker
from . import settings as ss
from .models import StudioAsset, StudioProject, StudioRender

log = logging.getLogger("ytshorts.studio.qa")

SHORTS_MAX = 180.0
LOUDNESS_TARGET = -14.0
FRAMES_PER_CALL = 10
PROBLEM_TYPES = ["misspelled_text", "unrequested_text", "real_person_likeness", "logo_or_watermark", "caption_problem",
                 "offensive_or_graphic", "visual_glitch", "misleading_realism"]


class QAError(RuntimeError):
    pass


def ff() -> str:
    return clip_settings.ffmpeg_path()


def _check(cid: str, group: str, label: str, status: str, detail: str = "", fix: str | None = None, **extra) -> dict:
    return {"id": cid, "group": group, "label": label, "status": status, "detail": detail, "fix": fix, **extra}


# --------------------------------------------------------------------------- technical
def measure(path: Path) -> dict:
    """One FFmpeg pass: loudness (EBU R128 + true peak), silences, black and frozen stretches."""
    cmd = [ff(), "-hide_banner", "-nostats", "-v", "verbose", "-i", str(path),
           "-filter_complex", "[0:a]ebur128=peak=true,silencedetect=noise=-45dB:d=1.2[a];"
                              "[0:v]blackdetect=d=0.4:pix_th=0.08,freezedetect=n=0.002:d=2.5[v]",
           "-map", "[a]", "-map", "[v]", "-f", "null", "-"]
    err = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900).stderr
    summary = err[err.rfind("Summary:"):] if "Summary:" in err else ""
    num = lambda pat, src: float(m.group(1)) if (m := re.search(pat, src)) else None  # noqa: E731
    silences = [(float(a), float(b)) for a, b in re.findall(r"silence_start: ([\d.]+)[\s\S]*?silence_end: ([\d.]+)", err)]
    blacks = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", err)]
    fstarts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", err)]
    fends = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", err)]
    freezes = list(zip(fstarts, fends + [None] * (len(fstarts) - len(fends))))
    return {"integrated": num(r"I:\s+(-?[\d.]+) LUFS", summary), "lra": num(r"LRA:\s+([\d.]+) LU", summary),
            "true_peak": num(r"Peak:\s+(-?[\d.]+) dBFS", summary[summary.find("True peak"):]) if "True peak" in summary else None,
            "silences": silences, "blacks": blacks, "freezes": freezes}


def streams(path: Path) -> dict:
    out = subprocess.run([app_settings.ffprobe_path, "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,sample_rate,channels,duration",
                          "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, timeout=60).stdout
    data = json.loads(out or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    return {"video": v, "audio": a, "duration": float(data.get("format", {}).get("duration") or 0)}


def _where(t: float, tl: timeline.Timeline) -> str:
    for sc_i, sc in enumerate(tl.scenes, 1):
        for sh in sc.shots:
            if timeline.secs(sh.start) <= t < timeline.secs(sh.end):
                return f"scene {sc_i}, shot {sh.index + 1}"
    return "the end"


def technical_checks(p: StudioProject, path: Path, tl: timeline.Timeline) -> list[dict]:
    out = []
    s = streams(path)
    v, a = s["video"], s["audio"]
    W, H = (1080, 1920) if p.video_format == "short" else (1920, 1080)
    fps = v.get("r_frame_rate", "0/1")
    ok_fmt = (v.get("codec_name") == "h264" and v.get("pix_fmt") == "yuv420p" and (v.get("width"), v.get("height")) == (W, H)
              and fps in ("30/1", "30000/1000") and a.get("codec_name") == "aac" and a.get("channels") == 2)
    out.append(_check("format", "technical", "Video format", "pass" if ok_fmt else "fail",
                      f"{v.get('codec_name')} {v.get('width')}×{v.get('height')} {v.get('pix_fmt')} @ {fps} fps, "
                      f"audio {a.get('codec_name')} {a.get('channels')} ch {a.get('sample_rate')} Hz", None if ok_fmt else "render"))
    D = s["duration"]
    if p.video_format == "short" and D > SHORTS_MAX:
        out.append(_check("length", "technical", "Shorts length", "fail", f"{D:.1f}s is over YouTube's {SHORTS_MAX:.0f}s Shorts limit.", "script"))
    else:
        off = (D - p.target_duration) / p.target_duration
        st = "pass" if abs(off) <= 0.2 else "warn"
        out.append(_check("length", "technical", "Length vs target", st,
                          f"{D:.1f}s vs {p.target_duration}s target ({off:+.0%})."
                          + ("" if st == "pass" else " Shorten the script or raise the voice speed."), None if st == "pass" else "script"))
    vd, ad = float(v.get("duration") or D), float(a.get("duration") or D)
    out.append(_check("sync", "technical", "Audio and video length", "pass" if abs(vd - ad) <= 0.15 else "fail",
                      f"video {vd:.2f}s, audio {ad:.2f}s", None if abs(vd - ad) <= 0.15 else "render"))
    m = measure(path)
    I, tp = m["integrated"], m["true_peak"]
    if I is None:
        out.append(_check("loudness", "technical", "Loudness", "warn", "Couldn't measure loudness.", "render"))
    else:
        st = "pass" if abs(I - LOUDNESS_TARGET) <= 1.5 else "warn"
        out.append(_check("loudness", "technical", "Loudness", st,
                          f"{I:.1f} LUFS (YouTube plays at about {LOUDNESS_TARGET:.0f}){'' if st == 'pass' else '. Re-render.'}",
                          None if st == "pass" else "render", value=I))
    if tp is not None:
        st = "pass" if tp <= -0.5 else "warn"
        out.append(_check("peaks", "technical", "Peaks (clipping)", st, f"true peak {tp:.1f} dBTP", None if st == "pass" else "render", value=tp))
    gaps = [(a0, b0) for a0, b0 in m["silences"] if a0 > 0.3 and b0 < D - 1.0]
    out.append(_check("silence", "technical", "Dead air", "pass" if not gaps else "warn",
                      "No silent gaps." if not gaps else "Silence at " + ", ".join(f"{a0:.1f}–{b0:.1f}s ({_where(a0, tl)})" for a0, b0 in gaps[:5]),
                      None if not gaps else "voice"))
    out.append(_check("black", "technical", "Black frames", "pass" if not m["blacks"] else "warn",
                      "None." if not m["blacks"] else "Black at " + ", ".join(f"{a0:.1f}–{b0:.1f}s ({_where(a0, tl)})" for a0, b0 in m["blacks"][:5]),
                      None if not m["blacks"] else "assets"))
    frz = [(a0, b0 if b0 is not None else D) for a0, b0 in m["freezes"]]
    out.append(_check("frozen", "technical", "Frozen picture", "pass" if not frz else "warn",
                      "Always moving." if not frz else "Still for 2.5s+ at " + ", ".join(f"{a0:.1f}–{b0:.1f}s ({_where(a0, tl)})" for a0, b0 in frz[:5])
                      + ". Give these shots a camera move.", None if not frz else "storyboard"))
    first_word = min((w["start"] for sc in tl.scenes for w in sc.words), default=None)
    if first_word is not None:
        st = "pass" if first_word <= 0.6 else "warn"
        out.append(_check("hook_start", "technical", "Hook starts immediately", st, f"first word at {first_word:.2f}s", None if st == "pass" else "voice"))
    return out


# --------------------------------------------------------------------------- content
def content_checks(p: StudioProject, board: dict, voice: dict, render_summary: dict) -> list[dict]:
    out = []
    research = json.loads(p.research_json or "{}")
    scr = json.loads(p.script_json or "{}")
    if scr and research:
        warns = script.check(scr, p, research)
        hard = [w for w in warns if w["kind"] in ("facts", "number")]
        soft = [w for w in warns if w["kind"] not in ("facts", "number", "length")]
        out.append(_check("script_facts", "content", "Every claim is sourced", "pass" if not hard else "fail",
                          "All points cite research facts; every number is in a cited fact." if not hard else
                          "; ".join(f"scene {w['scene'] + 1 if w['scene'] is not None else '-'}: {w['message']}" for w in hard[:5]), None if not hard else "script"))
        if soft:
            out.append(_check("script_style", "content", "Script style", "warn",
                              "; ".join(f"scene {w['scene'] + 1 if w['scene'] is not None else '-'}: {w['message']}" for w in soft[:5]), "script"))
        facts = {f["id"]: f for f in research.get("facts", [])}
        shaky = sorted({fid for sc in scr.get("scenes", []) for fid in sc.get("fact_ids", [])
                        if fid in facts and facts[fid].get("status") != "verified"})
        out.append(_check("uncertain", "content", "Uncertain claims", "pass" if not shaky else "warn",
                          "Only verified facts are used." if not shaky else
                          "Uses " + ", ".join(f"{fid} ({facts[fid].get('status')})" for fid in shaky)
                          + ". Make sure the narration says these are rumours/opinions.", None if not shaky else "script"))
        n_src = len(research.get("sources", []))
        out.append(_check("sources", "content", "Sources for the description", "pass" if n_src else "warn",
                          f"{n_src} sources will be listed in the description." if n_src else "No sources recorded.", None if n_src else "research"))
    open_board = [f"scene {i + 1}: {w}" for i, sc in enumerate(board["scenes"]) for w in sc.get("warnings", [])]
    out.append(_check("storyboard", "content", "Storyboard warnings", "pass" if not open_board else "warn",
                      "None open." if not open_board else "; ".join(open_board[:5]), None if not open_board else "storyboard"))
    weak = [f"scene {i + 1} ({x['match']:.0%})" for i, x in enumerate(voice.get("scenes", [])) if x.get("match", 1) < 0.85]
    out.append(_check("voice_match", "content", "Voice matches the script", "pass" if not weak else "warn",
                      "Every scene was heard as written." if not weak else "Listen to " + ", ".join(weak) + " and re-record if wrong.",
                      None if not weak else "voice"))
    mus = render_summary.get("music")
    out.append(_check("music", "content", "Music licence", "pass" if mus else "info",
                      (f"{mus['title']} ({mus['source']}, {mus['license'].split(':')[0]})" if mus else "No music in this render."),
                      None if mus else "render"))
    with session_scope() as s:
        ai = s.query(StudioAsset).filter_by(project_id=p.id, origin="ai", status="ready").count()
        stock = s.query(StudioAsset).filter(StudioAsset.project_id == p.id, StudioAsset.origin.in_(("pexels", "pixabay")),
                                            StudioAsset.status == "ready").count()
    if ai:
        out.append(_check("ai_disclosure", "content", "AI imagery", "info",
                          f"{ai} AI illustrations, all stylized and never presented as real footage. YouTube's 'altered or synthetic "
                          "content' label is only required for realistic-looking content; tick it if any image could pass as real."))
    if stock:
        out.append(_check("credits", "content", "Stock credits", "pass", f"{stock} stock credits saved for the description."))
    return out


# --------------------------------------------------------------------------- visual (vision model)
VISION_SYSTEM = """You are the final quality reviewer of a YouTube video before it is published. You get frames from the
finished render, one per shot, with what each frame is supposed to contain. Report only real, visible problems:
- misspelled_text: on-screen text with spelling mistakes or garbled letters (common in AI images) - compare to the expected text.
- unrequested_text: readable text inside an AI illustration that was not asked for (signs, fake brand names). Generic, correctly
  spelled background text is low severity; text that changes the meaning or names a real brand is medium.
- real_person_likeness: a face that appears to depict a specific real, identifiable person.
- logo_or_watermark: real company logos, stock-site watermarks, channel logos.
- caption_problem: captions/graphics cut off, overlapping each other, unreadable on the background, or covering the key subject.
- offensive_or_graphic: gore, nudity, hateful symbols, anything inappropriate for a general audience.
- visual_glitch: broken rendering, smeared or melted shapes, extra limbs, stretched images, compression garbage.
- misleading_realism: an AI illustration that looks like a real photo of a real event or person.
Captions (big white words with one yellow word), the yellow scene label at the top and graphic text are drawn by our
editor from the exact expected text, so they are spelled correctly: never report them as misspelled. Only report them for
layout problems (cut off, overlapping, covering the subject). Check spelling only for text that is part of the picture itself.
Severity: high = must fix before publishing; medium = should fix; low = minor. Be concrete and brief. Do not invent problems."""


def _vision_schema(n: int) -> dict:
    prob = {"type": "object", "additionalProperties": False, "required": ["type", "severity", "detail"],
            "properties": {"type": {"type": "string", "enum": PROBLEM_TYPES},
                           "severity": {"type": "string", "enum": ["low", "medium", "high"]}, "detail": {"type": "string"}}}
    frame = {"type": "object", "additionalProperties": False, "required": ["frame", "problems"],
             "properties": {"frame": {"type": "integer", "minimum": 1, "maximum": n}, "problems": {"type": "array", "items": prob}}}
    return {"type": "object", "additionalProperties": False, "required": ["frames"],
            "properties": {"frames": {"type": "array", "items": frame}}}


def frame_samples(tl: timeline.Timeline, assets: dict) -> list[dict]:
    """The middle of every shot, with what should be visible there."""
    out = []
    for sc_i, sc in enumerate(tl.scenes, 1):
        for sh in sc.shots:
            t = round(timeline.secs((sh.start + sh.end) // 2), 2)
            said = " ".join(w["word"] for w in sc.words if abs(w["start"] - t) < 1.2)
            g = sh.shot.get("graphic") or {}
            a = assets.get((sh.scene_id, sh.index)) or {}
            out.append({"t": t, "scene": sc_i, "shot": sh.index + 1, "scene_id": sh.scene_id, "index": sh.index,
                        "visual": a.get("origin") or sh.shot.get("visual_type"), "description": sh.shot.get("visual_description", ""),
                        "graphic": f"{g.get('text', '')} / {g.get('subtext', '')}" if g.get("kind", "none") != "none" else "",
                        "kicker": sc.board.get("on_screen_text", ""), "caption_words": said})
    return out


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def about_our_text(detail: str, sample: dict) -> bool:
    """A spelling complaint whose quoted text is (part of) text our editor rendered (label, graphic, captions):
    that text comes verbatim from the storyboard, so the complaint is a misreading."""
    ours = _norm_text(" ".join([sample.get("kicker", ""), sample.get("graphic", ""), sample.get("caption_words", "")]))
    quotes = re.findall(r"[\"“”'‘’]([^\"“”‘’]{3,80})[\"“”'‘’]", detail)
    return any(_norm_text(q) and _norm_text(q) in ours for q in quotes)


def grab(path: Path, t: float, dest: Path) -> bytes:
    subprocess.run([ff(), "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                    "-vf", "scale=540:-2", "-q:v", "3", str(dest)], check=True, timeout=60)
    return dest.read_bytes()


def visual_checks(p: StudioProject, path: Path, tl: timeline.Timeline, assets: dict, qa_dir: Path, report) -> tuple[list[dict], list[dict]]:
    samples = frame_samples(tl, assets)
    frames_dir = qa_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for f in frames_dir.glob("*.jpg"):
        f.unlink()
    images = []
    for i, smp in enumerate(samples):
        name = f"{i + 1:03d}_{smp['scene_id']}_{smp['index']}.jpg"
        images.append(grab(path, smp["t"], frames_dir / name))
        smp["file"] = name
    findings = []
    for start in range(0, len(samples), FRAMES_PER_CALL):
        chunk = samples[start:start + FRAMES_PER_CALL]
        report(0.45 + 0.45 * start / len(samples), f"AI review of frames {start + 1}-{start + len(chunk)}")
        lines = [f"Frame {k + 1}: scene {c['scene']} shot {c['shot']} at {c['t']}s. Visual: {c['visual']} - {c['description']}. "
                 f"Expected graphic text: {c['graphic'] or 'none'}. Scene label: {c['kicker'] or 'none'}. "
                 f"Words being spoken (captions): {c['caption_words'] or 'none'}." for k, c in enumerate(chunk)]
        user = (f"Video topic: {p.topic}. Format: {'vertical Short' if p.video_format == 'short' else 'horizontal video'}.\n"
                + "\n".join(lines) + "\nReturn one entry per frame (empty problems if fine).")
        res = llm.chat_json(ss.QA_MODEL, VISION_SYSTEM, user, _vision_schema(len(chunk)), "qa_frames",
                            project_id=p.id, stage="qa", images=images[start:start + FRAMES_PER_CALL])
        for fr in res.get("frames", []):
            k = fr["frame"] - 1
            if 0 <= k < len(chunk):
                for prob in fr["problems"]:
                    if prob["type"] == "misspelled_text" and about_our_text(prob["detail"], chunk[k]):
                        log.info("dropped false spelling finding about our own overlay: %s", prob["detail"])
                        continue
                    findings.append({**prob, "frame": chunk[k]["file"], "t": chunk[k]["t"], "scene": chunk[k]["scene"],
                                     "shot": chunk[k]["shot"], "scene_id": chunk[k]["scene_id"], "index": chunk[k]["index"]})
    sev = {"high": "fail", "medium": "warn", "low": "info"}
    checks = []
    for ptype in PROBLEM_TYPES:
        hits = [f for f in findings if f["type"] == ptype]
        label = ptype.replace("_", " ").capitalize()
        if not hits:
            checks.append(_check(f"vis_{ptype}", "visual", label, "pass", "Nothing found."))
            continue
        worst = max(hits, key=lambda f: ["low", "medium", "high"].index(f["severity"]))["severity"]
        fix = "storyboard" if ptype == "caption_problem" else "assets"     # labels/graphics text is edited in the storyboard
        checks.append(_check(f"vis_{ptype}", "visual", label, sev[worst],
                             "; ".join(f"scene {h['scene']} shot {h['shot']} ({h['t']}s): {h['detail']}" for h in hits[:6]), fix,
                             frames=[h["frame"] for h in hits]))
    return checks, findings


# --------------------------------------------------------------------------- stage
def _latest_render(project_id: int) -> tuple[int, Path, dict]:
    with session_scope() as s:
        r = (s.query(StudioRender).filter_by(project_id=project_id, status="done").order_by(StudioRender.id.desc()).first())
        if r is None or not r.file_path or not Path(r.file_path).is_file():
            raise QAError("No finished render. Run Rendering first.")
        return r.id, Path(r.file_path), json.loads(r.qa_json or "{}").get("summary", {})


@worker.register("qa")
def run(project_id: int, report) -> None:
    from .editor import _inputs

    p, board, voice, assets = _inputs(project_id)
    rid, path, rsum = _latest_render(project_id)
    tl = timeline.build(board, voice, assets, p.editing_style)
    qa_dir = ss.project_dir(project_id) / "qa"
    qa_dir.mkdir(exist_ok=True)
    report(0.05, "Measuring the video (format, loudness, gaps, frozen frames)")
    checks = technical_checks(p, path, tl)
    report(0.35, "Checking script, facts and credits")
    checks += content_checks(p, board, voice, rsum)
    findings = []
    if clip_settings.openai_available():
        vis, findings = visual_checks(p, path, tl, assets, qa_dir, report)
        checks += vis
    else:
        checks.append(_check("vis_skipped", "visual", "AI frame review", "warn", "Skipped: OPENAI_API_KEY is missing."))
    _save_result(project_id, {"render_id": rid, "checks": checks, "findings": findings, "model": ss.QA_MODEL})


def result(project_id: int) -> dict | None:
    with session_scope() as s:
        pr = s.get(StudioProject, project_id)
        rid = json.loads(pr.outputs_json or "{}").get("qa", {}).get("render_id")
        if rid is None:
            return None
        r = s.get(StudioRender, rid)
        return json.loads(r.qa_json or "{}").get("qa") if r else None


def blocking(project_id: int) -> list[str]:
    res = result(project_id)
    if res is None:
        return ["Run the quality check first."]
    return [f"{c['label']}: {c['detail']}" for c in res["checks"] if c["status"] == "fail"]


NOT_DISMISSIBLE = {"format", "sync", "length", "script_facts"}   # objective failures: fix them instead


def _save_result(project_id: int, res: dict) -> None:
    res["counts"] = {k: sum(1 for c in res["checks"] if c["status"] == k) for k in ("pass", "info", "warn", "fail", "dismissed")}
    res["passed"] = res["counts"]["fail"] == 0
    with session_scope() as s:
        r = s.get(StudioRender, res["render_id"])
        data = json.loads(r.qa_json or "{}")
        data["qa"] = res
        r.qa_json = json.dumps(data)
        pr = s.get(StudioProject, project_id)
        outputs = json.loads(pr.outputs_json or "{}")
        outputs["qa"] = {"render_id": res["render_id"], "counts": res["counts"], "passed": res["passed"]}
        pr.outputs_json = json.dumps(outputs)


def dismiss(project_id: int, check_id: str, note: str, undo: bool = False) -> dict:
    """The creator overrules a finding (e.g. the AI reviewer misread something). Recorded with their note."""
    res = result(project_id)
    if res is None:
        raise QAError("Run the quality check first.")
    c = next((x for x in res["checks"] if x["id"] == check_id), None)
    if c is None:
        raise QAError("Check not found")
    if undo:
        if c["status"] == "dismissed":
            c["status"] = c.pop("status_before")
            c.pop("dismissed_note", None)
    else:
        if c["id"] in NOT_DISMISSIBLE and c["status"] == "fail":
            raise QAError("This is an objective failure and can't be dismissed. Use the Fix button.")
        if c["status"] in ("warn", "fail", "info"):
            c["status_before"], c["status"], c["dismissed_note"] = c["status"], "dismissed", note.strip()[:300] or "Checked by the creator"
    _save_result(project_id, res)
    return res
