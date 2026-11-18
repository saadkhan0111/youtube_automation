"""Background music for Studio videos.

Order of preference (nothing is downloaded from YouTube or random websites):
  1. your own tracks: backend/data/music/<mood>/*.mp3|wav|m4a|ogg|flac  (e.g. data/music/energetic/track.mp3)
  2. Jamendo API (needs JAMENDO_CLIENT_ID, free): only Creative Commons licenses that allow commercial use and
     changes (CC BY / CC BY-SA). The track's credit line is stored for the video description.
  3. no music (the render reports it).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..config import settings
from . import settings as ss

log = logging.getLogger("ytshorts.studio.music")

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}
# Jamendo tags that suit each mood
MOOD_TAGS = {
    "energetic": "energetic+electronic", "upbeat": "upbeat+happy", "epic": "epic+cinematic", "suspense": "suspense+tension",
    "mysterious": "mysterious+ambient", "dark": "dark+cinematic", "emotional": "emotional+piano", "chill": "chill+lofi",
}
SPEED_FOR_MOOD = {"energetic": "high+veryhigh", "upbeat": "high+medium", "epic": "medium+high"}

# Test hooks
jamendo_hook = None    # fn(mood) -> list[track dict] (same shape as _jamendo_tracks output)
download_hook = None   # fn(url, dest) -> None


def music_root() -> Path:
    d = settings.data_dir / "music"
    d.mkdir(parents=True, exist_ok=True)
    return d


def jamendo_key() -> str:
    return os.environ.get(ss.JAMENDO_CLIENT_ID_ENV, "").strip()


def license_ok(url: str | None) -> bool:
    """CC BY or CC BY-SA only: NC (non-commercial) would block monetized videos, ND forbids editing/mixing."""
    u = (url or "").lower()
    return "creativecommons.org/licenses/by" in u and "-nc" not in u and "-nd" not in u


def local_tracks(mood: str) -> list[Path]:
    d = music_root() / mood
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.suffix.lower() in AUDIO_EXT and p.is_file())


def _jamendo_tracks(mood: str) -> list[dict]:
    if jamendo_hook is not None:
        return jamendo_hook(mood)
    params = {"client_id": jamendo_key(), "format": "json", "limit": 30, "fuzzytags": MOOD_TAGS.get(mood, mood),
              "include": "licenses musicinfo", "audioformat": "mp32", "order": "popularity_total",
              "vocalinstrumental": "instrumental", "durationbetween": "60_400"}
    if mood in SPEED_FOR_MOOD:
        params["speed"] = SPEED_FOR_MOOD[mood]
    url = "https://api.jamendo.com/v3.0/tracks/?" + urllib.parse.urlencode(params, safe="+")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "InfoVideoStudio/1.0"}), timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.warning("Jamendo search failed: %s", exc)
        return []
    if data.get("headers", {}).get("status") != "success":
        log.warning("Jamendo error: %s", data.get("headers"))
        return []
    return [{"id": str(t["id"]), "title": t.get("name"), "artist": t.get("artist_name"), "duration": float(t.get("duration") or 0),
             "audio": t.get("audiodownload") or t.get("audio"), "download_allowed": t.get("audiodownload_allowed", True),
             "license_url": t.get("license_ccurl"), "page": t.get("shareurl")} for t in data.get("results", [])]


def _download(url: str, dest: Path) -> None:
    if download_hook is not None:
        download_hook(url, dest)
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "InfoVideoStudio/1.0"}), timeout=60) as r, \
            open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    tmp.replace(dest)


def pick(mood: str, min_seconds: float, seed: str, avoid: set[str] = frozenset()) -> dict | None:
    """A track for the mood, or None. Returns {path, title, artist, source, license, credit, page, id}."""
    tracks = [t for t in local_tracks(mood) if str(t) not in avoid]
    if tracks:
        t = tracks[int(hashlib.sha1(seed.encode()).hexdigest(), 16) % len(tracks)]
        return {"id": str(t), "path": str(t), "title": t.stem, "artist": None, "source": "your library",
                "license": "Your own track", "credit": None, "page": None}
    if not jamendo_key() and jamendo_hook is None:
        return None
    cands = [t for t in _jamendo_tracks(mood) if t["audio"] and t["download_allowed"] and license_ok(t["license_url"])
             and t["id"] not in avoid]
    long_enough = [t for t in cands if t["duration"] >= min_seconds] or cands
    if not long_enough:
        return None
    t = long_enough[int(hashlib.sha1(seed.encode()).hexdigest(), 16) % min(5, len(long_enough))]   # one of the top 5
    cache = music_root() / "jamendo"
    cache.mkdir(exist_ok=True)
    path = cache / f"{t['id']}.mp3"
    meta = cache / f"{t['id']}.json"
    if not path.exists():
        _download(t["audio"], path)
    lic = "CC BY-SA" if "by-sa" in (t["license_url"] or "") else "CC BY"
    credit = f"Music: \"{t['title']}\" by {t['artist']} (Jamendo, {lic}) {t['page'] or ''}".strip()
    info = {"id": t["id"], "path": str(path), "title": t["title"], "artist": t["artist"], "source": "Jamendo",
            "license": f"{lic}: {t['license_url']}", "credit": credit, "page": t["page"]}
    meta.write_text(json.dumps(info), encoding="utf-8")
    return info
