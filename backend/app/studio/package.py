"""Package stage: everything needed to publish the approved render.

  * title options, hashtags, tags, pinned comment and thumbnail text (AI, checked against the script's facts)
  * the description is assembled by code, not by the model: intro + sources actually cited + stock/music credits +
    an AI-imagery note, so credits and sources can never be dropped or invented
  * thumbnail variants: the project's best images, cover-cropped, with big text (FFmpeg + libass, free)
  * approving the package hands the video to the existing Videos queue (pending review); uploading still goes through
    the normal review and schedule there.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from ..clipper import captions as cap
from ..clipper import settings as clip_settings
from ..db import session_scope
from ..services import ingest
from ..services.metadata_rules import MAX_DESCRIPTION_BYTES, clean_hashtags, clean_tags, clean_title
from . import llm, worker
from . import settings as ss
from .models import StudioAsset, StudioProject, StudioRender

log = logging.getLogger("ytshorts.studio.package")

CATEGORY_IDS = {"gaming": "20", "tech": "28", "science": "28", "history": "27", "countdown": "24", "general": "24"}
NUMBER = re.compile(r"\d[\d,.]*")
CLICKBAIT = re.compile(r"\b(you won'?t believe|shocking|insane truth|gone wrong|(100|1000)% (real|proof)|leaked footage|"
                       r"official (trailer|announcement)|confirmed!+)\b", re.IGNORECASE)
THUMB_VARIANTS = 3


class PackageError(RuntimeError):
    pass


def ff() -> str:
    return clip_settings.ffmpeg_path()


# --------------------------------------------------------------------------- metadata (AI)
def _schema() -> dict:
    s = {"type": "string"}
    title = {"type": "object", "additionalProperties": False, "required": ["title", "angle"],
             "properties": {"title": s, "angle": s}}
    return {"type": "object", "additionalProperties": False,
            "required": ["titles", "intro", "hashtags", "tags", "pinned_comment", "thumbnail_texts"],
            "properties": {"titles": {"type": "array", "items": title}, "intro": s,
                           "hashtags": {"type": "array", "items": s}, "tags": {"type": "array", "items": s},
                           "pinned_comment": s, "thumbnail_texts": {"type": "array", "items": s}}}


SYSTEM = """You write YouTube packaging for an information video. Rules:
- Titles: 5 options, max 70 characters, each a different angle (curiosity, list/number, question, bold claim, benefit).
  Every claim and every number in a title must be supported by the narration/facts given. No clickbait lies, no
  "official", "leaked", "confirmed" unless a fact says so, no ALL CAPS titles, at most one emoji.
- intro: 2-3 sentences for the top of the description, written for viewers (not SEO stuffing). No URLs.
- hashtags: 3-5, no spaces, include #Shorts for a Short.
- tags: 10-15 search phrases people actually type.
- pinned_comment: one short question that invites replies.
- thumbnail_texts: 3 options of 2-4 words, punchy, readable at phone size, no punctuation spam.
Write in the video's language."""


def _facts_used(p: StudioProject) -> tuple[list[dict], list[dict]]:
    research = json.loads(p.research_json or "{}")
    scr = json.loads(p.script_json or "{}")
    used = {fid for sc in scr.get("scenes", []) for fid in sc.get("fact_ids", [])}
    facts = [f for f in research.get("facts", []) if f["id"] in used]
    src_ids = {sid for f in facts for sid in f.get("source_ids", [])}
    sources = [s for s in research.get("sources", []) if s["id"] in src_ids]
    return facts, sources


def check_title(title: str, allowed_text: str) -> list[str]:
    warns = []
    if len(title) > 100:
        warns.append("Longer than YouTube's 100-character limit.")
    elif len(title) > 70:
        warns.append("Over 70 characters: it will be cut off on phones.")
    if CLICKBAIT.search(title):
        warns.append(f'Clickbait wording: "{CLICKBAIT.search(title).group(0)}".')
    for n in NUMBER.findall(title):
        if n.rstrip(".,") not in allowed_text:
            warns.append(f"The number {n} isn't in the narration or facts.")
    letters = [c for c in title if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        warns.append("Mostly capitals: looks spammy.")
    return warns


def generate_metadata(p: StudioProject) -> dict:
    scr = json.loads(p.script_json or "{}")
    facts, _ = _facts_used(p)
    narration = scr.get("full_narration") or " ".join(sc["narration"] for sc in scr.get("scenes", []))
    user = (f"Topic: {p.topic}\nFormat: {'YouTube Short (vertical)' if p.video_format == 'short' else 'long YouTube video'}\n"
            f"Audience: {p.audience or 'general'}\nLanguage: {ss.LANGUAGES.get(p.language, 'English')}\n"
            f"Working title from the script: {scr.get('title', '')}\nHook: {scr.get('hook', '')}\n\nNarration:\n{narration}\n\n"
            "Facts used (status in brackets; uncertain ones must stay hedged):\n"
            + "\n".join(f"- [{f['status']}] {f['claim']}" for f in facts))
    raw = llm.chat_json(ss.SCRIPT_MODEL, SYSTEM, user, _schema(), "package", project_id=p.id, stage="package")
    allowed = " ".join([narration, p.topic] + [f["claim"] + " " + f.get("detail", "") for f in facts])
    titles = []
    for t in raw["titles"][:6]:
        title = clean_title(t["title"])
        if title and title not in [x["title"] for x in titles]:
            titles.append({"title": title, "angle": t["angle"], "warnings": check_title(title, allowed)})
    if scr.get("title") and clean_title(scr["title"]) not in [x["title"] for x in titles]:
        title = clean_title(scr["title"])
        titles.append({"title": title, "angle": "script's working title", "warnings": check_title(title, allowed)})
    titles.sort(key=lambda x: len(x["warnings"]))
    hashtags = clean_hashtags(raw["hashtags"])
    if p.video_format == "short" and not any(h.lower() == "#shorts" for h in hashtags):
        hashtags = clean_hashtags(["#Shorts", *hashtags])
    return {"titles": titles, "title": titles[0]["title"] if titles else clean_title(p.topic), "intro": raw["intro"].strip(),
            "hashtags": hashtags, "tags": clean_tags(raw["tags"]), "pinned_comment": raw["pinned_comment"].strip(),
            "thumbnail_texts": [t.strip() for t in raw["thumbnail_texts"] if t.strip()][:4],
            "category_id": CATEGORY_IDS.get(p.category, "24"), "model": ss.SCRIPT_MODEL}


# --------------------------------------------------------------------------- description (code)
def credits(project_id: int, render_summary: dict) -> dict:
    with session_scope() as s:
        rows = s.query(StudioAsset).filter_by(project_id=project_id, status="ready").all()
        stock = sorted({f"{a.attribution}: {a.source_url}" for a in rows if a.origin in ("pexels", "pixabay") and a.source_url})
        ai = sum(1 for a in rows if a.origin == "ai")
    mus = render_summary.get("music") or {}
    return {"stock": stock, "music": mus.get("credit"), "ai_images": ai}


def build_description(meta: dict, sources: list[dict], cred: dict) -> str:
    """Intro, sources, credits, hashtags. Sources and credits are never dropped: if the limit is hit, the intro is trimmed."""
    parts_tail = []
    if sources:
        parts_tail.append("Sources:\n" + "\n".join(f"• {s.get('title') or s.get('publisher') or 'Source'}: {s['url']}" for s in sources))
    cr = []
    if cred.get("stock"):
        cr.append("Stock footage/photos:\n" + "\n".join(f"• {c}" for c in cred["stock"]))
    if cred.get("music"):
        cr.append(cred["music"])
    if cred.get("ai_images"):
        cr.append("Some visuals are AI-generated illustrations, not real footage or photos.")
    if cr:
        parts_tail.append("Credits:\n" + "\n".join(cr))
    if meta.get("hashtags"):
        parts_tail.append(" ".join(meta["hashtags"]))
    tail = "\n\n".join(parts_tail)
    intro = meta.get("intro", "")
    room = MAX_DESCRIPTION_BYTES - len(tail.encode("utf-8")) - 4
    if room < 0:
        raise PackageError("Sources and credits alone exceed YouTube's 5000-byte description limit.")
    if len(intro.encode("utf-8")) > room:
        intro = intro.encode("utf-8")[:room].decode("utf-8", errors="ignore").rsplit(" ", 1)[0] + "…"
    return (intro + "\n\n" + tail).strip()


# --------------------------------------------------------------------------- thumbnails
def thumbnail_sources(project_id: int) -> list[Path]:
    """Best stills for a thumbnail: AI illustrations and stock photos first (sharp, no motion blur), then video frames."""
    with session_scope() as s:
        rows = s.query(StudioAsset).filter_by(project_id=project_id, status="ready").order_by(StudioAsset.scene_id, StudioAsset.shot_index).all()
        imgs = [Path(a.local_path) for a in rows if a.kind == "image" and a.local_path]
        vids = [(Path(a.local_path), json.loads(a.meta_json or "{}").get("start", 0)) for a in rows if a.kind == "video" and a.local_path]
    out = [p for p in imgs if p.is_file()]
    for v, start in vids:
        if v.is_file():
            out.append((v, float(start or 0) + 0.8))
    return out


THUMB_ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: T,Segoe UI Black,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&HA0000000,-1,0,0,0,100,100,1,0,1,{bord},{shad},5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,T,,0,0,0,,{{\\pos({x},{y})\\frz-3}}{text}
"""


def make_thumbnail(src, text: str, out: Path, fmt: str, accent_word: int = -1) -> None:
    W, H = (1280, 720) if fmt == "long" else (1080, 1920)
    work = out.parent
    words = cap.clean(text, True).upper().split()
    if 0 <= accent_word < len(words) or accent_word == -1:
        k = len(words) - 1 if accent_word == -1 else accent_word
        words[k] = r"{\c&H1FD2FF&}" + words[k] + r"{\c&HFFFFFF&}"
    size = 150 if fmt == "long" else 170
    if len(text) > 16:
        size = int(size * 0.8)
    y = int(H * (0.72 if fmt == "long" else 0.30))
    ass = work / f"{out.stem}.ass"
    ass.write_text(THUMB_ASS.format(w=W, h=H, size=size, bord=12, shad=6, x=W // 2, y=y, text=" ".join(words)), encoding="utf-8")
    if isinstance(src, tuple):
        inp = ["-ss", f"{src[1]:.2f}", "-i", str(src[0])]
    else:
        inp = ["-i", str(src)]
    fonts = work / "fonts"
    fonts.mkdir(exist_ok=True)
    for name in ("seguibl.ttf", "ariblk.ttf"):
        f = Path("C:/Windows/Fonts") / name
        if f.exists() and not (fonts / name).exists():
            shutil.copy2(f, fonts / name)
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},eq=saturation=1.25:contrast=1.08,"
          f"vignette=angle=PI/4,subtitles={ass.name}:fontsdir=fonts")
    subprocess.run([ff(), "-y", "-v", "error", *inp, "-frames:v", "1", "-vf", vf, "-q:v", "2", out.name],
                   cwd=work, check=True, timeout=120)
    ass.unlink(missing_ok=True)


def make_thumbnails(p: StudioProject, texts: list[str]) -> list[dict]:
    srcs = thumbnail_sources(p.id)
    if not srcs:
        raise PackageError("No images to build a thumbnail from.")
    d = ss.project_dir(p.id) / "package"
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("thumb_*.jpg"):
        f.unlink()
    texts = texts or [p.topic[:24]]
    # spread the picks over the video (opening image, a middle one, a late one)
    picks = [srcs[0], srcs[len(srcs) // 2], srcs[-1]] if len(srcs) >= 3 else (srcs * 3)[:3]
    out = []
    for i in range(THUMB_VARIANTS):
        name = f"thumb_{i + 1}_{hashlib.sha1(str(picks[i]).encode()).hexdigest()[:6]}.jpg"
        make_thumbnail(picks[i], texts[i % len(texts)], d / name, p.video_format)
        out.append({"file": name, "text": texts[i % len(texts)]})
    return out


# --------------------------------------------------------------------------- stage
def _latest_render(project_id: int) -> tuple[int, Path, dict]:
    with session_scope() as s:
        pr = s.get(StudioProject, project_id)
        rid = json.loads(pr.outputs_json or "{}").get("qa", {}).get("render_id")
        r = s.get(StudioRender, rid) if rid else None
        if r is None or r.status != "done" or not r.file_path or not Path(r.file_path).is_file():
            raise PackageError("No checked render. Run the quality check first.")
        return r.id, Path(r.file_path), json.loads(r.qa_json or "{}").get("summary", {})


@worker.register("package")
def run(project_id: int, report) -> None:
    with session_scope() as s:
        p = s.get(StudioProject, project_id)
        s.expunge(p)
    rid, _, rsum = _latest_render(project_id)
    report(0.1, "Writing titles, tags and description")
    meta = generate_metadata(p)
    _, sources = _facts_used(p)
    cred = credits(project_id, rsum)
    meta["description"] = build_description(meta, sources, cred)
    meta["sources"] = sources
    meta["credits"] = cred
    report(0.7, "Making thumbnails")
    meta["thumbnails"] = make_thumbnails(p, meta["thumbnail_texts"])
    meta["thumbnail"] = meta["thumbnails"][0]["file"]
    meta["render_id"] = rid
    meta["handoff"] = None
    _store(project_id, meta)


def _store(project_id: int, meta: dict) -> None:
    with session_scope() as s:
        pr = s.get(StudioProject, project_id)
        outputs = json.loads(pr.outputs_json or "{}")
        outputs["package"] = meta
        pr.outputs_json = json.dumps(outputs)


def get(project_id: int) -> dict | None:
    with session_scope() as s:
        pr = s.get(StudioProject, project_id)
        return json.loads(pr.outputs_json or "{}").get("package")


def update(project_id: int, title: str | None = None, intro: str | None = None, hashtags: list[str] | None = None,
           tags: list[str] | None = None, pinned_comment: str | None = None, thumbnail: str | None = None) -> dict:
    meta = get(project_id)
    if meta is None:
        raise PackageError("Run Package first.")
    if title is not None:
        t = clean_title(title)
        if not t:
            raise PackageError("Title can't be empty.")
        meta["title"] = t
    if intro is not None:
        meta["intro"] = intro.strip()
    if hashtags is not None:
        meta["hashtags"] = clean_hashtags(hashtags)
    if tags is not None:
        meta["tags"] = clean_tags(tags)
    if pinned_comment is not None:
        meta["pinned_comment"] = pinned_comment.strip()
    if thumbnail is not None:
        if thumbnail not in [x["file"] for x in meta["thumbnails"]]:
            raise PackageError("Unknown thumbnail.")
        meta["thumbnail"] = thumbnail
    meta["description"] = build_description(meta, meta.get("sources", []), meta.get("credits", {}))
    _store(project_id, meta)
    return meta


def remake_thumbnail(project_id: int, index: int, text: str) -> dict:
    meta = get(project_id)
    if meta is None or not 0 <= index < len(meta["thumbnails"]):
        raise PackageError("Unknown thumbnail.")
    with session_scope() as s:
        p = s.get(StudioProject, project_id)
        s.expunge(p)
    srcs = thumbnail_sources(project_id)
    picks = [srcs[0], srcs[len(srcs) // 2], srcs[-1]] if len(srcs) >= 3 else (srcs * 3)[:3]
    d = ss.project_dir(project_id) / "package"
    old = meta["thumbnails"][index]["file"]
    name = f"thumb_{index + 1}_{hashlib.sha1((str(picks[index]) + text).encode()).hexdigest()[:6]}.jpg"
    make_thumbnail(picks[index], text.strip()[:40] or "WATCH", d / name, p.video_format)
    if old != name:
        (d / old).unlink(missing_ok=True)
    meta["thumbnails"][index] = {"file": name, "text": text.strip()[:40]}
    if meta["thumbnail"] == old:
        meta["thumbnail"] = name
    _store(project_id, meta)
    return meta


def handoff(project_id: int) -> int:
    """Send the checked render + package to the Videos queue (pending review). Returns the video id."""
    meta = get(project_id)
    if meta is None:
        raise PackageError("Run Package first.")
    if meta.get("handoff"):
        return meta["handoff"]["video_id"]
    rid, path, _ = _latest_render(project_id)
    with session_scope() as s:
        p = s.get(StudioProject, project_id)
        scr = json.loads(p.script_json or "{}")
        topic, category = p.topic, p.category
    work = ss.project_dir(project_id) / "package" / "handoff"
    work.mkdir(parents=True, exist_ok=True)
    mp4 = work / f"studio_p{project_id}_r{rid}.mp4"
    shutil.copy2(path, mp4)                                   # the import moves the file; keep the render in the project
    side = mp4.with_suffix(".json")
    side.write_text(json.dumps({
        "source_url": f"AI Information Video Studio project #{project_id}",
        "source_creator": "Original (AI Information Video Studio)",
        "permission_status": "original", "footage_type": "original",
        "permission_notes": "Original narration and edit; stock from Pexels/Pixabay, AI illustrations, licensed/own music (see description).",
        "topic": category, "context_notes": f"Studio project #{project_id}: {topic}",
        "transcript": scr.get("full_narration"), "original_commentary": scr.get("hook"),
        "title": meta["title"], "description": meta["description"], "tags": meta["tags"], "hashtags": meta["hashtags"],
    }), encoding="utf-8")
    try:
        vid = ingest.import_clip(mp4, side, generate=False, check_stable=False)
    except ingest.DuplicateClip as exc:
        vid = exc.existing_id
    if vid is None:
        raise PackageError("The Videos library didn't accept the file.")
    from ..models import Video

    with session_scope() as s:
        v = s.get(Video, vid)
        v.category_id = meta.get("category_id", "24")
    meta["handoff"] = {"video_id": vid, "render_id": rid}
    _store(project_id, meta)
    return vid
