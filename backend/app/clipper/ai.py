"""OpenAI calls used by the clipping engine: transcription, JSON generation, text-to-speech.

Every function can be replaced in tests (module-level hooks) so no real API is hit.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import settings as cs

log = logging.getLogger("ytshorts.clipper.ai")

# Test hooks
transcribe_hook = None   # fn(path) -> {"segments": [...], "words": [...], "text": str}
json_hook = None         # fn(system, user, schema) -> dict
tts_hook = None          # fn(text, out_path) -> None

_client = None


def client():
    global _client
    if _client is None:
        from openai import OpenAI

        if not cs.openai_available():
            raise RuntimeError("OPENAI_API_KEY is not set in backend/.env")
        _client = OpenAI(max_retries=4, timeout=600)
    return _client


def transcribe(audio_path: Path) -> dict:
    """Word- and segment-level timestamps (Whisper verbose_json)."""
    if transcribe_hook is not None:
        return transcribe_hook(audio_path)
    with audio_path.open("rb") as f:
        r = client().audio.transcriptions.create(
            model=cs.OPENAI_TRANSCRIBE_MODEL,
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["word", "segment"],
        )
    data = r.model_dump() if hasattr(r, "model_dump") else dict(r)
    return {
        "text": data.get("text", ""),
        "language": data.get("language"),
        "segments": [{"start": s["start"], "end": s["end"], "text": s["text"].strip()} for s in data.get("segments") or []],
        "words": [{"start": w["start"], "end": w["end"], "word": w["word"].strip()} for w in data.get("words") or []],
    }


def generate_json(system: str, user: str, schema: dict, name: str = "result") -> dict:
    if json_hook is not None:
        return json_hook(system, user, schema)
    r = client().chat.completions.create(
        model=cs.OPENAI_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},
    )
    choice = r.choices[0]
    if getattr(choice.message, "refusal", None):
        raise RuntimeError(f"Model refused: {choice.message.refusal}")
    if choice.finish_reason == "length":
        raise RuntimeError("Model response was truncated.")
    return json.loads(choice.message.content)


def tts(text: str, out_path: Path, voice: str | None = None, instructions: str | None = None) -> None:
    if tts_hook is not None:
        return tts_hook(text, out_path)  # voice ignored in tests
    voice = voice or cs.OPENAI_TTS_VOICE
    with client().audio.speech.with_streaming_response.create(
        model=cs.OPENAI_TTS_MODEL, voice=voice, input=text, instructions=instructions or cs.TTS_INSTRUCTIONS, response_format="mp3",
    ) as resp:
        resp.stream_to_file(out_path)
