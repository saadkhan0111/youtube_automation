"""Record a YouTube live stream while it is live, in parts; every finished part becomes a quick-mode Clipper project
(no transcript, no AI voice: loud moments are proposed, you trim, render and post).

yt-dlp resolves the live HLS address; FFmpeg copies it (no re-encode) into MPEG-TS parts with the segment muxer.
TS parts stay playable even if the app is killed mid-part. The recorder reconnects when the connection drops or the
signed stream URL expires, and stops when the stream ends, when you press Stop, or at the time limit.

Same permission rules as YouTube imports: only streams you own or were allowed to clip, with proof recorded.
"""
from __future__ import annotations

import csv
import logging
import subprocess
import threading
import time
from pathlib import Path

from ..db import session_scope
from ..events import record_event
from ..models import ClipProject, LiveRecording, LiveStatus, ProjectStatus
from ..timeutil import utcnow
from . import settings as cs
from . import youtube_import as yi

log = logging.getLogger("ytshorts.clipper.live")

SECONDS_PER_PART_MINUTE = 60     # tests shrink this
MIN_PART_SECONDS = 60            # shorter leftovers (e.g. the last seconds before Stop) are dropped
RETRY_DELAY = 10
MAX_RECONNECTS = 30

# Tests replace this: fn(url) -> {"live_status": ..., "title", "channel", "stream_url", "headers"}
resolve_hook = None

_runs: dict[int, dict] = {}       # recording id -> {"thread", "stop", "proc"}
_lock = threading.Lock()


class LiveError(RuntimeError):
    pass


def rec_dir(rec_id: int) -> Path:
    d = cs.sources_dir() / "live" / str(rec_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve(url: str) -> dict:
    """Live status and the HLS address of the best <=1080p muxed format."""
    if resolve_hook is not None:
        return resolve_hook(url)
    if not yi.is_youtube_url(url):
        raise LiveError("Paste a YouTube live link.")
    try:
        with yi._ydl({"skip_download": True}) as y:
            info = y.extract_info(url.strip(), download=False)
    except Exception as exc:
        raise LiveError(yi._friendly(exc)) from exc
    live = info.get("live_status") or ("is_live" if info.get("is_live") else "not_live")
    out = {"live_status": live, "title": info.get("title"), "channel": info.get("channel") or info.get("uploader"),
           "url": info.get("webpage_url") or url, "stream_url": None, "headers": {}}
    if live == "is_live":
        hls = [f for f in info.get("formats", []) if str(f.get("protocol", "")).startswith("m3u8")]
        has_v = lambda f: f.get("vcodec") not in (None, "none")                      # noqa: E731
        has_a = lambda f: f.get("acodec") not in (None, "none") or (not has_v(f) and f.get("acodec") is None)  # noqa: E731
        muxed = [f for f in hls if has_v(f) and f.get("acodec") not in (None, "none") and (f.get("height") or 0) <= 1080]
        video = [f for f in hls if has_v(f) and (f.get("height") or 0) <= 1080]
        audio = [f for f in hls if not has_v(f) and has_a(f)]
        best_v = max(muxed or video, key=lambda f: ((f.get("height") or 0), f.get("tbr") or 0), default=None)
        if best_v is None:
            raise LiveError("No recordable stream format found.")
        out.update(stream_url=best_v["url"], headers=best_v.get("http_headers") or {}, height=best_v.get("height"))
        if not muxed:
            # YouTube now serves live video and audio as separate HLS streams: record both together
            if not audio:
                raise LiveError("The stream has no audio track to record.")
            best_a = max(audio, key=lambda f: (f.get("abr") or f.get("tbr") or 0, f.get("format_id", "")))
            out["audio_url"] = best_a["url"]
    return out


def _update(rec_id: int, **fields) -> None:
    with session_scope() as s:
        r = s.get(LiveRecording, rec_id)
        if r is not None:
            for k, v in fields.items():
                setattr(r, k, v)


def _register_part(rec_id: int, path: Path, part_no: int, start_offset: float) -> int | None:
    """A finished part becomes a quick-mode Clipper project (analysis starts right away)."""
    from . import media, worker

    try:
        dur = media.probe(path)["duration"]
    except Exception as exc:  # noqa: BLE001
        log.warning("part %s unreadable: %s", path, exc)
        return None
    if dur < MIN_PART_SECONDS:
        path.unlink(missing_ok=True)
        return None
    with session_scope() as s:
        r = s.get(LiveRecording, rec_id)
        if s.query(ClipProject).filter_by(source_path=str(path)).first():
            return None
        h0, h1 = start_offset, start_offset + dur
        fmt = lambda x: f"{int(x // 3600)}:{int(x % 3600 // 60):02d}:{int(x % 60):02d}"  # noqa: E731
        p = ClipProject(name=f"{(r.title or 'Live stream')[:180]} · part {part_no} ({fmt(h0)}-{fmt(h1)})", source_path=str(path),
                        source_mode="live", source_url=r.url, source_creator=r.source_creator or r.channel,
                        permission_status=r.permission_status, permission_notes=r.permission_notes, plain=True,
                        live_recording_id=rec_id, live_part=part_no, max_candidates=r.max_candidates,
                        min_clip_seconds=r.min_clip_seconds, max_clip_seconds=r.max_clip_seconds,
                        status=ProjectStatus.EXTRACTING, progress=0.0, stage_message="Queued (quick mode)")
        s.add(p)
        s.flush()
        pid = p.id
        r.parts_done = (r.parts_done or 0) + 1
    record_event("clipper", f"Live recording #{rec_id}: part {part_no} saved as clip project #{pid}")
    worker.enqueue_analysis(pid)
    return pid


def _ffmpeg_cmd(stream_url: str, headers: dict, out_dir: Path, part_seconds: int, start_number: int, list_name: str,
                audio_url: str | None = None) -> list[str]:
    cmd = [cs.ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "warning"]

    def inp(u: str) -> list[str]:
        opts = []
        if headers:
            opts += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        if u.startswith("http"):
            opts += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
        return opts + ["-i", u]

    cmd += inp(stream_url)
    if audio_url:
        cmd += inp(audio_url) + ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    cmd += ["-c", "copy",
            "-f", "segment", "-segment_time", str(part_seconds), "-segment_start_number", str(start_number),
            "-reset_timestamps", "1", "-segment_list", list_name, "-segment_list_type", "csv",
            "-segment_format", "mpegts", "part_%04d.ts"]
    return cmd


def _run(rec_id: int, stop: threading.Event, first_info: dict | None = None) -> None:
    with session_scope() as s:
        r = s.get(LiveRecording, rec_id)
        url, part_seconds, max_seconds = r.url, r.part_minutes * SECONDS_PER_PART_MINUTE, r.max_minutes * SECONDS_PER_PART_MINUTE
    out_dir = rec_dir(rec_id)
    registered: set[str] = set()
    offsets: dict[str, float] = {}
    next_part, elapsed_before, reconnects, started = 1, 0.0, 0, False
    ended_reason = "Stream ended"
    try:
        while not stop.is_set():
            info, first_info = (first_info or resolve(url)), None     # the API already checked the stream once
            if info["live_status"] != "is_live":
                if not started:
                    raise LiveError("This stream isn't live right now." + (" It hasn't started yet." if info["live_status"] == "is_upcoming" else
                                                                            " For a finished stream use 'From YouTube link'."))
                break
            if not started:
                _update(rec_id, title=info.get("title"), channel=info.get("channel"), status=LiveStatus.RECORDING,
                        started_at=utcnow(), message="Recording")
                started = True
            list_name = f"parts_{next_part:04d}.csv"
            cmd = _ffmpeg_cmd(info["stream_url"], info.get("headers") or {}, out_dir, part_seconds, next_part, list_name,
                              info.get("audio_url"))
            errlog = (out_dir / f"ffmpeg_{next_part:04d}.log").open("w", encoding="utf-8")
            proc = subprocess.Popen(cmd, cwd=out_dir, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errlog)
            _runs[rec_id]["proc"] = proc
            seg_start = time.monotonic()
            while proc.poll() is None:
                time.sleep(2)
                elapsed = elapsed_before + (time.monotonic() - seg_start)
                _collect(rec_id, out_dir, registered, offsets, elapsed_before, final=False)
                size = sum(f.stat().st_size for f in out_dir.glob("part_*.ts"))
                _update(rec_id, recorded_seconds=round(elapsed, 1), bytes_written=size,
                        message=f"Recording · part {max(len(list(out_dir.glob('part_*.ts'))), 1)} in progress")
                if stop.is_set() or elapsed >= max_seconds:
                    if elapsed >= max_seconds and not stop.is_set():
                        ended_reason = f"Time limit reached ({max_seconds // 60} min)"
                        stop.set()
                    _quit(proc)
                    break
            proc.wait(timeout=60)
            errlog.close()
            elapsed_before += time.monotonic() - seg_start
            _collect(rec_id, out_dir, registered, offsets, elapsed_before, final=True)
            next_part = max([int(p.stem.split("_")[1]) for p in out_dir.glob("part_*.ts")] + [next_part - 1]) + 1
            if stop.is_set():
                if ended_reason == "Stream ended":
                    ended_reason = "Stopped by you"
                break
            reconnects += 1
            if reconnects > MAX_RECONNECTS:
                raise LiveError("Lost the stream too many times.")
            _update(rec_id, message="Connection lost or stream address expired: reconnecting")
            stop.wait(RETRY_DELAY if proc.returncode else 1)
        _update(rec_id, status=LiveStatus.FINISHED, ended_at=utcnow(), message=ended_reason)
        record_event("clipper", f"Live recording #{rec_id} finished: {ended_reason}")
    except Exception as exc:
        log.exception("live recording failed")
        _collect(rec_id, out_dir, registered, offsets, elapsed_before, final=True)
        _update(rec_id, status=LiveStatus.FAILED, ended_at=utcnow(), error=str(exc)[:1000], message="Failed")
        record_event("clipper", f"Live recording #{rec_id} failed: {exc}", level="error")
    finally:
        _runs.pop(rec_id, None)


def _quit(proc: subprocess.Popen) -> None:
    """Ask FFmpeg to finish the current part cleanly (like pressing q)."""
    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()


def _collect(rec_id: int, out_dir: Path, registered: set[str], offsets: dict[str, float], elapsed_before: float,
             final: bool) -> None:
    """Register parts FFmpeg has finished (listed in a segment list), or every part once FFmpeg has exited."""
    done = set()
    for lst in out_dir.glob("parts_*.csv"):
        try:
            with lst.open(encoding="utf-8") as fh:
                for row in csv.reader(fh):
                    if row:
                        done.add(row[0])
        except OSError:
            pass
    files = sorted(out_dir.glob("part_*.ts"))
    if final:
        done |= {f.name for f in files}
    for f in files:
        if f.name not in offsets:
            offsets[f.name] = sum(_dur_cache.get(str(x), 0.0) for x in files if x.name < f.name)
        if f.name in done and f.name not in registered:
            registered.add(f.name)
            part_no = int(f.stem.split("_")[1])
            from . import media

            try:
                _dur_cache[str(f)] = media.probe(f)["duration"]
            except Exception:  # noqa: BLE001
                _dur_cache[str(f)] = 0.0
            _register_part(rec_id, f, part_no, offsets[f.name])


_dur_cache: dict[str, float] = {}


# --------------------------------------------------------------------------- control
def start(rec_id: int, first_info: dict | None = None) -> None:
    with _lock:
        if rec_id in _runs:
            return
        stop = threading.Event()
        t = threading.Thread(target=_run, args=(rec_id, stop, first_info), name=f"live-{rec_id}", daemon=True)
        _runs[rec_id] = {"thread": t, "stop": stop, "proc": None}
        t.start()


def stop(rec_id: int) -> bool:
    run = _runs.get(rec_id)
    if not run:
        return False
    _update(rec_id, status=LiveStatus.STOPPING, message="Stopping: finishing the current part")
    run["stop"].set()
    return True


def wait(rec_id: int, timeout: float = 120) -> None:
    run = _runs.get(rec_id)
    if run:
        run["thread"].join(timeout)


def recover_on_startup() -> None:
    """Recordings cut off by an app restart: keep (and register) the parts already on disk, mark them interrupted."""
    with session_scope() as s:
        ids = [r.id for r in s.query(LiveRecording).filter(LiveRecording.status.in_(list(LiveStatus.ACTIVE))).all()]
    for rid in ids:
        d = rec_dir(rid)
        _collect(rid, d, set(), {}, 0.0, final=True)
        _update(rid, status=LiveStatus.FAILED, ended_at=utcnow(),
                error="Interrupted by an app restart. Parts recorded so far were kept; start a new recording to continue.")
