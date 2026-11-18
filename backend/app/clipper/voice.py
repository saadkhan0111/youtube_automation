"""AI voice: OpenAI TTS with energetic delivery direction, then studio-style processing.

The processing (applied once per line) makes the voice cut through game audio and sound less flat:
  high-pass (remove rumble) -> compressor (even, punchy level) -> presence boost around 3 kHz and a little
  warmth at 180 Hz -> slight speed-up (pitch preserved) -> limiter.
The same pipeline is used for the dashboard previews, so a preview sounds exactly like the final Short.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from ..config import settings
from . import ai
from . import settings as cs

PREVIEW_TEXT = ("Bro, watch this! This GTA chase goes completely off the rails... "
                "and I did NOT see that coming. Would you have made that jump?")

VOICE_FILTER = ("highpass=f=90,"
                "acompressor=threshold=-20dB:ratio=3:attack=4:release=80:makeup=3,"
                "equalizer=f=3200:t=q:w=1.2:g=3,equalizer=f=180:t=q:w=1:g=1.5,"
                "atempo={tempo},alimiter=limit=0.95")


def enhance(src: Path, dst: Path, tempo: float | None = None) -> None:
    tempo = tempo or cs.VOICE_TEMPO
    subprocess.run([cs.ffmpeg_path(), "-y", "-v", "error", "-i", str(src), "-af", VOICE_FILTER.format(tempo=tempo),
                    "-ar", "48000", "-ac", "2", str(dst)], check=True)


def synth(text: str, out_wav: Path, voice: str | None = None, instructions: str | None = None,
          tempo: float | None = None) -> Path:
    """Text -> processed WAV (48 kHz stereo)."""
    raw = out_wav.with_suffix(".raw.mp3")
    ai.tts(text, raw, voice, instructions) if instructions else ai.tts(text, raw, voice)
    enhance(raw, out_wav, tempo)
    raw.unlink(missing_ok=True)
    return out_wav


def preview(voice: str) -> Path:
    """Cached sample of a voice with the current direction and processing."""
    if voice not in cs.TTS_VOICES:
        raise ValueError("Unknown voice")
    key = hashlib.sha1(f"{voice}|{cs.OPENAI_TTS_MODEL}|{cs.TTS_INSTRUCTIONS}|{VOICE_FILTER}|{cs.VOICE_TEMPO}|{PREVIEW_TEXT}"
                       .encode()).hexdigest()[:12]
    d = settings.data_dir / "voice_previews"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{voice}_{key}.wav"
    if not out.exists():
        tmp = out.with_suffix(".tmp.wav")
        synth(PREVIEW_TEXT, tmp, voice)
        tmp.replace(out)
    return out
