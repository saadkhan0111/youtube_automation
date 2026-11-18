"""Pre-upload video validation using ffprobe.

YouTube classifies an upload as a Short based on the video itself: a square or
vertical aspect ratio and a duration of up to 3 minutes (current rules; check
https://support.google.com/youtube/answer/15424877 for changes). Adding
"#Shorts" to the title/description does NOT by itself make a video a Short.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import settings

MAX_SHORTS_DURATION = 180.0           # seconds
MIN_DURATION = 1.0
MAX_FILE_SIZE = 256 * 1024 ** 3       # YouTube's max upload size (256 GB)
STABLE_SECONDS = 10                   # file must be unchanged this long (render finished)
COMPATIBLE_VIDEO = {"h264"}
ACCEPTABLE_VIDEO = {"hevc", "vp9", "av1"}  # YouTube accepts these, but H.264 is the target
COMPATIBLE_AUDIO = {"aac"}

# Tests can replace this: fn(path) -> dict (parsed ffprobe JSON)
ffprobe_runner = None


class ValidationUnavailable(Exception):
    pass


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    frame_rate: float | None = None
    file_size: int | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def ffprobe_available() -> bool:
    return ffprobe_runner is not None or shutil.which(settings.ffprobe_path) is not None or Path(settings.ffprobe_path).is_file()


def run_ffprobe(path: Path) -> dict:
    if ffprobe_runner is not None:
        return ffprobe_runner(path)
    if not ffprobe_available():
        raise ValidationUnavailable(
            "ffprobe was not found. Install FFmpeg (e.g. `winget install Gyan.FFmpeg`) or set FFPROBE_PATH."
        )
    proc = subprocess.run(
        [settings.ffprobe_path, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise ValueError(f"ffprobe could not read the file (corrupt or incomplete render?): {proc.stderr.strip()[:300]}")
    data = json.loads(proc.stdout or "{}")
    if proc.stderr.strip():
        data["_stderr"] = proc.stderr.strip()[:500]
    return data


def _fps(rate: str | None) -> float | None:
    if not rate or rate in {"0/0", "0"}:
        return None
    try:
        num, _, den = rate.partition("/")
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None


def _rotation(stream: dict) -> int:
    rot = stream.get("tags", {}).get("rotate")
    if rot is not None:
        try:
            return int(rot) % 360
        except ValueError:
            pass
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            try:
                return int(sd["rotation"]) % 360
            except (TypeError, ValueError):
                pass
    return 0


def file_is_stable(path: Path, stable_seconds: int = STABLE_SECONDS) -> bool:
    """True if the file hasn't been modified recently and isn't growing."""
    try:
        st1 = path.stat()
    except OSError:
        return False
    if time.time() - st1.st_mtime < stable_seconds:
        return False
    time.sleep(0.2)
    try:
        st2 = path.stat()
    except OSError:
        return False
    return st1.st_size == st2.st_size and st1.st_mtime == st2.st_mtime


def validate_video(path: Path, require_audio: bool = True, check_stable: bool = True) -> ValidationResult:
    res = ValidationResult(ok=False)
    # --- file checks
    if not path.exists():
        res.errors.append("File does not exist.")
        return res
    if not path.is_file():
        res.errors.append("Path is not a regular file.")
        return res
    if not os.access(path, os.R_OK):
        res.errors.append("File is not readable.")
        return res
    if path.suffix.lower() != ".mp4":
        res.errors.append(f"Unsupported container extension '{path.suffix}'. Export MP4.")
    for marker in (".partial", ".tmp", ".part", ".rendering"):
        if marker in path.name.lower():
            res.errors.append("File name indicates an unfinished render.")
    size = path.stat().st_size
    res.file_size = size
    if size == 0:
        res.errors.append("File is empty.")
        return res
    if size > MAX_FILE_SIZE:
        res.errors.append("File exceeds YouTube's 256 GB limit.")
    if check_stable and not file_is_stable(path):
        res.errors.append("File was modified in the last few seconds; rendering may still be in progress.")

    # --- media checks
    try:
        probe = run_ffprobe(path)
    except ValidationUnavailable as exc:
        res.errors.append(str(exc))
        return res
    except Exception as exc:
        res.errors.append(str(exc))
        return res

    fmt = probe.get("format", {}) or {}
    streams = probe.get("streams", []) or []
    fmt_name = fmt.get("format_name", "")
    if "mp4" not in fmt_name and "mov" not in fmt_name:
        res.errors.append(f"Container '{fmt_name}' is not MP4.")
    if probe.get("_stderr"):
        res.warnings.append(f"ffprobe reported: {probe['_stderr'][:200]}")

    video = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        res.errors.append("No video stream found.")
    else:
        vcodec = (video.get("codec_name") or "").lower()
        res.video_codec = vcodec
        if vcodec in ACCEPTABLE_VIDEO:
            res.warnings.append(f"Video codec is {vcodec}; H.264 is the recommended export codec.")
        elif vcodec not in COMPATIBLE_VIDEO:
            res.errors.append(f"Unsupported video codec '{vcodec}'. Export H.264.")
        w, h = int(video.get("width") or 0), int(video.get("height") or 0)
        if _rotation(video) in (90, 270):
            w, h = h, w
        res.width, res.height = w or None, h or None
        if not w or not h:
            res.errors.append("Could not determine video resolution.")
        else:
            if w > h:
                res.errors.append(f"Horizontal video ({w}x{h}). Shorts must be vertical or square.")
            elif abs((w / h) - 9 / 16) > 0.02 and w != h:
                res.warnings.append(f"Aspect ratio {w}x{h} is not 9:16; 1080x1920 is recommended.")
            if (w, h) != (1080, 1920) and w <= h:
                res.warnings.append(f"Resolution is {w}x{h}; 1080x1920 is recommended.")
        avg, real = _fps(video.get("avg_frame_rate")), _fps(video.get("r_frame_rate"))
        res.frame_rate = round(avg or real or 0, 3) or None
        if avg and real and abs(avg - real) > 0.5:
            res.warnings.append(f"Frame rate looks variable (avg {avg:.2f} vs {real:.2f} fps); a constant frame rate is recommended.")
        if res.frame_rate is None:
            res.warnings.append("Could not determine frame rate.")

    if audio is None:
        (res.errors if require_audio else res.warnings).append("No audio stream found.")
    else:
        acodec = (audio.get("codec_name") or "").lower()
        res.audio_codec = acodec
        if acodec not in COMPATIBLE_AUDIO:
            res.warnings.append(f"Audio codec is '{acodec}'; AAC is recommended.")

    duration = None
    try:
        duration = float(fmt.get("duration")) if fmt.get("duration") is not None else None
    except ValueError:
        duration = None
    if duration is None and video is not None and video.get("duration"):
        duration = float(video["duration"])
    res.duration = round(duration, 3) if duration else None
    if not duration:
        res.errors.append("Could not determine duration (file may be incomplete).")
    else:
        if duration < MIN_DURATION:
            res.errors.append(f"Duration {duration:.1f}s is too short.")
        if duration > MAX_SHORTS_DURATION:
            res.errors.append(f"Duration {duration:.1f}s exceeds the 3-minute Shorts limit.")
        if video is not None and video.get("duration"):
            try:
                vdur = float(video["duration"])
                if abs(vdur - duration) > 1.5:
                    res.warnings.append(f"Video stream ({vdur:.1f}s) and container ({duration:.1f}s) durations differ; check the render.")
            except ValueError:
                pass

    res.ok = not res.errors
    return res
