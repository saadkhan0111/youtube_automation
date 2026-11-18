"""FFmpeg/ffprobe helpers: probing, audio extraction/chunking, loudness analysis."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ..config import settings
from .settings import ffmpeg_path

CHUNK_SECONDS = 20 * 60   # 20 min of 32 kbps mono MP3 ≈ 4.8 MB (OpenAI limit: 25 MB)


def run(cmd: list[str], cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} failed: {proc.stderr.strip()[-1200:]}")
    return proc


def probe(path: Path) -> dict:
    out = run([settings.ffprobe_path, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]).stdout
    data = json.loads(out)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    return {
        "duration": float(data.get("format", {}).get("duration") or 0),
        "width": int(v["width"]) if v else None,
        "height": int(v["height"]) if v else None,
        "has_audio": a is not None,
    }


def media_duration(path: Path) -> float:
    return probe(path)["duration"]


def extract_audio_chunks(source: Path, out_dir: Path, duration: float, on_progress=None) -> list[tuple[Path, float]]:
    """Mono 16 kHz 32 kbps MP3 chunks. Returns [(path, offset_seconds)]. Existing chunks are reused."""
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks = []
    n = max(1, int((duration + CHUNK_SECONDS - 1) // CHUNK_SECONDS))
    for i in range(n):
        offset = i * CHUNK_SECONDS
        dest = out_dir / f"chunk_{i:03d}.mp3"
        if not dest.exists() or dest.stat().st_size == 0:
            tmp = dest.with_suffix(".tmp.mp3")
            run([ffmpeg_path(), "-y", "-loglevel", "error", "-ss", str(offset), "-t", str(CHUNK_SECONDS), "-i", str(source),
                 "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(tmp)])
            tmp.replace(dest)
        chunks.append((dest, float(offset)))
        if on_progress:
            on_progress((i + 1) / n)
    return chunks


def loudness_curve(source: Path) -> list[float]:
    """Momentary loudness (LUFS, max per second) via the ebur128 filter."""
    proc = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-nostats", "-v", "verbose", "-i", str(source), "-vn", "-af", "ebur128=framelog=verbose", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    per_sec: dict[int, float] = {}
    pat = re.compile(r"t:\s*([\d.]+)\s+TARGET:.*?M:\s*(-?[\d.]+|-inf)")
    for line in proc.stderr.splitlines():
        m = pat.search(line)
        if not m:
            continue
        t = int(float(m.group(1)))
        val = m.group(2)
        v = -70.0 if val == "-inf" else max(-70.0, float(val))
        per_sec[t] = max(per_sec.get(t, -70.0), v)
    if not per_sec:
        return []
    return [per_sec.get(i, -70.0) for i in range(max(per_sec) + 1)]


def loud_peaks(curve: list[float], top: int = 40, min_gap: int = 15) -> list[tuple[int, float]]:
    """Seconds where the audio is markedly louder than its surroundings."""
    if not curve:
        return []
    n = len(curve)
    scored = []
    for i, v in enumerate(curve):
        lo, hi = max(0, i - 30), min(n, i + 31)
        window = sorted(curve[lo:hi])
        median = window[len(window) // 2]
        scored.append((v - median, i, v))
    scored.sort(reverse=True)
    picked: list[tuple[int, float]] = []
    for delta, i, v in scored:
        if delta < 4:  # at least 4 LU above the local median
            break
        if all(abs(i - p) >= min_gap for p, _ in picked):
            picked.append((i, round(delta, 1)))
        if len(picked) >= top:
            break
    return sorted(picked)


def pcm_features(audio: Path, fps: int = 30) -> tuple[list[float], list[float]]:
    """Per video frame: RMS loudness envelope (0..1, normalised) and zero-crossing rate (0..1).
    The zero-crossing rate separates hissy/bright sounds (s, ee: high) from round vowels (oo: low)."""
    import array
    import math

    proc = subprocess.run([ffmpeg_path(), "-v", "error", "-i", str(audio), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                          capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("Could not decode voice audio")
    samples = array.array("h")
    samples.frombytes(proc.stdout[: len(proc.stdout) // 2 * 2])
    per = 16000 // fps
    env, zcr = [], []
    for i in range(0, len(samples), per):
        block = samples[i:i + per]
        if not block:
            break
        env.append(math.sqrt(sum(s * s for s in block) / len(block)) / 32768.0)
        crossings = sum(1 for a, b in zip(block, block[1:]) if (a >= 0) != (b >= 0))
        zcr.append(crossings / max(1, len(block) - 1))
    peak = max(env) if env else 1.0
    return [min(1.0, e / (peak or 1.0)) for e in env], zcr


def pcm_envelope(audio: Path, fps: int = 30) -> list[float]:
    """RMS envelope (0..1) per video frame for an audio file."""
    return pcm_features(audio, fps)[0]
