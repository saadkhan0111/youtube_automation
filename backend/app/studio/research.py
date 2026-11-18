"""Research engine (stage 1).

1. Fetch the user's reference URLs (primary material).
2. Web research with OpenAI web search (STUDIO_RESEARCH_MODEL): facts, sources, disputed claims, gaps.
   Every URL the search actually opened is collected.
3. Turn the notes into a structured report (STUDIO_STRUCTURE_MODEL, strict JSON schema).
4. Validate: only sources the search really visited (or the user provided) are kept; claims are linked to
   kept sources; an unsourced claim can never be "verified"; too little evidence fails the stage and asks
   for reference material instead of pretending research happened.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from ..db import session_scope
from . import llm, worker
from . import settings as ss
from .fetch import FetchedPage, fetch, normalize_url
from .models import StudioProject, StudioSource

log = logging.getLogger("ytshorts.studio.research")

SOURCE_TYPES = ["primary", "official", "publication", "research", "community", "other"]
FACT_STATUS = ["verified", "disputed", "uncertain", "opinion"]


# --------------------------------------------------------------------------- schema
class SourceOut(BaseModel):
    id: str
    url: str
    title: str
    publisher: str
    source_type: Literal["primary", "official", "publication", "research", "community", "other"]
    reliability: Literal["high", "medium", "low"]


class FactOut(BaseModel):
    id: str
    claim: str = Field(min_length=5)
    detail: str
    status: Literal["verified", "disputed", "uncertain", "opinion"]
    source_ids: list[str]
    why_interesting: str


class DisputedOut(BaseModel):
    claim: str
    explanation: str
    source_ids: list[str]


class ReportOut(BaseModel):
    summary: str
    best_angle: str
    sources: list[SourceOut]
    facts: list[FactOut]
    disputed: list[DisputedOut]
    gaps: list[str]


def _json_schema() -> dict:
    """Strict JSON schema for OpenAI structured outputs (every property required, no extras)."""
    src = {"type": "object", "additionalProperties": False,
           "required": ["id", "url", "title", "publisher", "source_type", "reliability"],
           "properties": {"id": {"type": "string"}, "url": {"type": "string"}, "title": {"type": "string"},
                          "publisher": {"type": "string"}, "source_type": {"type": "string", "enum": SOURCE_TYPES},
                          "reliability": {"type": "string", "enum": ["high", "medium", "low"]}}}
    fact = {"type": "object", "additionalProperties": False,
            "required": ["id", "claim", "detail", "status", "source_ids", "why_interesting"],
            "properties": {"id": {"type": "string"}, "claim": {"type": "string"}, "detail": {"type": "string"},
                           "status": {"type": "string", "enum": FACT_STATUS},
                           "source_ids": {"type": "array", "items": {"type": "string"}},
                           "why_interesting": {"type": "string"}}}
    disp = {"type": "object", "additionalProperties": False, "required": ["claim", "explanation", "source_ids"],
            "properties": {"claim": {"type": "string"}, "explanation": {"type": "string"},
                           "source_ids": {"type": "array", "items": {"type": "string"}}}}
    return {"type": "object", "additionalProperties": False,
            "required": ["summary", "best_angle", "sources", "facts", "disputed", "gaps"],
            "properties": {"summary": {"type": "string"}, "best_angle": {"type": "string"},
                           "sources": {"type": "array", "items": src}, "facts": {"type": "array", "items": fact},
                           "disputed": {"type": "array", "items": disp},
                           "gaps": {"type": "array", "items": {"type": "string"}}}}


# --------------------------------------------------------------------------- prompts
def fact_target(p: StudioProject) -> tuple[int, int]:
    minutes = p.target_duration / 60
    return (6, 10) if p.video_format == "short" else (max(12, int(minutes * 2.5)), max(20, int(minutes * 4)))


def research_prompt(p: StudioProject, refs: list[FetchedPage]) -> str:
    lo, hi = fact_target(p)
    ref_text = ""
    good = [r for r in refs if r.ok]
    if good:
        ref_text = "\n\nReference material provided by the creator (treat as primary sources; cite their URLs):\n" + "\n".join(
            f"--- {r.url} ({r.title or 'untitled'})\n{r.text[:4000]}" for r in good)
    return f"""Today's date is {date.today().isoformat()}.
You are a meticulous fact researcher for a {ss.CATEGORIES[p.category]} YouTube {'Short' if p.video_format == 'short' else 'video'} \
({p.target_duration} seconds) about:

  {p.topic}

Audience: {p.audience or 'general YouTube viewers'}.{f' Creator instructions: {p.instructions}' if p.instructions else ''}

Research the topic on the web and report {lo}-{hi} accurate, genuinely interesting facts a viewer may not know.
Rules:
- Prefer primary sources: official websites, developer/publisher/company announcements, official documentation,
  academic papers, government or museum sources. Then reputable publications. Use fan wikis, forums, Reddit or
  content farms only for clearly labelled community claims, never as the only support for a "fact".
- Give the exact source URL for every fact.
- Never invent facts, numbers, dates, quotes or events. If something cannot be verified, say so explicitly.
- Separately list rumours, leaks, disputed or unconfirmed claims, and explain why they are uncertain.
- Separate verified facts from opinions or speculation.
- List what you could not verify or find.{ref_text}"""


STRUCTURE_SYSTEM = """You convert research notes into a structured report. Use ONLY information and URLs that appear in the notes.
- sources: one entry per distinct URL used; ids S1, S2, ... Classify source_type (primary = the original creator/company/
  official documentation of the subject; official = other official bodies; publication = reputable media;
  research = academic; community = wikis, forums, fan sites, Reddit, YouTube comments; other) and reliability.
- facts: ids F1, F2, ...; claim = one short sentence; detail = supporting specifics; status:
  verified (supported by a cited primary/reputable source), disputed, uncertain (weak or single community source), opinion.
  source_ids must reference sources from your list. Never add facts that are not in the notes.
- disputed: rumours, leaks, conflicting or unconfirmed claims with an explanation.
- gaps: things the notes say could not be verified or were not found.
- best_angle: the most compelling angle for the video, based on the facts.
- summary: 2-3 sentences."""


# --------------------------------------------------------------------------- validation
class ResearchError(RuntimeError):
    pass


HEDGED = re.compile(r"\b(might|may|maybe|possibly|possible|perhaps|likely|unlikely|probably|rumou?r\w*|reportedly|"
                    r"allegedly|seems?|appears?|could|suggests?|speculat\w*|theor(y|ies|ized))\b", re.IGNORECASE)


def validate_report(raw: dict, allowed_urls: set[str], user_urls: set[str]) -> dict:
    """Pydantic-validate, drop sources the search never visited, re-link claims, enforce evidence rules."""
    try:
        rep = ReportOut.model_validate(raw)
    except ValidationError as exc:
        raise ResearchError(f"The research report had an invalid structure: {exc.errors()[:3]}") from exc

    kept, rejected, seen = [], [], {}
    for s in rep.sources:
        norm = normalize_url(s.url)
        if norm not in allowed_urls:
            rejected.append({"url": s.url, "reason": "not visited by the web search or provided by you"})
            continue
        if norm in seen:                       # duplicate URL under another id: map it to the first one
            seen[s.id] = seen[norm]
            continue
        d = s.model_dump()
        d["user_provided"] = norm in user_urls
        if d["user_provided"]:
            d["reliability"] = "high" if d["reliability"] == "medium" else d["reliability"]
        kept.append(d)
        seen[norm] = s.id
        seen[s.id] = s.id
    kept_ids = {s["id"] for s in kept}

    facts = []
    for f in rep.facts:
        d = f.model_dump()
        d["source_ids"] = sorted({seen.get(i, i) for i in d["source_ids"] if seen.get(i, i) in kept_ids})
        d["flags"] = []
        if not d["source_ids"]:
            d["flags"].append("no valid source")
            if d["status"] == "verified":
                d["status"] = "uncertain"
        else:
            types = {s["source_type"] for s in kept if s["id"] in d["source_ids"]}
            if d["status"] == "verified" and types <= {"community", "other"}:
                d["status"] = "uncertain"
                d["flags"].append("only community sources")
        if d["status"] == "verified" and HEDGED.search(d["claim"]):
            d["status"] = "uncertain"            # "might parody ..." is speculation, not a verified fact
            d["flags"].append("hedged wording")
        d["include"] = d["status"] in ("verified", "opinion")
        facts.append(d)

    disputed = []
    for x in rep.disputed:
        d = x.model_dump()
        d["source_ids"] = sorted({seen.get(i, i) for i in d["source_ids"] if seen.get(i, i) in kept_ids})
        disputed.append(d)

    verified = [f for f in facts if f["status"] == "verified"]
    if not kept:
        raise ResearchError("No usable sources were found. Add reference URLs (official pages, articles) and run research again.")
    if len(verified) < 3:
        raise ResearchError(f"Only {len(verified)} fact(s) could be verified from reliable sources. "
                            "Add reference URLs or broaden the topic, then run research again.")
    return {"summary": rep.summary, "best_angle": rep.best_angle, "sources": kept, "facts": facts,
            "disputed": disputed, "gaps": rep.gaps, "rejected_sources": rejected}


# --------------------------------------------------------------------------- stage handler
@worker.register("research")
def run(project_id: int, report) -> None:
    with session_scope() as s:
        p = s.get(StudioProject, project_id)
        s.expunge(p)
    refs: list[FetchedPage] = []
    if p.reference_urls:
        for i, u in enumerate(p.reference_urls):
            report(0.02 + 0.13 * i / len(p.reference_urls), f"Reading your reference {i + 1}/{len(p.reference_urls)}")
            refs.append(fetch(u))

    report(0.18, f"Searching the web ({ss.RESEARCH_MODEL}); this can take a minute")
    search_error = None
    try:
        web = llm.web_research(ss.RESEARCH_MODEL, research_prompt(p, refs), project_id)
    except Exception as exc:
        log.warning("web research failed: %s", exc)
        search_error = f"{exc.__class__.__name__}: {str(exc)[:200]}"
        web = None
    good_refs = [r for r in refs if r.ok]
    if web is None and not good_refs:
        raise ResearchError(f"Web search is unavailable ({search_error}) and no reference material could be read. "
                            "Add reference URLs or paste-able sources, then run research again.")

    notes = web.text if web else ""
    if not web:
        notes = "Web search was unavailable; only the creator's reference material below was used.\n" + "\n".join(
            f"--- {r.url}\n{r.text[:6000]}" for r in good_refs)
    allowed = {normalize_url(u) for u in (web.visited_urls if web else [])}
    user_urls = {normalize_url(r.url) for r in good_refs}
    allowed |= user_urls

    report(0.75, f"Organizing facts and sources ({ss.STRUCTURE_MODEL})")
    url_list = "\n".join(sorted(allowed))[:12000]
    raw = llm.chat_json(ss.STRUCTURE_MODEL, STRUCTURE_SYSTEM,
                        f"Topic: {p.topic}\n\nResearch notes:\n{notes}\n\nURLs that may be cited (use only these):\n{url_list}",
                        _json_schema(), name="research_report", project_id=project_id, stage="research")
    result = validate_report(raw, allowed, user_urls)
    result.update({
        "topic": p.topic,
        "search": {"performed": web is not None, "model": ss.RESEARCH_MODEL if web else None,
                   "search_calls": web.search_calls if web else 0, "pages_seen": len(allowed - user_urls),
                   "error": search_error},
        "references": [{"url": r.url, "ok": r.ok, "title": r.title, "error": r.error} for r in refs],
        "generated_at": date.today().isoformat(),
    })
    report(0.95, "Saving research")
    with session_scope() as s:
        p = s.get(StudioProject, project_id)
        p.research_json = json.dumps(result)
        for old in list(p.sources):
            s.delete(old)
        for src in result["sources"]:
            s.add(StudioSource(project_id=project_id, url=src["url"], title=src["title"], publisher=src["publisher"],
                               source_type=src["source_type"], reliability=src["reliability"],
                               user_provided=src["user_provided"], notes=src["id"]))


# --------------------------------------------------------------------------- user edits
class FactEdit(BaseModel):
    id: str
    claim: str | None = Field(None, min_length=5, max_length=500)
    detail: str | None = Field(None, max_length=2000)
    status: Literal["verified", "disputed", "uncertain", "opinion"] | None = None
    include: bool | None = None
    delete: bool = False


def apply_edits(research: dict, edits: list[FactEdit]) -> dict:
    by_id = {f["id"]: f for f in research["facts"]}
    for e in edits:
        f = by_id.get(e.id)
        if f is None:
            raise ResearchError(f"Unknown fact {e.id}")
        if e.delete:
            research["facts"] = [x for x in research["facts"] if x["id"] != e.id]
            continue
        if e.status == "verified" and not f["source_ids"]:
            raise ResearchError(f"{e.id} has no source, so it can't be marked verified.")
        for k in ("claim", "detail", "status", "include"):
            v = getattr(e, k)
            if v is not None:
                f[k] = v.strip() if isinstance(v, str) else v
        if e.status is not None and e.include is None:
            # a status change resets the default: only verified facts and opinions go into the script
            f["include"] = e.status in ("verified", "opinion")
        f["edited"] = True
    return research
