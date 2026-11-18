"""Metadata generation (title, description, hashtags, tags, category) with Claude.

The model never sees the video itself, only the context the clipper provides
(transcript, notes, source info). The prompt forbids inventing facts beyond
that context. Without ANTHROPIC_API_KEY a conservative template is used.
"""
from __future__ import annotations

import json
import logging
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..events import record_event
from ..models import Video
from .metadata_rules import ALLOWED_CATEGORIES, clean_hashtags, clean_tags, clean_title

log = logging.getLogger("ytshorts.metadata")

# Tests can replace: fn(prompt: str) -> dict
ai_generator = None

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "YouTube title, max 90 characters"},
        "description": {"type": "string", "description": "2-4 short paragraphs, no hashtags"},
        "hashtags": {"type": "array", "items": {"type": "string"}, "description": "3-5 hashtags"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "5-15 search tags"},
        "category_id": {"type": "string", "enum": sorted(ALLOWED_CATEGORIES)},
        "notes": {"type": "string", "description": "Anything the reviewer should double-check"},
    },
    "required": ["title", "description", "hashtags", "tags", "category_id", "notes"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You write YouTube Shorts metadata for a gaming channel focused on GTA 6, GTA 5 vs GTA 6 comparisons, streamer reactions, funny gameplay moments, discoveries, and game-mechanics analysis.

Rules:
- Base every claim ONLY on the clip context provided. You cannot see the video. Never invent gameplay facts, features, release dates, prices, leaks, or quotes that are not in the context.
- Titles: natural and interesting, under 90 characters, no ALL-CAPS shouting, no misleading clickbait, no promises the clip doesn't deliver. Must differ from the channel's recent titles listed.
- Description: 2-4 short paragraphs describing what the clip actually shows and the channel's own commentary/angle. Credit the source creator when one is given (e.g. "Clip: <creator>"), and include the source URL if provided. Do not put hashtags in the description; they are added separately.
- Hashtags: 3-5 relevant ones (e.g. #GTA6). No unrelated trending tags.
- Tags: 5-15 specific, relevant search phrases. No irrelevant or misleading tags.
- category_id: "20" (Gaming) unless the clip is clearly not gameplay.
- Do not mention "#Shorts" as a way to make it a Short; it does not affect classification.
- notes: list anything uncertain the human reviewer should verify (or "none")."""


def _build_prompt(video: Video, recent_titles: list[str]) -> str:
    ctx = {
        "topic": video.topic,
        "context_notes": video.context_notes,
        "original_commentary": video.original_commentary,
        "transcript_excerpt": (video.transcript or "")[:6000] or None,
        "source_creator": video.source_creator,
        "source_url": video.original_source_url,
        "clip_start_seconds": video.clip_start_time,
        "clip_end_seconds": video.clip_end_time,
        "duration_seconds": video.duration,
    }
    ctx = {k: v for k, v in ctx.items() if v not in (None, "")}
    return (
        "Clip context (JSON):\n" + json.dumps(ctx, ensure_ascii=False, indent=2)
        + "\n\nRecent titles on this channel (do not duplicate):\n"
        + ("\n".join(f"- {t}" for t in recent_titles) or "- (none)")
        + "\n\nGenerate the metadata."
    )


def _call_claude(prompt: str) -> dict:
    if ai_generator is not None:
        return ai_generator(prompt)
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        # No Claude key: use the OpenAI key instead (same prompt and schema).
        from ..clipper import ai as openai_ai

        return openai_ai.generate_json(SYSTEM_PROMPT, prompt, SCHEMA, name="metadata")
    import anthropic

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=settings.anthropic_model,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        # If a request is declined by a safety classifier, the API retries it on a fallback model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("The model declined to generate metadata for this clip.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Metadata response was truncated.")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise RuntimeError("Empty metadata response.")
    return json.loads(text)


def _template(video: Video) -> dict:
    topic = (video.topic or "GTA 6").strip()
    first = ((video.context_notes or video.original_commentary or "").strip().split(".")[0])[:70]
    title = f"{topic}: {first}" if first else f"{topic} clip"
    desc_parts = []
    if video.context_notes:
        desc_parts.append(video.context_notes.strip())
    if video.original_commentary:
        desc_parts.append(video.original_commentary.strip())
    if video.source_creator or video.original_source_url:
        desc_parts.append("Source: " + " ".join(x for x in [video.source_creator, video.original_source_url] if x))
    return {
        "title": title,
        "description": "\n\n".join(desc_parts) or f"{topic} short.",
        "hashtags": ["#GTA6", "#Gaming"],
        "tags": ["GTA 6", "GTA VI", "Grand Theft Auto 6", "gaming", topic],
        "category_id": "20",
        "notes": "Template metadata (AI disabled or unavailable). Please review and edit.",
    }


def recent_titles(db: Session, exclude_id: int | None = None, limit: int = 50) -> list[str]:
    q = select(Video.title).where(Video.title.is_not(None)).order_by(Video.id.desc()).limit(limit)
    if exclude_id:
        q = q.where(Video.id != exclude_id)
    return [t for t in db.scalars(q) if t]


def generate_metadata(db: Session, video: Video, actor: str = "system") -> dict:
    """Generate and store metadata on `video`. Returns the generated fields + source."""
    titles = recent_titles(db, exclude_id=video.id)
    lower_titles = {t.lower() for t in titles}
    source = "template"
    data = None
    use_ai = settings.metadata_ai_enabled and (ai_generator is not None or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("OPENAI_API_KEY"))
    if use_ai:
        prompt = _build_prompt(video, titles)
        for attempt in range(2):
            try:
                data = _call_claude(prompt)
                source = "ai"
            except Exception as exc:
                log.warning("AI metadata generation failed: %s", exc)
                record_event("metadata", f"AI metadata generation failed ({exc.__class__.__name__}: {str(exc)[:200]}); using template", level="warning", video_id=video.id, actor=actor)
                data = None
                break
            if clean_title(data.get("title")).lower() not in lower_titles:
                break
            prompt += f"\n\nThe title \"{data.get('title')}\" duplicates an existing title. Write a different one."
    if data is None:
        data = _template(video)
        source = "template"

    title = clean_title(data.get("title"))
    if title.lower() in lower_titles:
        n = 2
        while f"{title} (Part {n})".lower() in lower_titles:
            n += 1
        title = clean_title(f"{title[:88]} (Part {n})")
    video.title = title
    video.description = (data.get("description") or "").strip()
    video.hashtags = clean_hashtags(data.get("hashtags"))
    video.tags = clean_tags(data.get("tags"))
    video.category_id = data.get("category_id") if data.get("category_id") in ALLOWED_CATEGORIES else "20"
    video.metadata_source = source
    notes = (data.get("notes") or "").strip()
    if notes and notes.lower() != "none":
        video.review_notes = f"Metadata notes: {notes}"
    record_event("metadata", f"Metadata generated ({source})", video_id=video.id, actor=actor)
    return {"source": source, "title": video.title, "notes": notes}
