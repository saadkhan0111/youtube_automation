"""Small command-line helpers.

  python -m app.cli status              connection + quota summary
  python -m app.cli make-test-clip      render a 1080x1920 H.264/AAC test Short into the incoming folder (needs ffmpeg)
  python -m app.cli scan                import ready clips from the incoming folder now
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime

from . import db
from .config import settings


def _init() -> None:
    settings.ensure_dirs()
    db.init_engine()
    db.create_all()


def cmd_status(_args) -> None:
    from .services import oauth, quota

    _init()
    with db.session_scope() as s:
        st = oauth.connection_status(s)
        q = quota.status(s)
    ch = st.get("channel") or {}
    print(f"OAuth client file: {'present' if st['client_secret_present'] else 'MISSING'} ({settings.client_secret_file})")
    print(f"Connection: {st['status']}  channel: {ch.get('name')} ({ch.get('id')})")
    if st.get("last_error"):
        print(f"Last error: {st['last_error']}")
    for name, b in q["buckets"].items():
        flags = " EXHAUSTED" if b["exhausted"] else (" UNCERTAIN" if b["uncertain"] else "")
        print(f"Quota {name:6}: {b['used']}/{b['limit']} {b['unit']}{flags}")
    print(f"Quota day (Pacific): {q['quota_day_pacific']}  next reset (UTC): {q['next_reset_utc']}")


def cmd_make_test_clip(args) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        sys.exit("ffmpeg not found. Install it (winget install Gyan.FFmpeg) and open a new terminal.")
    settings.ensure_dirs()
    name = f"test_clip_{datetime.now():%Y%m%d_%H%M%S}"
    tmp = settings.data_dir / f"{name}.mp4"
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=1080x1920:rate=30:duration={args.seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={args.seconds}",
        "-vf", "drawtext=text='PRIVATE UPLOAD TEST':fontsize=72:fontcolor=white:x=(w-text_w)/2:y=h*0.15:box=1:boxcolor=black@0.6",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "high", "-r", "30",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-shortest", str(tmp),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        # drawtext needs a font; retry without it
        cmd = [c for c in cmd if not c.startswith("drawtext")]
        i = cmd.index("-vf")
        del cmd[i:i + 1]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(r.stderr)
    dest = settings.incoming_dir / f"{name}.mp4"
    shutil.move(str(tmp), dest)  # move the finished render in first...
    sidecar = {
        "source_url": None,
        "source_creator": "Me (generated test pattern)",
        "permission_status": "original",
        "footage_type": "original",
        "permission_notes": "Synthetic test pattern generated locally.",
        "topic": "Upload test",
        "context_notes": "Synthetic test pattern used to verify the private upload pipeline. Not gameplay.",
        "title": f"Private upload test {datetime.now():%Y-%m-%d %H:%M}",
        "description": "Automated pipeline test. Private.",
        "tags": ["test"],
        "hashtags": [],
    }
    (settings.incoming_dir / f"{name}.json").write_text(json.dumps(sidecar, indent=2), encoding="utf-8")  # ...sidecar last
    print(f"Created {dest} (+ sidecar). It will be imported on the next scan (~10 s after creation).")


def cmd_scan(_args) -> None:
    from .services import ingest

    _init()
    ids = ingest.scan_incoming()
    print(f"Imported: {ids or 'nothing'} (files must be unchanged for 10 s before import)")


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    m = sub.add_parser("make-test-clip")
    m.add_argument("--seconds", type=int, default=8)
    m.set_defaults(fn=cmd_make_test_clip)
    sub.add_parser("scan").set_defaults(fn=cmd_scan)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
