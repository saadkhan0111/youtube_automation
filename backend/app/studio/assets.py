"""Assets stage: find or make one visual for every storyboard shot.

Sources, by the shot's visual_type:
  stock_video / stock_image -> Pexels API (needs PEXELS_API_KEY; free). No key or no result -> "missing".
  ai_image                  -> OpenAI image model, in the project's one visual style; always labelled as an illustration.
  gameplay / user_footage   -> the creator's own videos (Videos library + AI Clipper sources). None -> stock -> missing.
  graphic                   -> nothing to fetch: drawn by the renderer (title cards, stat callouts, ...).

Nothing is substituted silently: a shot we can't fill stays "missing" with the reason, and the review screen offers
upload / AI image / next result. Re-running only works on shots whose plan changed (uploads are always kept).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..clipper import ai as clip_ai
from ..clipper import media
from ..clipper import settings as clip_settings
from ..db import session_scope
from . import costs, worker
from . import settings as ss
from .models import StudioAsset, StudioProject

log = logging.getLogger("ytshorts.studio.assets")

IMAGE_QUALITY = os.environ.get("STUDIO_IMAGE_QUALITY", "medium")      # low | medium | high
IMAGE_WORKERS = 3
MAX_DOWNLOAD = 120 * 1024 * 1024
PEXELS_LICENSE = "Pexels License: free to use, attribution appreciated"
AI_LICENSE = "AI-generated illustration (OpenAI), owned by you"
UA = "InfoVideoStudio/1.0"

# Test hooks
pexels_hook = None     # fn(kind, query, orientation) -> list[candidate dict] (same shape as _pexels_search output)
download_hook = None   # fn(url, dest) -> None
image_hook = None      # fn(prompt, size) -> png bytes


class AssetError(Exception):
    pass


# --------------------------------------------------------------------------- shot helpers
def pexels_key() -> str:
    return os.environ.get(ss.PEXELS_API_KEY_ENV, "").strip()


def shots_of(board: dict) -> list[tuple[str, int, dict, dict]]:
    """(scene_id, shot_index, shot, scene) for every shot in storyboard order."""
    return [(sc["scene_id"], i, sh, sc) for sc in board["scenes"] for i, sh in enumerate(sc["shots"])]


def spec_key(shot: dict, p: StudioProject, board: dict) -> str:
    """What the asset depends on. If it is unchanged, the existing asset is kept on re-run."""
    raw = json.dumps([shot["visual_type"], shot["visual_description"], shot.get("search_keywords", []),
                      p.video_format, board["overall"].get("visual_style") if shot["visual_type"] == "ai_image" else ""])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def orientation(p: StudioProject) -> str:
    return "portrait" if p.video_format == "short" else "landscape"


def assets_dir(project_id: int) -> Path:
    d = ss.project_dir(project_id) / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _thumb(src: Path, dest: Path, at: float = 0.0) -> Path | None:
    """Small JPEG preview for the review screen."""
    args = [clip_settings.ffmpeg_path(), "-y", "-v", "error"]
    if at > 0:
        args += ["-ss", f"{at:.2f}"]
    args += ["-i", str(src), "-frames:v", "1", "-vf", "scale=360:360:force_original_aspect_ratio=decrease", "-q:v", "4", str(dest)]
    try:
        subprocess.run(args, check=True, timeout=60)
        return dest
    except Exception as exc:  # noqa: BLE001 - a missing preview is not fatal
        log.warning("thumbnail failed for %s: %s", src, exc)
        return None


def _download(url: str, dest: Path) -> None:
    if download_hook is not None:
        download_hook(url, dest)
        return
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = 0
        while chunk := r.read(1 << 20):
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise AssetError("File too large")
            f.write(chunk)
    tmp.replace(dest)


# --------------------------------------------------------------------------- Pexels
def _pexels_get(path: str, params: dict) -> dict:
    url = f"https://api.pexels.com/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": pexels_key(), "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AssetError("Pexels rejected the API key (check PEXELS_API_KEY).") from exc
        if exc.code == 429:
            raise AssetError("Pexels rate limit reached (200 requests/hour). Try again later.") from exc
        raise AssetError(f"Pexels error {exc.code}") from exc


def _best_video_file(files: list[dict], portrait: bool) -> dict | None:
    """Smallest MP4 that is still sharp enough (short side >= 720, prefer <= 1080 to keep downloads small)."""
    ok = [f for f in files if f.get("file_type") == "video/mp4" and f.get("width") and f.get("height")
          and min(f["width"], f["height"]) >= 720 and (f["height"] >= f["width"]) == portrait]
    if not ok:
        ok = [f for f in files if f.get("file_type") == "video/mp4" and f.get("width") and min(f["width"], f["height"]) >= 540]
    if not ok:
        return None
    return min(ok, key=lambda f: (abs(min(f["width"], f["height"]) - 1080), f["width"] * f["height"]))


def _pexels_search(kind: str, query: str, orient: str) -> list[dict]:
    """Normalized candidates: {id, kind, url (download), page, author, author_url, width, height, duration}."""
    if pexels_hook is not None:
        return pexels_hook(kind, query, orient)
    portrait = orient == "portrait"
    out = []
    if kind == "video":
        data = _pexels_get("videos/search", {"query": query, "orientation": orient, "per_page": 12, "size": "medium"})
        for v in data.get("videos", []):
            f = _best_video_file(v.get("video_files", []), portrait)
            if f:
                out.append({"id": f"v{v['id']}", "kind": "video", "url": f["link"], "page": v.get("url"),
                            "author": (v.get("user") or {}).get("name"), "author_url": (v.get("user") or {}).get("url"),
                            "width": f["width"], "height": f["height"], "duration": float(v.get("duration") or 0)})
    else:
        data = _pexels_get("v1/search", {"query": query, "orientation": orient, "per_page": 12})
        for ph in data.get("photos", []):
            src = ph.get("src", {})
            out.append({"id": f"p{ph['id']}", "kind": "image", "url": src.get("large2x") or src.get("original"),
                        "page": ph.get("url"), "author": ph.get("photographer"), "author_url": ph.get("photographer_url"),
                        "width": ph.get("width"), "height": ph.get("height"), "duration": 0.0})
    return out


def queries_for(shot: dict) -> list[str]:
    """Most specific first, then broader fallbacks."""
    qs = [q.strip() for q in shot.get("search_keywords", []) if q and q.strip()]
    broader = [" ".join(q.split()[:2]) for q in qs]
    seen, out = set(), []
    for q in qs + broader:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out[:6]


def pick_stock(p: StudioProject, shot: dict, kind: str, used: set[str], skip: set[str] = frozenset()) -> dict | None:
    """First good Pexels candidate not used elsewhere in this project. Videos must cover the shot's length."""
    need = float(shot.get("seconds") or 2) + 0.3
    for q in queries_for(shot):
        cands = [c for c in _pexels_search(kind, q, orientation(p)) if c["id"] not in used and c["id"] not in skip and c.get("url")]
        if kind == "video":
            long_enough = [c for c in cands if c["duration"] >= need]
            cands = long_enough or cands
        if cands:
            return {**cands[0], "query": q}
    return None


def fetch_stock(p: StudioProject, shot: dict, kind: str, used: set[str], dest_stem: Path, skip: set[str] = frozenset()) -> dict:
    """Returns asset fields for a Pexels result, or raises AssetError("missing: ...")."""
    if not pexels_key() and pexels_hook is None:
        raise AssetError("missing: No Pexels key. Add PEXELS_API_KEY to backend/.env (free), upload your own, or use an AI image.")
    c = pick_stock(p, shot, kind, used, skip)
    if c is None:
        raise AssetError("missing: Pexels found nothing for these keywords. Upload your own, edit the keywords, or use an AI image.")
    ext = ".mp4" if kind == "video" else ".jpg"
    path = dest_stem.with_suffix(ext)
    _download(c["url"], path)
    used.add(c["id"])
    thumb = _thumb(path, dest_stem.with_name(dest_stem.name + "_thumb.jpg"), 0.5 if kind == "video" else 0)
    return {"kind": kind, "origin": "pexels", "local_path": str(path), "preview_path": str(thumb) if thumb else None,
            "source_url": c.get("page"), "license": PEXELS_LICENSE,
            "attribution": f"{'Video' if kind == 'video' else 'Photo'} by {c.get('author') or 'unknown'} on Pexels",
            "meta": {"pexels_id": c["id"], "query": c["query"], "width": c.get("width"), "height": c.get("height"),
                     "duration": c.get("duration"), "author_url": c.get("author_url")}}


# --------------------------------------------------------------------------- AI images
def image_size(p: StudioProject) -> str:
    return "1024x1536" if p.video_format == "short" else "1536x1024"


def image_prompt(shot: dict, board: dict) -> str:
    o = board["overall"]
    return (f"{shot['visual_description']}\n\nVisual style for the whole video: {o.get('visual_style', '')}. "
            f"Color grade: {o.get('color_grade', '')}.\n"
            "Stylized digital illustration, clearly an artwork and not a photograph. "
            "Text: render only words the description quotes or says a sign/banner reads, spelled exactly; no other text, "
            "captions, logos, watermarks or UI. People are generic, fictional and anonymous: never portray or resemble a real, "
            "identifiable person; if the description refers to a real person, show only an anonymous silhouette from behind. "
            "Leave calm space in the lower third for captions.")


TEXT_IN_IMAGE = re.compile(r"\b(reads?|reading|says|saying|lettering|spells?)\b", re.IGNORECASE)


def image_notes(shot: dict) -> str | None:
    if TEXT_IN_IMAGE.search(shot["visual_description"]):
        return "Contains written text: check the spelling in the preview."
    return None


def generate_image(p: StudioProject, prompt: str, dest_stem: Path) -> dict:
    size = image_size(p)
    if image_hook is not None:
        png = image_hook(prompt, size)
    else:
        try:
            r = clip_ai.client().images.generate(model=ss.IMAGE_MODEL, prompt=prompt, size=size, quality=IMAGE_QUALITY, n=1)
        except Exception as exc:  # noqa: BLE001 - shown to the user per shot
            msg = str(exc)
            if "safety" in msg.lower() or "moderation" in msg.lower():
                raise AssetError("The image model refused this description. Edit the shot description or upload your own.") from exc
            raise AssetError(f"Image generation failed: {msg[:200]}") from exc
        png = base64.b64decode(r.data[0].b64_json)
    costs.record(p.id, "assets", "openai", "image", {"images": 1, "size": size, "quality": IMAGE_QUALITY}, model=ss.IMAGE_MODEL)
    path = dest_stem.with_suffix(".png")
    path.write_bytes(png)
    thumb = _thumb(path, dest_stem.with_name(dest_stem.name + "_thumb.jpg"))
    return {"kind": "image", "origin": "ai", "local_path": str(path), "preview_path": str(thumb) if thumb else None,
            "license": AI_LICENSE, "attribution": "AI illustration", "prompt": prompt,
            "meta": {"model": ss.IMAGE_MODEL, "size": size, "quality": IMAGE_QUALITY}}


# --------------------------------------------------------------------------- creator's own footage
def library_videos() -> list[dict]:
    """The creator's own videos: the Videos library and AI Clipper source videos."""
    from ..models import ClipProject, Video

    out = []
    with session_scope() as s:
        for v in s.query(Video).all():
            out.append({"path": v.file_path, "text": " ".join(filter(None, [v.title, v.topic, v.context_notes, Path(v.file_path).stem]))})
        for c in s.query(ClipProject).all():
            out.append({"path": c.source_path, "text": " ".join(filter(None, [getattr(c, "name", ""), Path(c.source_path).stem]))})
    return [x for x in out if x["path"] and Path(x["path"]).is_file()]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def pick_library(shot: dict, seed: str, used_ranges: list[tuple[str, float]]) -> dict | None:
    vids = library_videos()
    if not vids:
        return None
    want = _words(" ".join(shot.get("search_keywords", [])) + " " + shot["visual_description"])
    best = max(vids, key=lambda v: len(want & _words(v["text"])))
    dur = media.media_duration(Path(best["path"]))
    need = float(shot.get("seconds") or 2)
    lo, hi = dur * 0.1, max(dur * 0.1, dur * 0.9 - need)
    # deterministic but spread out: never reuse a moment already used in this project
    h = int(hashlib.sha1(seed.encode()).hexdigest(), 16)
    start = lo + (h % 10_000) / 10_000 * (hi - lo)
    for _ in range(20):
        if all(not (p == best["path"] and abs(t - start) < need + 1) for p, t in used_ranges):
            break
        start = lo + ((start - lo + need * 3.7) % max(hi - lo, 0.1))
    used_ranges.append((best["path"], start))
    return {"path": best["path"], "start": round(start, 2), "duration": dur}


def use_library(shot: dict, seed: str, used_ranges: list, dest_stem: Path) -> dict:
    c = pick_library(shot, seed, used_ranges)
    if c is None:
        raise AssetError("missing: no videos in your library")
    thumb = _thumb(Path(c["path"]), dest_stem.with_name(dest_stem.name + "_thumb.jpg"), c["start"] + 0.3)
    return {"kind": "video", "origin": "library", "local_path": c["path"], "preview_path": str(thumb) if thumb else None,
            "license": "Your own footage", "attribution": None,
            "meta": {"start": c["start"], "source_duration": c["duration"], "auto_picked": True},
            "note": f"Auto-picked moment at {c['start']:.0f}s of {Path(c['path']).name}. Preview it, or upload a better clip."}


# --------------------------------------------------------------------------- one shot
def make_asset(p: StudioProject, board: dict, scene_id: str, idx: int, shot: dict, used: set[str], used_ranges: list,
               mode: str | None = None, skip: set[str] = frozenset()) -> dict:
    """Asset fields for one shot. `mode` overrides the planned visual_type (replace actions)."""
    stem = assets_dir(p.id) / f"{scene_id}_{idx}_{hashlib.sha1(os.urandom(8)).hexdigest()[:6]}"
    vt = mode or shot["visual_type"]
    if vt == "graphic":
        return {"kind": "graphic", "origin": "graphic", "license": "Made by the renderer",
                "meta": {"graphic": shot.get("graphic")}, "note": "Drawn at render time (no file needed)."}
    if vt == "ai_image":
        out = generate_image(p, image_prompt(shot, board), stem)
        if image_notes(shot):
            out["note"] = image_notes(shot)
        return out
    if vt in ("gameplay", "user_footage"):
        try:
            return use_library(shot, f"{p.id}{scene_id}{idx}", used_ranges, stem)
        except AssetError:
            try:
                a = fetch_stock(p, shot, "video", used, stem, skip)
                a["note"] = "No footage of your own found, so a stock clip is used. Upload your own to replace it."
                return a
            except AssetError as exc:
                raise AssetError("missing: Planned as your own footage, but your library is empty. "
                                 + str(exc).removeprefix("missing: ")) from exc
    kind = "video" if vt == "stock_video" else "image"
    return fetch_stock(p, shot, kind, used, stem, skip)


def _save(project_id: int, scene_id: str, idx: int, fields: dict | None, spec: str, status: str = "ready",
          note: str | None = None) -> None:
    """Replace whatever asset the shot had with this one (old files are removed)."""
    fields = dict(fields or {})
    meta = {**fields.pop("meta", {}), "spec": spec}
    note = fields.pop("note", None) or note
    with session_scope() as s:
        for old in s.query(StudioAsset).filter_by(project_id=project_id, scene_id=scene_id, shot_index=idx).all():
            _remove_files(old)
            s.delete(old)
        s.add(StudioAsset(project_id=project_id, scene_id=scene_id, shot_index=idx, status=status, note=note,
                          kind=fields.pop("kind", "image"), origin=fields.pop("origin", "none"),
                          meta_json=json.dumps(meta), **fields))


def _remove_files(a: StudioAsset) -> None:
    root = ss.project_dir(a.project_id).resolve()
    for f in (a.local_path, a.preview_path):
        if f and root in Path(f).resolve().parents:        # never delete the creator's library files
            Path(f).unlink(missing_ok=True)


def _used_ids(project_id: int, exclude: tuple[str, int] | None = None) -> tuple[set[str], list]:
    used, ranges = set(), []
    with session_scope() as s:
        for a in s.query(StudioAsset).filter_by(project_id=project_id).all():
            if exclude and (a.scene_id, a.shot_index) == exclude:
                continue
            m = json.loads(a.meta_json or "{}")
            if m.get("pexels_id"):
                used.add(m["pexels_id"])
            if a.origin == "library" and "start" in m:
                ranges.append((a.local_path, m["start"]))
    return used, ranges


# --------------------------------------------------------------------------- stage
def _load(project_id: int) -> tuple[StudioProject, dict]:
    with session_scope() as s:
        p = s.get(StudioProject, project_id)
        s.expunge(p)
    if not p.storyboard_json:
        raise AssetError("No storyboard. Run and approve Storyboard first.")
    return p, json.loads(p.storyboard_json)


_fill_requests: set[int] = set()     # projects whose next Assets run should also fill gaps with AI images


def request_ai_fill(project_id: int) -> None:
    _fill_requests.add(project_id)


@worker.register("assets")
def run(project_id: int, report) -> None:
    _run_stage(project_id, report)
    if project_id in _fill_requests:
        _fill_requests.discard(project_id)
        fill_missing_with_ai(project_id, report)


def _run_stage(project_id: int, report) -> None:
    p, board = _load(project_id)
    shots = shots_of(board)
    wanted = {(sid, i) for sid, i, _, _ in shots}
    with session_scope() as s:
        existing = {(a.scene_id, a.shot_index): (a.status, a.origin, json.loads(a.meta_json or "{}").get("spec"))
                    for a in s.query(StudioAsset).filter_by(project_id=project_id).all() if a.scene_id}
        for a in s.query(StudioAsset).filter_by(project_id=project_id).all():
            if a.scene_id and (a.scene_id, a.shot_index) not in wanted:        # shot no longer in the storyboard
                _remove_files(a)
                s.delete(a)
    todo = []
    for sid, i, shot, _ in shots:
        spec = spec_key(shot, p, board)
        st, origin, old_spec = existing.get((sid, i), (None, None, None))
        if st == "ready" and (origin == "upload" or old_spec == spec):
            continue                                                          # keep: unchanged or the creator's upload
        todo.append((sid, i, shot, spec))
    used, ranges = _used_ids(project_id)
    total = max(1, len(todo))
    done = 0

    def finish(sid, i, spec, fn):
        try:
            _save(project_id, sid, i, fn(), spec)
        except AssetError as exc:
            msg = str(exc)
            missing = msg.startswith("missing: ")
            _save(project_id, sid, i, {"kind": "none", "origin": "none"}, spec,
                  status="missing" if missing else "failed", note=msg.removeprefix("missing: "))

    # quick, sequential sources first (keeps Pexels de-duplication simple), AI images in parallel after
    ai_jobs = []
    for sid, i, shot, spec in todo:
        if shot["visual_type"] == "ai_image":
            ai_jobs.append((sid, i, shot, spec))
            continue
        report(0.05 + 0.9 * done / total, f"Finding visuals ({done + 1}/{total})")
        finish(sid, i, spec, lambda shot=shot, sid=sid, i=i: make_asset(p, board, sid, i, shot, used, ranges))
        done += 1
    if ai_jobs:
        with ThreadPoolExecutor(IMAGE_WORKERS) as ex:
            futs = {ex.submit(_safe_make, p, board, sid, i, shot, used, ranges): (sid, i, spec) for sid, i, shot, spec in ai_jobs}
            for fut in as_completed(futs):
                sid, i, spec = futs[fut]
                res = fut.result()
                finish(sid, i, spec, lambda res=res: _raise_or(res))
                done += 1
                report(0.05 + 0.9 * done / total, f"Generating AI images ({done}/{total})")


def _safe_make(*args):
    try:
        return make_asset(*args)
    except AssetError as exc:
        return exc


def _raise_or(res):
    if isinstance(res, Exception):
        raise res
    return res


# --------------------------------------------------------------------------- review helpers
def overview(project_id: int) -> dict:
    p, board = _load(project_id)
    with session_scope() as s:
        rows = {(a.scene_id, a.shot_index): asset_dict(a) for a in s.query(StudioAsset).filter_by(project_id=project_id).all()}
    scenes, counts = [], {"ready": 0, "missing": 0, "failed": 0, "pending": 0}
    plan = {"stock": 0, "ai_image": 0, "library": 0, "graphic": 0}
    for sc in board["scenes"]:
        shots = []
        for i, sh in enumerate(sc["shots"]):
            a = rows.get((sc["scene_id"], i))
            stale = bool(a and a["status"] == "ready" and a["origin"] != "upload" and a["spec"] != spec_key(sh, p, board))
            state = "pending" if a is None or stale else a["status"]
            counts[state] += 1
            vt = sh["visual_type"]
            plan["stock" if vt.startswith("stock") else "library" if vt in ("gameplay", "user_footage") else vt] += 1
            shots.append({"index": i, "visual_type": vt, "description": sh["visual_description"], "keywords": sh.get("search_keywords", []),
                          "seconds": sh.get("seconds"), "graphic": sh.get("graphic"), "asset": a, "stale": stale, "state": state})
        scenes.append({"scene_id": sc["scene_id"], "narration": sc["narration"], "shots": shots})
    return {"scenes": scenes, "counts": counts, "plan": plan, "pexels_key_present": bool(pexels_key()),
            "library_videos": len(library_videos()), "image_model": ss.IMAGE_MODEL, "image_quality": IMAGE_QUALITY,
            "credits": credits(project_id)}


def asset_dict(a: StudioAsset) -> dict:
    m = json.loads(a.meta_json or "{}")
    return {"id": a.id, "scene_id": a.scene_id, "shot_index": a.shot_index, "status": a.status, "note": a.note,
            "kind": a.kind, "origin": a.origin, "has_file": bool(a.local_path), "has_preview": bool(a.preview_path),
            "source_url": a.source_url, "license": a.license, "attribution": a.attribution, "prompt": a.prompt,
            "spec": m.get("spec"), "meta": {k: v for k, v in m.items() if k != "spec"}}


def credits(project_id: int) -> list[str]:
    """Lines for the video description (step 9)."""
    out = []
    with session_scope() as s:
        for a in s.query(StudioAsset).filter_by(project_id=project_id, origin="pexels", status="ready").all():
            line = f"{a.attribution}: {a.source_url}"
            if line not in out:
                out.append(line)
    return out


def blocking_problems(project_id: int) -> list[str]:
    o = overview(project_id)
    c = o["counts"]
    out = []
    if c["missing"] or c["failed"]:
        out.append(f"{c['missing'] + c['failed']} shot(s) have no visual yet.")
    if c["pending"]:
        out.append(f"{c['pending']} shot(s) changed since the last run. Run Assets again.")
    return out


def _shot(board: dict, scene_id: str, idx: int) -> dict:
    sc = next((x for x in board["scenes"] if x["scene_id"] == scene_id), None)
    if sc is None or not 0 <= idx < len(sc["shots"]):
        raise AssetError("Shot not found")
    return sc["shots"][idx]


def replace(project_id: int, scene_id: str, idx: int, action: str) -> dict:
    """action: next (another Pexels/library result) | ai_image | stock_video | stock_image | library | graphic"""
    p, board = _load(project_id)
    shot = _shot(board, scene_id, idx)
    spec = spec_key(shot, p, board)
    used, ranges = _used_ids(project_id, exclude=(scene_id, idx))
    skip: set[str] = set()
    with session_scope() as s:
        cur = s.query(StudioAsset).filter_by(project_id=project_id, scene_id=scene_id, shot_index=idx).first()
        cur_meta = json.loads(cur.meta_json or "{}") if cur else {}
        cur_origin = cur.origin if cur else None
    if action == "next":
        skip = set(cur_meta.get("tried", [])) | ({cur_meta["pexels_id"]} if cur_meta.get("pexels_id") else set())
        if cur_origin == "library":
            mode = "gameplay"
            ranges.append((cur.local_path, cur_meta.get("start", 0)))     # pick a different moment
        elif cur_origin == "pexels":
            mode = "stock_video" if cur_meta["pexels_id"].startswith("v") else "stock_image"
        elif shot["visual_type"] in ("stock_video", "stock_image", "gameplay", "user_footage"):
            mode = shot["visual_type"]
        else:
            mode = "stock_video"
    elif action == "library":
        mode = "gameplay"
    elif action in ("ai_image", "stock_video", "stock_image", "graphic"):
        mode = action
    else:
        raise AssetError("Unknown action")
    try:
        fields = make_asset(p, board, scene_id, idx, shot, used, ranges, mode=mode, skip=skip)
        fields.setdefault("meta", {})["tried"] = sorted(skip)
        _save(project_id, scene_id, idx, fields, spec)
    except AssetError as exc:
        msg = str(exc)
        if action == "next" and cur and msg.startswith("missing: "):
            raise AssetError("No more results for these keywords. Edit the shot's keywords in the storyboard, or upload.") from exc
        raise AssetError(msg.removeprefix("missing: ")) from exc
    return overview(project_id)


def save_upload(project_id: int, scene_id: str, idx: int, filename: str, src) -> dict:
    p, board = _load(project_id)
    shot = _shot(board, scene_id, idx)
    ext = Path(filename).suffix.lower()
    kind = "video" if ext in (".mp4", ".mov", ".m4v", ".webm", ".mkv") else "image" if ext in (".jpg", ".jpeg", ".png", ".webp") else None
    if kind is None:
        raise AssetError("Upload a video (mp4/mov/webm/mkv) or an image (jpg/png/webp).")
    stem = assets_dir(project_id) / f"{scene_id}_{idx}_up{hashlib.sha1(os.urandom(8)).hexdigest()[:6]}"
    path = stem.with_suffix(ext)
    with open(path, "wb") as f:
        shutil.copyfileobj(src, f)
    try:
        info = media.probe(path)
    except Exception as exc:  # noqa: BLE001
        path.unlink(missing_ok=True)
        raise AssetError("That file couldn't be read as a video or image.") from exc
    thumb = _thumb(path, stem.with_name(stem.name + "_thumb.jpg"), 0.5 if kind == "video" else 0)
    _save(project_id, scene_id, idx, {"kind": kind, "origin": "upload", "local_path": str(path),
                                      "preview_path": str(thumb) if thumb else None, "license": "Your upload",
                                      "meta": {"filename": filename, "duration": info.get("duration"),
                                               "width": info.get("width"), "height": info.get("height")}},
          spec_key(shot, p, board))
    return overview(project_id)


def fill_missing_with_ai(project_id: int, report=lambda *_: None) -> dict:
    p, board = _load(project_id)
    o = overview(project_id)
    todo = [(sc["scene_id"], sh["index"]) for sc in o["scenes"] for sh in sc["shots"] if sh["state"] in ("missing", "failed")]
    for n, (sid, i) in enumerate(todo):
        report(n / max(1, len(todo)), f"AI image {n + 1}/{len(todo)}")
        shot = _shot(board, sid, i)
        try:
            fields = generate_image(p, image_prompt(shot, board), assets_dir(project_id) / f"{sid}_{i}_ai{hashlib.sha1(os.urandom(8)).hexdigest()[:6]}")
            fields["note"] = f"AI illustration instead of {shot['visual_type'].replace('_', ' ')}."
            _save(project_id, sid, i, fields, spec_key(shot, p, board))
        except AssetError as exc:
            _save(project_id, sid, i, {"kind": "none", "origin": "none"}, spec_key(shot, p, board), status="failed", note=str(exc))
    return overview(project_id)
