"""Project analysis: extract audio -> transcribe (Whisper) -> loudness -> AI moment detection."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..db import session_scope
from ..events import record_event
from ..models import CandidateStatus, ClipCandidate, ClipProject, ProjectStatus
from . import ai, media
from . import settings as cs

log = logging.getLogger("ytshorts.clipper.analysis")

CATEGORIES = ["funny", "discovery", "comparison", "reaction", "action", "mechanics", "story", "fail"]

MOMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_seconds": {"type": "number"},
                    "end_seconds": {"type": "number"},
                    "score": {"type": "number", "description": "1-10 how likely this works as a standalone Short"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "headline": {"type": "string", "description": "On-screen title, max 7 words, punchy, truthful"},
                    "hook_line": {"type": "string", "description": "Spoken by the creator's avatar at the start, max 14 words"},
                    "outro_line": {"type": "string", "description": "Spoken at the end: short reaction or question to viewers, max 14 words"},
                    "reason": {"type": "string", "description": "Why this moment works"},
                },
                "required": ["start_seconds", "end_seconds", "score", "category", "headline", "hook_line", "outro_line", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}

SYSTEM = """You are a short-form video editor for a GTA gaming YouTube channel (GTA 6, GTA 5 vs GTA 6, streamer reactions, funny moments, discoveries, mechanics).
You get a timestamped transcript of a long gameplay video or stream (it may contain the player's commentary and/or in-game dialogue) plus markers where the audio suddenly gets loud (action, explosions, yelling).

Find the moments that would make the best standalone YouTube Shorts:
- A clear beginning and payoff inside the window; a viewer with no context should get it.
- Prefer funny reactions, surprising events, chases/crashes/explosions, interesting mechanics or discoveries, strong dialogue lines.
- Start slightly before the setup, end right after the payoff. Do not cut mid-sentence.
- Respect the requested min/max duration. Moments must not overlap.
- Headline, hook and outro must be truthful to what happens in the transcript. Never invent events, features, release dates or quotes.
- hook_line: spoken by the channel's avatar in the first seconds, like a hyped creator talking to friends. Conversational,
  punchy, a little cheeky, with natural interjections ("Bro,", "Nah,", "Okay,") and an exclamation or question that creates
  curiosity. Max 14 words. (e.g. "Bro, watch how fast this boat heist goes completely wrong!")
- outro_line: a genuine reaction plus a question that invites comments, same energetic voice
  (e.g. "Nah, that was insane... would you have jumped? Tell me below!").
- Write in English. No hashtags."""


def _update(project_id: int, **fields) -> None:
    with session_scope() as s:
        p = s.get(ClipProject, project_id)
        for k, v in fields.items():
            setattr(p, k, v)


def transcript_path(project_id: int) -> Path:
    return cs.project_dir(project_id) / "transcript.json"


def load_transcript(project_id: int) -> dict | None:
    p = transcript_path(project_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _fmt(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def run_analysis(project_id: int) -> None:
    """Full analysis. Safe to re-run: audio chunks and chunk transcripts are cached on disk."""
    try:
        with session_scope() as s:
            p = s.get(ClipProject, project_id)
            source = Path(p.source_path)
            min_len, max_len, max_n, topic = p.min_clip_seconds, p.max_clip_seconds, p.max_candidates, p.topic_hint
        if not source.is_file():
            raise RuntimeError(f"Source video not found: {source}")
        info = media.probe(source)
        if not info["has_audio"]:
            raise RuntimeError("Source video has no audio track; moments are found from speech and sound.")
        _update(project_id, duration=info["duration"], width=info["width"], height=info["height"],
                status=ProjectStatus.EXTRACTING, progress=0.02, stage_message="Extracting audio", error=None)

        work = cs.project_dir(project_id)
        chunks = media.extract_audio_chunks(source, work / "audio", info["duration"],
                                            on_progress=lambda f: _update(project_id, progress=0.02 + 0.13 * f))

        # --- transcription (cached per chunk)
        _update(project_id, status=ProjectStatus.TRANSCRIBING, stage_message="Transcribing speech (OpenAI Whisper)")
        segments, words = [], []
        for i, (chunk, offset) in enumerate(chunks):
            cache = chunk.with_suffix(".json")
            if cache.exists():
                part = json.loads(cache.read_text(encoding="utf-8"))
            else:
                part = ai.transcribe(chunk)
                cache.write_text(json.dumps(part), encoding="utf-8")
            for sgm in part.get("segments", []):
                segments.append({"start": round(sgm["start"] + offset, 2), "end": round(sgm["end"] + offset, 2), "text": sgm["text"]})
            for w in part.get("words", []):
                words.append({"start": round(w["start"] + offset, 2), "end": round(w["end"] + offset, 2), "word": w["word"]})
            _update(project_id, progress=0.15 + 0.5 * (i + 1) / len(chunks),
                    stage_message=f"Transcribing speech ({i + 1}/{len(chunks)})")
        transcript = {"segments": segments, "words": words}
        transcript_path(project_id).write_text(json.dumps(transcript), encoding="utf-8")
        cost = round(info["duration"] / 60 * cs.WHISPER_COST_PER_MIN, 3)

        # --- loudness
        _update(project_id, status=ProjectStatus.ANALYZING, progress=0.68, stage_message="Measuring loud moments",
                est_cost_usd=cost)
        curve = media.loudness_curve(source)
        peaks = media.loud_peaks(curve)
        (work / "loudness.json").write_text(json.dumps({"curve": curve, "peaks": peaks}), encoding="utf-8")

        # --- AI moment detection
        _update(project_id, progress=0.75, stage_message=f"Finding the best moments ({cs.OPENAI_MODEL})")
        moments = find_moments(segments, peaks, info["duration"], min_len, max_len, max_n, topic,
                               on_progress=lambda f: _update(project_id, progress=0.75 + 0.23 * f))
        with session_scope() as s:
            p = s.get(ClipProject, project_id)
            # replace earlier proposals that were never rendered
            for c in list(p.candidates):
                if c.status in (CandidateStatus.PROPOSED, CandidateStatus.DISCARDED):
                    s.delete(c)
            for m in moments:
                s.add(ClipCandidate(project_id=project_id, start_time=m["start"], end_time=m["end"], score=m["score"],
                                    category=m["category"], headline=m["headline"][:120], hook_line=m["hook_line"],
                                    outro_line=m["outro_line"], reason=m["reason"], excerpt=m["excerpt"]))
            p.status = ProjectStatus.READY
            p.progress = 1.0
            p.stage_message = f"Found {len(moments)} moments"
        record_event("clipper", f"Project #{project_id}: found {len(moments)} candidate moments")
    except Exception as exc:
        log.exception("analysis failed")
        _update(project_id, status=ProjectStatus.FAILED, error=str(exc)[:2000], stage_message="Failed")
        record_event("clipper", f"Project #{project_id} analysis failed: {exc}", level="error")


def _windows(segments: list[dict], size: float = 25 * 60, overlap: float = 90) -> list[list[dict]]:
    if not segments:
        return []
    end = segments[-1]["end"]
    out, start = [], 0.0
    while start < end:
        w = [s for s in segments if s["end"] > start and s["start"] < start + size]
        if w:
            out.append(w)
        start += size - overlap
    return out


def _snap(start: float, end: float, segments: list[dict], duration: float) -> tuple[float, float]:
    """Snap to sentence boundaries: start at a segment start, end at a segment end."""
    starts = [s["start"] for s in segments if s["start"] <= start + 1.0 and s["start"] >= start - 4.0]
    ends = [s["end"] for s in segments if s["end"] >= end - 1.0 and s["end"] <= end + 4.0]
    ns = min(starts, key=lambda x: abs(x - start)) if starts else start
    ne = min(ends, key=lambda x: abs(x - end)) if ends else end
    ns = max(0.0, ns - 0.3)
    ne = min(duration, ne + 0.4)
    return round(ns, 2), round(ne, 2)


def find_moments(segments, peaks, duration, min_len, max_len, max_n, topic=None, on_progress=None) -> list[dict]:
    windows = _windows(segments)
    if not windows:
        raise RuntimeError("No speech or dialogue was found in the video, so no moments could be detected.")
    per_window = max(3, -(-max_n * 2 // len(windows)))
    found = []
    for wi, w in enumerate(windows):
        w_start, w_end = w[0]["start"], w[-1]["end"]
        lines = [f"[{_fmt(s['start'])}-{_fmt(s['end'])}] {s['text']}" for s in w]
        loud = [f"{_fmt(t)} (+{d} LU)" for t, d in peaks if w_start <= t <= w_end]
        user = (
            (f"Video topic: {topic}\n" if topic else "")
            + f"Find up to {per_window} moments between {_fmt(w_start)} and {_fmt(w_end)}. "
            + f"Each must be {min_len}-{max_len} seconds long. Use seconds from the start of the video for start_seconds/end_seconds.\n\n"
            + "Loud moments: " + (", ".join(loud) or "none") + "\n\nTranscript:\n" + "\n".join(lines)
        )
        result = ai.generate_json(SYSTEM, user, MOMENT_SCHEMA, name="moments")
        for m in result.get("moments", []):
            try:
                st, en = float(m["start_seconds"]), float(m["end_seconds"])
            except (TypeError, ValueError):
                continue
            if en <= st:
                continue
            st, en = _snap(st, en, segments, duration)
            length = en - st
            if length < min_len * 0.8:
                en = min(duration, st + min_len)
            if en - st > max_len * 1.1:
                en = st + max_len
            if en - st < 5:
                continue
            excerpt = " ".join(s["text"] for s in segments if s["end"] > st and s["start"] < en)[:1500]
            found.append({
                "start": st, "end": round(en, 2), "score": max(0.0, min(10.0, float(m.get("score", 5)))),
                "category": m.get("category") if m.get("category") in CATEGORIES else "action",
                "headline": m.get("headline", "").strip(), "hook_line": m.get("hook_line", "").strip(),
                "outro_line": m.get("outro_line", "").strip(), "reason": m.get("reason", "").strip(), "excerpt": excerpt,
            })
        if on_progress:
            on_progress((wi + 1) / len(windows))
    # de-duplicate overlapping moments (keep the higher score)
    found.sort(key=lambda m: m["score"], reverse=True)
    kept: list[dict] = []
    for m in found:
        overlap = any(min(m["end"], k["end"]) - max(m["start"], k["start"]) > 2.0 for k in kept)
        if not overlap:
            kept.append(m)
        if len(kept) >= max_n:
            break
    return sorted(kept, key=lambda m: m["start"])
