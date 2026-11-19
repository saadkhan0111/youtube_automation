"""Import a section of a YouTube video / finished live stream as a Clipper source (yt-dlp).

Only for footage you are allowed to use (your own, or a creator who gave you permission): the project records the
link, the creator and your proof of permission. Note: YouTube's Terms of Service don't allow downloading without
YouTube's own permission even when the creator agrees; this is the user's informed choice (see the dashboard note).

Only the requested time range is downloaded (yt-dlp download_ranges + FFmpeg), up to 1080p, merged to MP4.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse

from ..db import session_scope
from ..models import ClipProject, ProjectStatus
from . import media
from . import settings as cs

log = logging.getLogger("ytshorts.clipper.youtube")

HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}
MAX_RANGE = 4 * 3600            # one import covers at most 4 hours
FORMAT = ("bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b")

# Tests replace this: fn(params) -> object with extract_info(url, download) like yt_dlp.YoutubeDL
ydl_factory = None
_cancel: dict[int, threading.Event] = {}


class ImportError_(RuntimeError):
    pass


def is_youtube_url(url: str) -> bool:
    try:
        u = urlparse(url.strip())
    except ValueError:
        return False
    return u.scheme in ("http", "https") and (u.hostname or "").lower() in HOSTS


def parse_time(value: str | float | int | None) -> float | None:
    """'1:20:05', '80:05', '4805', '4805.5' -> seconds. Empty -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    v = value.strip()
    if not v:
        return None
    if not re.fullmatch(r"\d+(\.\d+)?|\d+(:\d{1,2}){1,2}(\.\d+)?", v):
        raise ImportError_(f"Can't read the time '{value}'. Use h:mm:ss, mm:ss or seconds.")
    parts = [float(x) for x in v.split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def _node_path() -> str | None:
    return shutil.which("node")


def _ydl(extra: dict | None = None):
    params = {"quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True, "retries": 5,
              "fragment_retries": 10, "socket_timeout": 30, "ffmpeg_location": str(Path(cs.ffmpeg_path()).parent)}
    node = _node_path()
    if node:
        params["js_runtimes"] = {"node": {"path": node}}
    params.update(extra or {})
    if ydl_factory is not None:
        return ydl_factory(params)
    import yt_dlp

    return yt_dlp.YoutubeDL(params)


def _friendly(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if "private video" in low:
        return "This video is private. Ask the streamer to make it public or unlisted, or to share the file."
    if "members-only" in low or "join this channel" in low:
        return "This is a members-only video, which can't be imported."
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube is asking for a sign-in check from this network. Try again later or from another network."
    if "age" in low and "restrict" in low:
        return "This video is age-restricted and can't be imported without signing in."
    if "is not a valid url" in low or "unsupported url" in low:
        return "That doesn't look like a YouTube video link."
    return re.sub(r"\x1b\[[0-9;]*m", "", msg).replace("ERROR: ", "")[:400]


def probe(url: str) -> dict:
    """Title, channel, length and live status, without downloading anything."""
    if not is_youtube_url(url):
        raise ImportError_("Paste a YouTube link (youtube.com/watch?v=..., youtube.com/live/..., or youtu.be/...).")
    try:
        with _ydl({"skip_download": True}) as y:
            info = y.extract_info(url.strip(), download=False)
    except ImportError_:
        raise
    except Exception as exc:
        raise ImportError_(_friendly(exc)) from exc
    live = info.get("live_status") or ("is_live" if info.get("is_live") else "not_live")
    return {"id": info.get("id"), "title": info.get("title"), "channel": info.get("channel") or info.get("uploader"),
            "channel_url": info.get("channel_url") or info.get("uploader_url"), "duration": info.get("duration"),
            "live_status": live, "was_live": live in ("was_live", "post_live"), "thumbnail": info.get("thumbnail"),
            "url": info.get("webpage_url") or url.strip(), "upload_date": info.get("upload_date"),
            "chapters": [{"title": c.get("title"), "start": c.get("start_time")} for c in (info.get("chapters") or [])][:50]}


def check_range(info: dict, start: float | None, end: float | None) -> tuple[float, float]:
    if info["live_status"] == "is_live":
        raise ImportError_("This stream is still live. Import it after it ends (the replay appears on the channel).")
    if info["live_status"] == "is_upcoming":
        raise ImportError_("This stream hasn't started yet.")
    dur = float(info.get("duration") or 0)
    s = start or 0.0
    e = end if end is not None else dur
    if dur and e > dur + 1:
        raise ImportError_(f"The end time is after the end of the video ({int(dur // 3600)}:{int(dur % 3600 // 60):02d}:{int(dur % 60):02d}).")
    if e <= s:
        raise ImportError_("The end time must be after the start time.")
    if e - s < 30:
        raise ImportError_("Import at least 30 seconds, so the AI has context to find moments.")
    if e - s > MAX_RANGE:
        raise ImportError_("Import at most 4 hours at a time. Split long streams into parts.")
    return s, e


def _update(pid: int, **fields) -> None:
    with session_scope() as s:
        p = s.get(ClipProject, pid)
        if p is not None:
            for k, v in fields.items():
                setattr(p, k, v)


def _ticker(project_id: int, folder: Path, stem: str, span: float, stop: threading.Event) -> None:
    """Range downloads go through FFmpeg, which reports no progress to yt-dlp: show the growing file size and an
    estimate (these run at about real-time speed)."""
    import time

    t0 = time.monotonic()
    while not stop.wait(3):
        size = sum(f.stat().st_size for f in folder.glob(stem + "*") if f.is_file())
        el = time.monotonic() - t0
        _update(project_id, progress=round(min(0.95, el / max(span * 1.1, 1)), 3),
                stage_message=f"Downloading the section: {size / 1e6:.0f} MB so far ({el:.0f}s, about {span / 60:.0f} min expected)")


def use_full_download(span: float, total: float | None) -> bool:
    """Range downloads stream through FFmpeg at ~1x real time; a full parallel download runs ~10x real time.
    So when the section is a big share of the video, fetching everything and cutting locally is faster."""
    return bool(total) and span >= 0.1 * total


def run_download(project_id: int) -> None:
    """Background job: download the section, then hand the project to the normal analysis."""
    from . import worker

    with session_scope() as s:
        p = s.get(ClipProject, project_id)
        url, dest = p.source_url, Path(p.source_path)
        start, end, total_len = p.clip_range_start or 0.0, p.clip_range_end, p.duration
    span = (end or 0) - start
    cancel = _cancel.setdefault(project_id, threading.Event())
    full_mode = use_full_download(span, total_len)
    stem = dest.stem + ("_full" if full_mode else "")
    tmpl = str(dest.with_name(stem)) + ".%(ext)s"

    def hook(d: dict) -> None:
        if cancel.is_set():
            raise ImportError_("Cancelled")
        if d.get("status") == "downloading":
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            frac = (done / total) if total else 0
            el = d.get("elapsed") or 0
            _update(project_id, progress=round(min(0.99, frac), 3),
                    stage_message=f"Downloading from YouTube: {done / 1e6:.0f} MB" + (f" of ~{total / 1e6:.0f} MB" if total else "")
                    + (f" ({el:.0f}s)" if el else ""))

    try:
        _update(project_id, stage_message="Connecting to YouTube", progress=0.0)
        from yt_dlp.utils import download_range_func

        opts = {"format": FORMAT, "merge_output_format": "mp4", "outtmpl": tmpl, "progress_hooks": [hook],
                "concurrent_fragment_downloads": 8, "http_chunk_size": 10 * 1024 * 1024}
        if not full_mode:
            # cuts snap to the nearest keyframe (a second or two); exact re-encoded cuts were ~4x slower
            opts["download_ranges"] = download_range_func(None, [(start, end)])
        stop = threading.Event()
        if not full_mode:
            threading.Thread(target=_ticker, args=(project_id, dest.parent, stem, span, stop), daemon=True).start()
        try:
            with _ydl(opts) as y:
                y.extract_info(url, download=True)
        finally:
            stop.set()
        got = next((f for f in dest.parent.glob(stem + ".*") if f.suffix.lower() in (".mp4", ".mkv", ".webm")), None)
        if got is None:
            raise ImportError_("The download finished but no video file was produced.")
        if full_mode:
            _update(project_id, progress=0.99, stage_message="Cutting the section")
            subprocess.run([cs.ffmpeg_path(), "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(got), "-t", f"{span:.3f}",
                            "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-avoid_negative_ts", "make_zero",
                            "-movflags", "+faststart", str(dest)], check=True, timeout=1800)
            got.unlink(missing_ok=True)
        elif got != dest:
            got.replace(dest)
        info = media.probe(dest)
        if info["duration"] < min(25.0, span * 0.5):
            raise ImportError_(f"Only {info['duration']:.0f}s were downloaded. The stream may still be processing on YouTube.")
        _update(project_id, status=ProjectStatus.EXTRACTING, progress=0.0, stage_message="Downloaded. Queued for analysis",
                duration=info["duration"], width=info["width"], height=info["height"], error=None)
    except Exception as exc:
        for f in dest.parent.glob(dest.stem + "*"):
            f.unlink(missing_ok=True)
        msg = "Cancelled." if str(exc) == "Cancelled" else f"YouTube import failed: {_friendly(exc)}"
        _update(project_id, status=ProjectStatus.FAILED, error=msg, stage_message=None)
        log.warning("youtube import %s failed: %s", project_id, exc)
        return
    finally:
        _cancel.pop(project_id, None)
    worker.enqueue_analysis(project_id)


def cancel(project_id: int) -> None:
    _cancel.setdefault(project_id, threading.Event()).set()     # also stops a download that is still queued
