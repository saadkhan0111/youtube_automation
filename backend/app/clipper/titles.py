"""YouTube metadata for quick-mode clips, generated during the render when you didn't type a title.

Quick mode skips the full-video transcript and AI headlines. At render time this looks at the finished clip itself:
3 frames + what is said in that clip only (Whisper on just those seconds, a fraction of a cent) -> title,
description, tags and hashtags. If the AI isn't available or fails, the render falls back to the plain metadata.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from ..services.metadata_rules import clean_hashtags, clean_tags, clean_title
from . import ai
from . import settings as cs

log = logging.getLogger("ytshorts.clipper.titles")

# CLIP_TITLE_LANGUAGE=en (default) or hinglish (Hindi/Urdu in Roman letters)
LANGS = {"en": "English", "hinglish": "Hinglish (Hindi/Urdu written in Roman letters, the way Indian/Pakistani creators title Shorts)"}
LANGUAGE = os.environ.get("CLIP_TITLE_LANGUAGE", "en").strip().lower()

SYSTEM = """You write YouTube metadata for a Short cut from a gaming stream or gameplay video. You see frames from the
clip and get what is said in it.
- title: max 60 characters, punchy, curiosity or reaction driven, like a top gaming Shorts channel. Describe what
  actually happens or is said in THIS clip; never invent events, names or numbers you can't see or hear. No clickbait
  lies ("gone wrong", "you won't believe"), no ALL CAPS, at most one emoji, no hashtags.
- description: 1-2 short sentences for viewers about this moment (no links, no hashtags).
- tags: 6-10 search phrases people actually type (game name, streamer if known, what happens).
- hashtags: 3-4, no spaces (the app adds #Shorts itself).
- what_happens: one line describing the clip, for the reviewer."""

SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["title", "description", "tags", "hashtags", "what_happens"],
          "properties": {"title": {"type": "string"}, "description": {"type": "string"},
                         "tags": {"type": "array", "items": {"type": "string"}},
                         "hashtags": {"type": "array", "items": {"type": "string"}}, "what_happens": {"type": "string"}}}


def frames(clip: Path, duration: float) -> list[bytes]:
    out = []
    for f in (0.2, 0.5, 0.8):
        r = subprocess.run([cs.ffmpeg_path(), "-v", "error", "-ss", f"{duration * f:.2f}", "-i", str(clip), "-frames:v", "1",
                            "-vf", "scale=540:-2", "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "pipe:1"],
                           capture_output=True, timeout=60)
        if r.returncode == 0 and r.stdout:
            out.append(r.stdout)
    return out


def speech(clip: Path) -> str:
    """Transcribe only this clip."""
    with tempfile.TemporaryDirectory() as d:
        a = Path(d) / "clip.mp3"
        r = subprocess.run([cs.ffmpeg_path(), "-v", "error", "-i", str(clip), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(a)],
                           capture_output=True, timeout=120)
        if r.returncode != 0 or not a.exists():
            return ""
        try:
            return (ai.transcribe(a).get("text") or "").strip()
        except Exception as exc:  # noqa: BLE001 - metadata still works from the frames
            log.warning("clip transcription failed: %s", exc)
            return ""


def for_render(clip: Path, duration: float, project_name: str, topic: str | None, creator: str | None) -> dict | None:
    """Title/description/tags/hashtags from the rendered clip, or None if the AI isn't available or fails."""
    if not cs.openai_available():
        return None
    try:
        said = speech(clip)
        user = (f"Source video: {project_name}\n" + (f"What it is: {topic}\n" if topic else "")
                + (f"Streamer/creator: {creator}\n" if creator else "") + f"Clip length: {duration:.0f} s\n"
                + (f"Said in the clip (auto-transcribed, may be imperfect): {said[:1500]}\n" if said else "No clear speech in the clip.\n")
                + f"Write everything in {LANGS.get(LANGUAGE, LANGS['en'])}.")
        res = ai.generate_json(SYSTEM, user, SCHEMA, name="clip_metadata", images=frames(clip, duration))
        title = clean_title((res.get("title") or "").strip().strip('"'))[:70]
        if not title:
            return None
        return {"title": title, "description": (res.get("description") or "").strip(),
                "tags": clean_tags(res.get("tags") or []), "hashtags": clean_hashtags(res.get("hashtags") or []),
                "what_happens": (res.get("what_happens") or "").strip(), "heard": said[:500]}
    except Exception as exc:  # noqa: BLE001 - never fail a render because of metadata
        log.warning("AI metadata for clip failed: %s", exc)
        return None
