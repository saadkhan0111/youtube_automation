"""Metadata sanitising / validation and the videos.insert request body."""
from __future__ import annotations

import re

from ..models import PublishMode, Upload, Video
from ..timeutil import to_rfc3339

MAX_TITLE = 100
MAX_DESCRIPTION_BYTES = 5000
MAX_TAGS_CHARS = 500
MAX_HASHTAGS = 5
ALLOWED_CATEGORIES = {"20": "Gaming", "24": "Entertainment", "22": "People & Blogs"}

_ANGLE = re.compile(r"[<>]")


def clean_title(title: str | None) -> str:
    t = _ANGLE.sub("", (title or "")).strip()
    t = re.sub(r"\s+", " ", t)
    return t[:MAX_TITLE].rstrip()


def clean_hashtags(tags: list[str] | None) -> list[str]:
    out, seen = [], set()
    for h in tags or []:
        h = re.sub(r"[^\w]", "", (h or "").lstrip("#"))
        if not h or h.lower() in seen:
            continue
        seen.add(h.lower())
        out.append("#" + h)
    return out[:MAX_HASHTAGS]


def clean_tags(tags: list[str] | None) -> list[str]:
    out, seen, total = [], set(), 0
    for t in tags or []:
        t = _ANGLE.sub("", (t or "")).replace(",", " ").strip().lstrip("#")
        t = re.sub(r"\s+", " ", t)
        if not t or t.lower() in seen or len(t) > 100:
            continue
        cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)  # YouTube counts quotes + separators
        if total + cost > MAX_TAGS_CHARS:
            break
        seen.add(t.lower())
        out.append(t)
        total += cost
    return out


def compose_description(description: str | None, hashtags: list[str]) -> str:
    d = _ANGLE.sub("", (description or "")).strip()
    missing = [h for h in hashtags if h.lower() not in d.lower()]
    if missing:
        d = (d + "\n\n" + " ".join(missing)).strip()
    b = d.encode("utf-8")
    if len(b) > MAX_DESCRIPTION_BYTES:
        d = b[:MAX_DESCRIPTION_BYTES].decode("utf-8", errors="ignore")
    return d


def validate_metadata(video: Video) -> list[str]:
    errors = []
    if not clean_title(video.title):
        errors.append("Title is required.")
    if video.title and len(video.title) > MAX_TITLE:
        errors.append(f"Title exceeds {MAX_TITLE} characters.")
    if video.title and _ANGLE.search(video.title):
        errors.append("Title must not contain < or >.")
    if video.description and len(video.description.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        errors.append("Description exceeds 5000 bytes.")
    if len(video.hashtags) > MAX_HASHTAGS:
        errors.append(f"Use at most {MAX_HASHTAGS} hashtags.")
    if video.category_id not in ALLOWED_CATEGORIES:
        errors.append("Unsupported category.")
    return errors


def build_upload_body(video: Video, up: Upload) -> dict:
    hashtags = clean_hashtags(video.hashtags)
    status: dict = {
        "selfDeclaredMadeForKids": False,
        "embeddable": True,
    }
    if up.publish_mode == PublishMode.SCHEDULED:
        # publishAt is only valid with privacyStatus=private on a never-published video.
        status["privacyStatus"] = "private"
        status["publishAt"] = to_rfc3339(up.scheduled_at)
    elif up.publish_mode == PublishMode.PUBLIC:
        status["privacyStatus"] = "public"
    elif up.publish_mode == PublishMode.UNLISTED:
        status["privacyStatus"] = "unlisted"
    else:
        status["privacyStatus"] = "private"
    return {
        "snippet": {
            "title": clean_title(video.title),
            "description": compose_description(video.description, hashtags),
            "tags": clean_tags(video.tags),
            "categoryId": video.category_id if video.category_id in ALLOWED_CATEGORIES else "20",
        },
        "status": status,
    }
