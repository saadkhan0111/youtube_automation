"""AI scriptwriter (stage 2).

Writes a spoken script from the INCLUDED research facts only, then checks it in code:
  * every content scene cites fact ids that exist and are included
  * every number/year in the narration appears in a cited fact (catches invented statistics)
  * no generic AI/YouTuber intros ("welcome back", "in this video", ...)
  * length estimated from word count and speaking pace; one automatic rewrite if it is far off target
  * no near-duplicate sentences (no repeating information to fill time)
Problems that remain after the rewrite are shown as warnings in the editor; nothing is hidden.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from ..clipper import settings as clip_settings
from ..db import session_scope
from . import llm, worker
from . import settings as ss
from .models import StudioProject

log = logging.getLogger("ytshorts.studio.script")

PURPOSES = ["hook", "setup", "point", "reveal", "transition", "payoff", "cta"]
CONTENT_PURPOSES = {"setup", "point", "reveal", "payoff"}
# Spoken words per second by narration style (before the voice's slight speed-up). Calibrated on real OpenAI TTS
# output: the energetic voice measured 2.45 words/s after processing (names, numbers and pauses slow it down).
PACE = {"energetic": 2.3, "conversational": 2.15, "dramatic": 1.9, "storyteller": 1.95, "authoritative": 2.05}
SCENE_GAP = 0.25            # pause between scenes (seconds)
LENGTH_TOLERANCE = 0.15     # +-15 % of the target duration is fine

BANNED = re.compile(r"\b(welcome (back )?to (my|the|our) channel|welcome back|hey (guys|everyone|there)|in (this|today'?s) video|"
                    r"without further ado|let'?s (just )?(dive|jump) (right )?in|smash (that|the) like|"
                    r"don'?t forget to (like|subscribe)|make sure (to|you) subscribe|before we (begin|start))\b", re.IGNORECASE)
NUMBER = re.compile(r"\b\d[\d,.:/]*\b")


# --------------------------------------------------------------------------- schema
class SceneOut(BaseModel):
    purpose: Literal["hook", "setup", "point", "reveal", "transition", "payoff", "cta"]
    narration: str = Field(min_length=3)
    fact_ids: list[str]
    emphasis: list[str]


class Thumbnail(BaseModel):
    concept: str
    text: str


class ScriptOut(BaseModel):
    title: str = Field(min_length=3, max_length=100)
    scenes: list[SceneOut] = Field(min_length=2)
    thumbnail: Thumbnail
    description: str


def _schema() -> dict:
    scene = {"type": "object", "additionalProperties": False, "required": ["purpose", "narration", "fact_ids", "emphasis"],
             "properties": {"purpose": {"type": "string", "enum": PURPOSES}, "narration": {"type": "string"},
                            "fact_ids": {"type": "array", "items": {"type": "string"}},
                            "emphasis": {"type": "array", "items": {"type": "string"}}}}
    return {"type": "object", "additionalProperties": False, "required": ["title", "scenes", "thumbnail", "description"],
            "properties": {"title": {"type": "string"}, "scenes": {"type": "array", "items": scene},
                           "thumbnail": {"type": "object", "additionalProperties": False, "required": ["concept", "text"],
                                         "properties": {"concept": {"type": "string"}, "text": {"type": "string"}}},
                           "description": {"type": "string"}}}


def _scene_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["scene"],
            "properties": {"scene": _schema()["properties"]["scenes"]["items"]}}


# --------------------------------------------------------------------------- pacing
def words(text: str) -> int:
    return len(re.findall(r"[\w'’-]+", text))


def pace(p: StudioProject) -> float:
    return PACE.get(p.narration_style, 2.6) * clip_settings.VOICE_TEMPO


def scene_seconds(text: str, wps: float) -> float:
    return round(words(text) / wps + SCENE_GAP, 1)


def target_words(p: StudioProject) -> int:
    return int(p.target_duration * PACE.get(p.narration_style, 2.6) * clip_settings.VOICE_TEMPO)


def scene_range(p: StudioProject) -> tuple[int, int]:
    if p.video_format == "short":
        return (5, 9)
    return (max(8, p.target_duration // 40), max(12, p.target_duration // 20))


# --------------------------------------------------------------------------- prompts
STYLE_NOTES = {
    "viral": "Viral & energetic: punchy, fast, surprising; short sentences; every line earns its place.",
    "documentary": "Cinematic documentary: atmospheric and dramatic; vivid imagery; measured build-up; emotional payoff.",
    "explainer": "Educational explainer: crystal clear; one idea per scene; simple analogies; numbers made tangible.",
    "countdown": "Top-N countdown: number the items from N down to 1 (best last); tease what's coming; quick reveals.",
}


def system_prompt(p: StudioProject) -> str:
    lang = ss.LANGUAGES.get(p.language, "English")
    lo, hi = scene_range(p)
    return f"""You are an elite YouTube scriptwriter. Write narration that will be SPOKEN by a {ss.NARRATION_STYLES[p.narration_style].lower()} \
AI voice, in {lang}. Style: {STYLE_NOTES[p.editing_style]}

Rules:
- Scene 1 (purpose "hook") must grab attention in the first 3 seconds: a surprising claim, question or contradiction. No greeting.
- Never use generic intros or filler: no "welcome back", "in this video", "hey guys", "let's dive in", "without further ado",
  no "don't forget to like and subscribe".
- Natural spoken language: short sentences, contractions, rhythm, varied sentence length, the occasional rhetorical question.
- Use ONLY the facts provided. Each setup/point/reveal/payoff scene lists the fact_ids it uses. Never add facts, numbers, dates,
  names, quotes or events that are not in the facts. Write numbers as digits only if they appear in the facts.
- Items in the "unconfirmed" list may only be mentioned as unconfirmed (e.g. "this hasn't been confirmed, but...").
- Build momentum: end scenes with a pull into the next one. Use transitions between sections.
- Never repeat information to fill time. If the facts run out, end sooner.
- Finish with a satisfying payoff or twist, then a short CTA scene specific to the topic (e.g. "Which detail did you miss?").
- Write {lo}-{hi} scenes. Each scene is 1-3 sentences.
- emphasis: 1-3 key words from the scene's narration to highlight on screen.
- title: natural, curiosity-driven, accurate, max 70 characters, no clickbait lies.
- thumbnail: concept = what the image shows; text = max 4 words.
- description: 2 short paragraphs about what the video covers (no hashtags, no source list - sources are added automatically)."""


def facts_block(research: dict) -> tuple[str, list[dict]]:
    facts = [f for f in research["facts"] if f.get("include")]
    lines = [f"{f['id']}: {f['claim']} ({f['detail']})" + (f" [status: {f['status']}]" if f["status"] != "verified" else "")
             for f in facts]
    unconfirmed = [f"- {d['claim']}: {d['explanation']}" for d in research.get("disputed", [])]
    text = "Facts:\n" + "\n".join(lines)
    if unconfirmed:
        text += "\n\nUnconfirmed (mention only as unconfirmed, or skip):\n" + "\n".join(unconfirmed)
    return text, facts


def user_prompt(p: StudioProject, research: dict) -> str:
    text, _ = facts_block(research)
    return (f"Topic: {p.topic}\nAudience: {p.audience or 'general YouTube viewers'}\n"
            f"Format: {'YouTube Short (vertical)' if p.video_format == 'short' else 'long-form YouTube video'}\n"
            f"Target length: about {target_words(p)} spoken words (~{p.target_duration} seconds).\n"
            f"Angle: {research.get('best_angle', '')}\n"
            + (f"Creator instructions: {p.instructions}\n" if p.instructions else "") + "\n" + text)


# --------------------------------------------------------------------------- checks
def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 12]


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"\w+", s.lower())) - {"the", "a", "an", "and", "of", "to", "in", "is", "it", "this", "that"}


def check(script: dict, p: StudioProject, research: dict) -> list[dict]:
    """Return warnings: [{scene (index or None), kind, message}]. Also updates per-scene seconds and totals."""
    wps = pace(p)
    facts = {f["id"]: f for f in research["facts"]}
    included = {fid for fid, f in facts.items() if f.get("include")}
    warnings: list[dict] = []
    all_sentences: list[tuple[int, str]] = []
    total = 0.0
    for i, sc in enumerate(script["scenes"]):
        sc["seconds"] = scene_seconds(sc["narration"], wps)
        total += sc["seconds"]
        bad_ids = [x for x in sc["fact_ids"] if x not in included]
        if bad_ids:
            warnings.append({"scene": i, "kind": "facts", "message": f"Cites unknown or excluded facts: {', '.join(bad_ids)}"})
        if sc["purpose"] in CONTENT_PURPOSES and not [x for x in sc["fact_ids"] if x in included]:
            warnings.append({"scene": i, "kind": "facts", "message": "Makes a point without citing a research fact."})
        # numbers may come from the cited facts or from the topic itself ("GTA 6")
        cited_text = " ".join(f"{facts[x]['claim']} {facts[x]['detail']}" for x in sc["fact_ids"] if x in facts) + " " + p.topic
        for num in NUMBER.findall(sc["narration"]):
            core = num.rstrip(".,:/")
            if p.editing_style == "countdown" and core.isdigit() and int(core) <= 10:
                continue                         # "Number 3" is structure, not a statistic
            if core and core not in cited_text:
                warnings.append({"scene": i, "kind": "number",
                                 "message": f'"{core}" does not appear in the cited facts. Check it or remove it.'})
        m = BANNED.search(sc["narration"])
        if m:
            warnings.append({"scene": i, "kind": "style", "message": f'Generic phrase: "{m.group(0)}"'})
        all_sentences += [(i, s) for s in _sentences(sc["narration"])]
    for a in range(len(all_sentences)):
        for b in range(a + 1, len(all_sentences)):
            ta, tb = _tokens(all_sentences[a][1]), _tokens(all_sentences[b][1])
            if ta and tb and len(ta & tb) / len(ta | tb) > 0.7:
                warnings.append({"scene": all_sentences[b][0], "kind": "repetition",
                                 "message": f'Repeats scene {all_sentences[a][0] + 1}: "{all_sentences[b][1][:60]}..."'})
    if script["scenes"][0]["purpose"] != "hook":
        warnings.append({"scene": 0, "kind": "structure", "message": "The first scene should be the hook."})
    if p.video_format == "short":
        first = (_sentences(script["scenes"][0]["narration"]) or [script["scenes"][0]["narration"]])[0]
        secs = round(words(first) / wps, 1)
        if secs > 4.0:
            warnings.append({"scene": 0, "kind": "structure",
                             "message": f"The opening sentence takes {secs}s; a Short's hook should land within ~3 s."})
    total = round(total, 1)
    script["estimated_seconds"] = total
    script["word_count"] = sum(words(s["narration"]) for s in script["scenes"])
    off = (total - p.target_duration) / p.target_duration
    if abs(off) > LENGTH_TOLERANCE:
        warnings.append({"scene": None, "kind": "length",
                         "message": f"Estimated {total:.0f}s vs target {p.target_duration}s ({off:+.0%})."})
    script["full_narration"] = "\n\n".join(s["narration"] for s in script["scenes"])
    script["hook"] = script["scenes"][0]["narration"]
    return warnings


def validate(raw: dict) -> dict:
    try:
        out = ScriptOut.model_validate(raw).model_dump()
    except ValidationError as exc:
        raise ScriptError(f"The script had an invalid structure: {exc.errors()[:3]}") from exc
    for sc in out["scenes"]:
        sc["narration"] = re.sub(r"\s+", " ", sc["narration"]).strip()
        sc["emphasis"] = [e for e in sc["emphasis"] if e and e.lower() in sc["narration"].lower()][:3]
    out["title"] = out["title"].strip()
    return out


class ScriptError(RuntimeError):
    pass


def _ids(script: dict) -> dict:
    for i, sc in enumerate(script["scenes"], 1):
        sc["id"] = sc.get("id") or f"S{i}"
    return script


# --------------------------------------------------------------------------- generation
def generate(p: StudioProject, research: dict, project_id: int | None = None) -> tuple[dict, list[dict]]:
    system = system_prompt(p)
    user = user_prompt(p, research)
    raw = llm.chat_json(ss.SCRIPT_MODEL, system, user, _schema(), name="script", project_id=project_id, stage="script")
    script = validate(raw)
    warnings = check(script, p, research)
    fixable = [w for w in warnings if w["kind"] in ("length", "number", "style", "repetition", "facts")]
    if fixable:
        feedback = "\n".join(f"- {'scene ' + str(w['scene'] + 1) + ': ' if w['scene'] is not None else ''}{w['message']}"
                             for w in fixable)
        raw = llm.chat_json(ss.SCRIPT_MODEL, system,
                            user + f"\n\nYour previous draft:\n{json.dumps({k: script[k] for k in ('title', 'scenes')})}\n\n"
                                   f"Rewrite it and fix these problems:\n{feedback}",
                            _schema(), name="script", project_id=project_id, stage="script")
        second = validate(raw)
        w2 = check(second, p, research)
        # keep whichever draft has fewer problems
        if len(w2) <= len(warnings):
            script, warnings = second, w2
    return _ids(script), warnings


def regenerate_scene(p: StudioProject, research: dict, script: dict, index: int, instruction: str | None,
                     project_id: int | None = None) -> dict:
    sc = script["scenes"][index]
    context = "\n".join(f"{i + 1}. [{s['purpose']}] {s['narration']}" for i, s in enumerate(script["scenes"]))
    user = (user_prompt(p, research) + f"\n\nCurrent script:\n{context}\n\nRewrite ONLY scene {index + 1} "
            f"(purpose: {sc['purpose']}). Keep it consistent with the scenes before and after it."
            + (f"\nCreator's instruction for this scene: {instruction}" if instruction else ""))
    raw = llm.chat_json(ss.SCRIPT_MODEL, system_prompt(p), user, _scene_schema(), name="scene",
                        project_id=project_id, stage="script")
    try:
        new = SceneOut.model_validate(raw["scene"]).model_dump()
    except (ValidationError, KeyError) as exc:
        raise ScriptError(f"The rewritten scene had an invalid structure: {exc}") from exc
    new["narration"] = re.sub(r"\s+", " ", new["narration"]).strip()
    new["emphasis"] = [e for e in new["emphasis"] if e.lower() in new["narration"].lower()][:3]
    new["id"] = sc["id"]
    return new


@worker.register("script")
def run(project_id: int, report) -> None:
    with session_scope() as s:
        p = s.get(StudioProject, project_id)
        s.expunge(p)
    if not p.research_json:
        raise ScriptError("No approved research. Run and approve Research first.")
    research = json.loads(p.research_json)
    if not any(f.get("include") for f in research["facts"]):
        raise ScriptError("No research facts are included. Include at least one fact.")
    report(0.1, f"Writing the script ({ss.SCRIPT_MODEL})")
    script, warnings = generate(p, research, project_id)
    report(0.9, "Checking facts, numbers and length")
    script["warnings"] = warnings
    script["model"] = ss.SCRIPT_MODEL
    with session_scope() as s:
        s.get(StudioProject, project_id).script_json = json.dumps(script)
