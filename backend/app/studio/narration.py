"""Voiceover (stage 4): narration audio + exact timing for every scene.

Per scene:
  1. Text for the voice = narration with the pronunciation dictionary applied (captions keep the written form).
  2. OpenAI TTS (the project's voice) with a delivery direction for the narration style, then the shared voice
     processing (compression, presence EQ, slight speed-up) and loudness normalisation so all scenes match.
  3. Whisper word timestamps on the generated audio, aligned to the script's words, so captions use the script's
     spelling with the real timing.
  4. A match check: if Whisper hears something different from the script (skipped/garbled words), it's flagged.
Audio is cached by a hash of everything that affects it: unchanged scenes are never regenerated or paid for twice.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import subprocess
from pathlib import Path

from ..clipper import ai as clip_ai
from ..clipper import media
from ..clipper import settings as clip_settings
from ..clipper import voice as clip_voice
from ..db import session_scope
from . import costs, worker
from . import settings as ss
from .models import StudioProject, StudioScene

log = logging.getLogger("ytshorts.studio.narration")

TARGET_LUFS = -16.0
MATCH_WARN = 0.85          # below this word-match ratio, the scene is flagged

DIRECTIONS = {
    "energetic": clip_settings.TTS_INSTRUCTIONS,
    "dramatic": ("Dramatic, suspenseful documentary narrator. Slightly slower and deliberate, lower register, "
                 "tension in the voice, a short pause before each reveal. Never melodramatic or shouting."),
    "conversational": ("Friendly and relaxed, like telling a friend something cool. Warm, natural rhythm, "
                       "medium pace, a smile in the voice."),
    "authoritative": ("Calm, confident explainer. Clear articulation, measured pace, precise emphasis on key "
                      "terms and numbers. Trustworthy, never robotic."),
    "storyteller": ("Engaging storyteller. Vivid and expressive, builds intrigue, varies pace: quicker through "
                    "the setup, slower on the payoff."),
}
# pause AFTER a scene, by the scene's purpose (seconds)
PAUSE_AFTER = {"hook": 0.25, "setup": 0.2, "point": 0.25, "reveal": 0.35, "transition": 0.15, "payoff": 0.4, "cta": 0.0}
PAUSE_BEFORE_REVEAL = 0.15

DEFAULT_PRONUNCIATIONS = {
    "GTA VI": "G.T.A. six", "GTA 6": "G.T.A. six", "GTA V": "G.T.A. five", "GTA 5": "G.T.A. five",
    "PS5": "P.S. five", "PS4": "P.S. four", "AI": "A.I.", "NPC": "N.P.C.", "NPCs": "N.P.C.s",
}


class VoiceError(RuntimeError):
    pass


# --------------------------------------------------------------------------- text
def spoken_text(text: str, pronunciations: dict[str, str]) -> str:
    """Apply the pronunciation dictionary (whole words/phrases, case-sensitive for acronyms)."""
    for written in sorted(pronunciations, key=len, reverse=True):
        spoken = pronunciations[written]
        if written and spoken:
            text = re.sub(rf"(?<!\w){re.escape(written)}(?!\w)", spoken, text)
    return text


def direction(p: StudioProject) -> str:
    base = DIRECTIONS.get(p.narration_style, DIRECTIONS["energetic"])
    lang = ss.LANGUAGES.get(p.language, "English")
    return base + ("" if p.language == "en" else f" Speak natural, fluent {lang}.")


def settings_of(p: StudioProject) -> dict:
    data = json.loads(p.voice_json) if p.voice_json else {}
    data.setdefault("pronunciations", dict(DEFAULT_PRONUNCIATIONS))
    data.setdefault("speed", 1.0)
    return data


def tempo(p: StudioProject) -> float:
    """Final speed-up applied after synthesis (pitch preserved): shared default x the project's speed setting."""
    return round(clip_settings.VOICE_TEMPO * float(settings_of(p).get("speed", 1.0)), 3)


def cache_key(spoken: str, p: StudioProject) -> str:
    raw = "|".join([spoken, p.voice or ss.DEFAULT_VOICE, direction(p), ss.TTS_MODEL, str(tempo(p)),
                    clip_voice.VOICE_FILTER, str(TARGET_LUFS)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- audio
def normalize_loudness(src: Path, dst: Path) -> None:
    subprocess.run([clip_settings.ffmpeg_path(), "-y", "-v", "error", "-i", str(src),
                    "-af", f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=7,aresample=48000", "-ac", "2", str(dst)], check=True)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w'’]+", text)


def _norm(tok: str) -> str:
    return re.sub(r"[^\w]", "", tok.lower().replace("’", "'"))


def align(script_text: str, whisper_words: list[dict], duration: float,
          respelled: set[str] | None = None) -> tuple[list[dict], float]:
    """Give every script word a start/end time from Whisper's words. Returns (words, match ratio).

    `respelled` = normalized script words the pronunciation dictionary changed (e.g. "gta", "6"); they are
    timed like the others but left out of the match ratio, since Whisper is expected to hear them differently."""
    script_toks = _tokens(script_text)
    if not script_toks:
        return [], 1.0
    heard = [w for w in whisper_words if _norm(w["word"])]
    sm = difflib.SequenceMatcher(a=[_norm(t) for t in script_toks], b=[_norm(w["word"]) for w in heard], autojunk=False)
    times: list[tuple[float, float] | None] = [None] * len(script_toks)
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            times[a + k] = (heard[b + k]["start"], heard[b + k]["end"])
    skip = respelled or set()
    scored = [i for i, tok in enumerate(script_toks) if _norm(tok) not in skip]
    matched = sum(1 for i in scored if times[i] is not None)
    # words Whisper heard differently (names, numbers): interpolate between their known neighbours
    i = 0
    while i < len(times):
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(times) and times[j] is None:
            j += 1
        left = times[i - 1][1] if i > 0 else 0.0
        right = times[j][0] if j < len(times) else duration
        span = max(0.05, right - left)
        n = j - i
        for k in range(n):
            times[i + k] = (round(left + span * k / n, 3), round(left + span * (k + 1) / n, 3))
        i = j
    words = [{"word": t, "start": round(s, 3), "end": round(e, 3)} for t, (s, e) in zip(script_toks, times)]
    return words, round(matched / len(scored), 3) if scored else 1.0


def sentences_with_times(script_text: str, words: list[dict]) -> list[dict]:
    out, idx = [], 0
    for sent in [s for s in re.split(r"(?<=[.!?])\s+", script_text) if s.strip()]:
        n = len(_tokens(sent))
        chunk = words[idx: idx + n]
        if chunk:
            out.append({"text": sent.strip(), "start": chunk[0]["start"], "end": chunk[-1]["end"]})
        idx += n
    return out


def produce_scene(p: StudioProject, scene_id: str, narration: str, purpose: str, audio_dir: Path,
                  pronunciations: dict) -> dict:
    """Generate (or reuse) the audio for one scene and measure its timing."""
    spoken = spoken_text(narration, pronunciations)
    key = cache_key(spoken, p)
    wav = audio_dir / f"{scene_id}_{key}.wav"
    timing_file = wav.with_suffix(".json")
    if wav.exists() and timing_file.exists():
        data = json.loads(timing_file.read_text(encoding="utf-8"))
        data["cached"] = True
        return data
    raw = audio_dir / f"{scene_id}_{key}.voice.wav"
    clip_voice.synth(spoken, raw, p.voice or ss.DEFAULT_VOICE, direction(p), tempo(p))
    costs.record(p.id, "voice", "openai", "tts", {"characters": len(spoken)}, model=ss.TTS_MODEL)
    normalize_loudness(raw, wav)
    raw.unlink(missing_ok=True)
    duration = round(media.media_duration(wav), 3)

    heard = clip_ai.transcribe(wav)
    costs.record(p.id, "voice", "openai", "transcribe", {"minutes": round(duration / 60, 4)}, model=ss.TRANSCRIBE_MODEL)
    respelled = {_norm(tok) for written in pronunciations if written and re.search(rf"(?<!\w){re.escape(written)}(?!\w)", narration)
                 for tok in _tokens(written)}
    words, match = align(narration, heard.get("words", []), duration, respelled)
    data = {
        "scene_id": scene_id, "file": wav.name, "duration": duration, "match": match,
        "heard": heard.get("text", ""), "spoken_text": spoken, "words": words,
        "sentences": sentences_with_times(narration, words), "pause_after": PAUSE_AFTER.get(purpose, 0.2),
        "warnings": [] if match >= MATCH_WARN else [
            f"The voice doesn't fully match the script ({match:.0%} of words heard). Listen to it and regenerate if needed."],
        "cached": False,
    }
    timing_file.write_text(json.dumps(data), encoding="utf-8")
    return data


# --------------------------------------------------------------------------- stage
def summarize(p: StudioProject, scenes: list[dict], board: dict | None) -> dict:
    total = round(sum(s["duration"] + s["pause_after"] for s in scenes), 2)
    # reveals get a little extra breath before them
    if board:
        purposes = [b["purpose"] for b in board["scenes"]]
        total = round(total + PAUSE_BEFORE_REVEAL * sum(1 for x in purposes if x in ("reveal", "payoff")), 2)
    off = (total - p.target_duration) / p.target_duration
    warnings = [f"Narration is {total:.0f}s vs the {p.target_duration}s target ({off:+.0%})."] if abs(off) > 0.2 else []
    return {"total_seconds": total, "target_seconds": p.target_duration, "voice": p.voice or ss.DEFAULT_VOICE,
            "speed": settings_of(p).get("speed", 1.0),
            "narration_style": p.narration_style, "scenes": scenes, "warnings": warnings}


@worker.register("voice")
def run(project_id: int, report) -> None:
    with session_scope() as s:
        p = s.get(StudioProject, project_id)
        s.expunge(p)
        rows = [(r.id, json.loads(r.storyboard_json)) for r in s.get(StudioProject, project_id).scenes if r.storyboard_json]
    if not rows:
        raise VoiceError("No storyboard scenes. Run and approve Storyboard first.")
    board = json.loads(p.storyboard_json) if p.storyboard_json else None
    vs = settings_of(p)
    audio_dir = ss.project_dir(project_id) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for i, (row_id, sb) in enumerate(rows):
        report(0.05 + 0.9 * i / len(rows), f"Recording scene {i + 1}/{len(rows)}")
        data = produce_scene(p, sb["scene_id"], sb["narration"], sb["purpose"], audio_dir, vs["pronunciations"])
        results.append(data)
        with session_scope() as s:
            row = s.get(StudioScene, row_id)
            row.audio_path = str(audio_dir / data["file"])
            row.timing_json = json.dumps({k: data[k] for k in ("words", "sentences", "duration", "pause_after")})
            row.duration = data["duration"]
            row.status = "voiced"
    summary = summarize(p, results, board)
    with session_scope() as s:
        pr = s.get(StudioProject, project_id)
        current = settings_of(pr)
        current["summary"] = summary
        pr.voice_json = json.dumps(current)


# --------------------------------------------------------------------------- single-scene regeneration
def regenerate_one(project_id: int, scene_id: str) -> dict:
    """Re-record one scene from scratch (e.g. it sounded wrong), then update the project summary."""
    with session_scope() as s:
        p = s.get(StudioProject, project_id)
        s.expunge(p)
        row = next((r for r in s.get(StudioProject, project_id).scenes
                    if r.storyboard_json and json.loads(r.storyboard_json)["scene_id"] == scene_id), None)
        if row is None:
            raise VoiceError("Scene not found")
        row_id, sb = row.id, json.loads(row.storyboard_json)
    vs = settings_of(p)
    audio_dir = ss.project_dir(project_id) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    key = cache_key(spoken_text(sb["narration"], vs["pronunciations"]), p)
    for f in (audio_dir / f"{scene_id}_{key}.wav", audio_dir / f"{scene_id}_{key}.json"):
        f.unlink(missing_ok=True)                       # force a fresh take
    data = produce_scene(p, scene_id, sb["narration"], sb["purpose"], audio_dir, vs["pronunciations"])
    with session_scope() as s:
        row = s.get(StudioScene, row_id)
        row.audio_path = str(audio_dir / data["file"])
        row.timing_json = json.dumps({k: data[k] for k in ("words", "sentences", "duration", "pause_after")})
        row.duration = data["duration"]
        pr = s.get(StudioProject, project_id)
        current = settings_of(pr)
        summary = current.get("summary") or {"scenes": []}
        scenes = [data if x["scene_id"] == scene_id else x for x in summary["scenes"]]
        if scene_id not in [x["scene_id"] for x in scenes]:
            scenes.append(data)
        board = json.loads(pr.storyboard_json) if pr.storyboard_json else None
        current["summary"] = summarize(pr, scenes, board)
        pr.voice_json = json.dumps(current)
        return current["summary"]
