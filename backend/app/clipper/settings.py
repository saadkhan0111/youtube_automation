"""Clipping-engine configuration (environment variables, see .env.example)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from ..config import BACKEND_DIR, settings


def _p(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    p = Path(raw) if raw else default
    return (p if p.is_absolute() else BACKEND_DIR / p).resolve()


OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5")
OPENAI_TRANSCRIBE_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1")  # supports word timestamps
OPENAI_TTS_MODEL = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
OPENAI_TTS_VOICE = os.environ.get("OPENAI_TTS_VOICE", "cedar")
_DEFAULT_DIRECTION = " ".join([
    "Persona: a hyped, charismatic gaming YouTuber reacting live to a wild GTA clip, talking to his friends.",
    "Energy: high and genuinely excited, like you can't believe what just happened. Never flat, never like reading.",
    "Pacing: fast and punchy; speed up through the setup, then a tiny dramatic pause right before the payoff word.",
    "Emphasis: hit the key words hard (the action, the surprise). Let the voice rise on questions.",
    "Tone: warm, playful, a little cheeky; a hint of a laugh where it fits. Conversational, natural breaths.",
    "Do not shout or distort; keep it clear and close to the mic.",
])
TTS_INSTRUCTIONS = os.environ.get("OPENAI_TTS_INSTRUCTIONS", _DEFAULT_DIRECTION)
VOICE_TEMPO = float(os.environ.get("VOICE_TEMPO", "1.06"))   # slight speed-up after synthesis (pitch kept)
AVATAR_PATH = _p("AVATAR_PATH", BACKEND_DIR / "assets" / "avatar.png")
VIDEO_ENCODER = os.environ.get("VIDEO_ENCODER", "auto")  # auto | libx264 | h264_qsv
WHISPER_COST_PER_MIN = 0.006

# marin and cedar are OpenAI's newest, most natural voices
TTS_VOICES = ["cedar", "marin", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse", "alloy"]
SOURCE_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".flv", ".ts", ".m4v", ".avi"}


def projects_dir() -> Path:
    d = settings.data_dir / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sources_dir() -> Path:
    d = settings.data_dir / "sources"
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_dir(project_id: int) -> Path:
    d = projects_dir() / str(project_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def ffmpeg_path() -> str:
    """ffmpeg next to the configured ffprobe, else from PATH."""
    probe = Path(settings.ffprobe_path)
    if probe.is_file():
        cand = probe.with_name("ffmpeg.exe" if probe.suffix.lower() == ".exe" else "ffmpeg")
        if cand.is_file():
            return str(cand)
    found = shutil.which("ffmpeg")
    if not found:
        raise RuntimeError("ffmpeg not found. Install FFmpeg or set FFPROBE_PATH to the ffprobe inside the FFmpeg bin folder.")
    return found


def openai_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))
