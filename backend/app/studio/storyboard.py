"""AI storyboard generator (stage 3): turns the approved script into shot-level visual plans.

Each script scene gets 1-5 shots. Narration is copied from the script (never rewritten here); durations come
from the script's pacing. The model's JSON is validated with Pydantic and checked in code:
  * every script scene is covered exactly once, in order (one automatic retry with feedback, else the stage fails)
  * shot lengths are compared with the editing style's pacing
  * graphic shots carry their text; footage/image shots carry stock search keywords
  * AI images may not be described as real photos, real gameplay or authentic historical pictures
  * numbers shown on screen must appear in the narration or the research facts
"""
from __future__ import annotations

import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from ..db import session_scope
from . import llm, worker
from . import settings as ss
from . import styles as st
from .models import StudioProject, StudioScene

log = logging.getLogger("ytshorts.studio.storyboard")

FAKE_REAL = re.compile(r"\b(real (photo|photograph|footage|picture)|actual (footage|photo|gameplay)|screenshot|"
                       r"historical photo(graph)?|authentic (photo|footage|image)|archival photo|official (screenshot|footage))\b",
                       re.IGNORECASE)
NUMBER = re.compile(r"\b\d[\d,.:/]*\b")
NEGATION = re.compile(r"\b(not|no|never|isn'?t|without)\b[\w\s]*$")
# an AI image that would portray a specific real person (likeness, mugshot, "a nod to" someone)
LIKENESS = re.compile(r"\b(mugshot|look-?alike|likeness|resembl\w*|portrait of|nod to|nod|based on (the )?real|modeled on|"
                      r"inspired by (the )?real (man|woman|person|guy))\b", re.IGNORECASE)


class StoryboardError(RuntimeError):
    pass


def claims_real(text: str) -> bool:
    """True if the text presents an image as real ("real photo of ..."), ignoring negations ("not a real photo")."""
    for m in FAKE_REAL.finditer(text):
        before = text[max(0, m.start() - 14):m.start()].lower()
        if not NEGATION.search(before):
            return True
    return False


# --------------------------------------------------------------------------- schema
class Graphic(BaseModel):
    kind: Literal["none", "title_card", "number_reveal", "stat_callout", "ranking_card", "comparison", "quote_card",
                  "timeline", "map_pin", "text_punch"]
    text: str
    subtext: str


class Shot(BaseModel):
    visual_type: Literal["stock_video", "stock_image", "ai_image", "gameplay", "user_footage", "graphic"]
    visual_description: str = Field(min_length=5)
    search_keywords: list[str]
    camera_movement: Literal["static", "slow_push_in", "slow_pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down",
                             "zoom_punch", "parallax", "handheld_drift"]
    effects: list[Literal["film_grain", "vignette", "color_pop", "light_leak", "glow", "shake", "speed_ramp", "blur_in",
                          "chromatic_aberration", "letterbox"]]
    share: float = Field(gt=0, le=1)
    graphic: Graphic


class SceneBoard(BaseModel):
    scene_id: str
    shots: list[Shot] = Field(min_length=1, max_length=5)
    on_screen_text: str
    animation: str
    sound_effects: list[Literal["whoosh", "impact", "riser", "pop", "ding", "swoosh_soft", "glitch", "boom", "click", "none"]]
    music_mood: Literal["energetic", "upbeat", "epic", "suspense", "mysterious", "dark", "emotional", "chill"]
    transition_out: Literal["cut", "fast_cut", "crossfade", "fade_black", "whip_pan", "zoom_through", "flash", "slide", "glitch"]


class Overall(BaseModel):
    music_mood: Literal["energetic", "upbeat", "epic", "suspense", "mysterious", "dark", "emotional", "chill"]
    visual_style: str = Field(min_length=5)     # one consistent look for AI images
    color_grade: str


class BoardOut(BaseModel):
    overall: Overall
    scenes: list[SceneBoard] = Field(min_length=1)


def _enum(values: list[str]) -> dict:
    return {"type": "string", "enum": values}


def _schema() -> dict:
    graphic = {"type": "object", "additionalProperties": False, "required": ["kind", "text", "subtext"],
               "properties": {"kind": _enum(["none"] + st.GRAPHIC_KINDS), "text": {"type": "string"},
                              "subtext": {"type": "string"}}}
    shot = {"type": "object", "additionalProperties": False,
            "required": ["visual_type", "visual_description", "search_keywords", "camera_movement", "effects", "share", "graphic"],
            "properties": {"visual_type": _enum(st.VISUAL_TYPES), "visual_description": {"type": "string"},
                           "search_keywords": {"type": "array", "items": {"type": "string"}},
                           "camera_movement": _enum(st.CAMERA_MOVES),
                           "effects": {"type": "array", "items": _enum(st.EFFECTS)},
                           "share": {"type": "number"}, "graphic": graphic}}
    scene = {"type": "object", "additionalProperties": False,
             "required": ["scene_id", "shots", "on_screen_text", "animation", "sound_effects", "music_mood", "transition_out"],
             "properties": {"scene_id": {"type": "string"}, "shots": {"type": "array", "items": shot},
                            "on_screen_text": {"type": "string"}, "animation": {"type": "string"},
                            "sound_effects": {"type": "array", "items": _enum(st.SOUND_EFFECTS)},
                            "music_mood": _enum(st.MUSIC_MOODS), "transition_out": _enum(st.TRANSITIONS)}}
    overall = {"type": "object", "additionalProperties": False, "required": ["music_mood", "visual_style", "color_grade"],
               "properties": {"music_mood": _enum(st.MUSIC_MOODS), "visual_style": {"type": "string"},
                              "color_grade": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False, "required": ["overall", "scenes"],
            "properties": {"overall": overall, "scenes": {"type": "array", "items": scene}}}


def _scene_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["scene"],
            "properties": {"scene": _schema()["properties"]["scenes"]["items"]}}


# --------------------------------------------------------------------------- prompts
def gameplay_available() -> bool:
    """Does the creator have their own gameplay in the clipper library?"""
    from ..models import ClipProject, Video

    with session_scope() as s:
        return s.query(Video).count() > 0 or s.query(ClipProject).count() > 0


def system_prompt(p: StudioProject) -> str:
    cfg = st.style(p.editing_style)
    lo, hi = cfg["shot_seconds"]
    aspect = "vertical 9:16" if p.video_format == "short" else "horizontal 16:9"
    gameplay = ("The creator HAS their own gameplay clips: use visual_type 'gameplay' when the narration describes "
                "gameplay." if gameplay_available() else
                "The creator has no gameplay footage: do not use visual_type 'gameplay'.")
    return f"""You are a senior video editor storyboarding a {aspect} {ss.CATEGORIES[p.category]} video.
Style: {ss.EDITING_STYLES[p.editing_style]}. {cfg['brief']}
Each shot should last {lo}-{hi} seconds, so split longer scenes into several shots (max 5 per scene; share = fraction of the scene's time).

Visuals must match the narration literally: show exactly what the voice is talking about at that moment.
Choose visual_type in this priority:
1. user_footage - only if the creator mentioned their own footage in the instructions.
2. stock_video / stock_image - real-world places, objects, people doing things, technology, nature, cities.
   search_keywords: 2-5 concrete visual English phrases a stock site understands (e.g. "miami aerial night", "police car lights").
3. gameplay - {gameplay}
4. ai_image - things stock can't show (game worlds, fictional or historical scenes, concepts). Describe it as an ILLUSTRATION
   in the project's visual style. Never describe an AI image as a real photo, screenshot, real gameplay or authentic historical picture.
   Never let an AI image portray or resemble a real, identifiable person (no likenesses, mugshots or "nods" to a real
   person's look): show the place, object or idea instead, or an anonymous silhouette. Avoid text inside AI images; if a
   sign or banner is essential, quote its exact words ("a sign reading 'Kelly County'") and keep it to a few words.
5. graphic - numbers, stats, rankings, comparisons, quotes, timelines, key-word punches. Put the exact words in graphic.text
   (short!) and graphic.subtext. Only use numbers that appear in the narration. For non-graphic shots set graphic.kind "none".
{'Countdown: every numbered item opens with a ranking_card graphic (text = the number, subtext = the item name).' if p.editing_style == 'countdown' else ''}
Camera movement, effects, sound effects and transitions should follow the style and the meaning of the moment: impacts on
reveals, risers before payoffs, whooshes on fast transitions. Don't add effects without a reason.
on_screen_text: a short text overlay (or empty). animation: how text/graphics animate in.
overall.visual_style: ONE consistent look for every AI image (e.g. "cinematic digital painting, neon Miami palette").
Return one storyboard scene per script scene, same scene_id, same order."""


def user_prompt(p: StudioProject, script: dict) -> str:
    lines = [f"{sc['id']} [{sc['purpose']}, {sc.get('seconds', 5)}s, highlight: {', '.join(sc.get('emphasis', []))}]: {sc['narration']}"
             for sc in script["scenes"]]
    return (f"Topic: {p.topic}\nTitle: {script['title']}\n"
            + (f"Creator instructions: {p.instructions}\n" if p.instructions else "")
            + "\nScript scenes:\n" + "\n".join(lines))


# --------------------------------------------------------------------------- validation
def _shot_seconds(scene_seconds: float, shots: list[dict]) -> None:
    total = sum(s["share"] for s in shots) or 1.0
    for s in shots:
        s["share"] = round(s["share"] / total, 3)
        s["seconds"] = round(scene_seconds * s["share"], 2)


def check_scene(board: dict, sc: dict, p: StudioProject, allowed_numbers: str) -> list[str]:
    cfg = st.style(p.editing_style)
    lo, hi = cfg["shot_seconds"]
    warnings = []
    for i, shot in enumerate(board["shots"], 1):
        g = shot["graphic"]
        if shot["visual_type"] == "graphic":
            if g["kind"] == "none" or not g["text"].strip():
                warnings.append(f"Shot {i} is a graphic but has no graphic type/text.")
        else:
            if g["kind"] != "none":
                shot["graphic"] = {"kind": "none", "text": "", "subtext": ""}
            if shot["visual_type"] in ("stock_video", "stock_image") and len([k for k in shot["search_keywords"] if k.strip()]) < 2:
                warnings.append(f"Shot {i} needs at least 2 search keywords.")
        if shot["visual_type"] == "ai_image" and claims_real(shot["visual_description"]):
            warnings.append(f"Shot {i}: an AI image may not be described as a real photo/footage/screenshot.")
        if shot["visual_type"] == "ai_image" and LIKENESS.search(shot["visual_description"]):
            warnings.append(f"Shot {i}: an AI image may not portray or resemble a real person. Show the place/object/idea or a silhouette.")
        if shot["visual_type"] == "gameplay" and not gameplay_available():
            warnings.append(f"Shot {i} wants gameplay footage, but there is none in your library.")
        if shot["seconds"] > hi * 1.35:
            warnings.append(f"Shot {i} lasts {shot['seconds']}s; this style changes visuals every {lo}-{hi}s.")
        if shot["seconds"] < 0.8:
            warnings.append(f"Shot {i} lasts only {shot['seconds']}s.")
        for num in NUMBER.findall(f"{g['text']} {g['subtext']}"):
            core = num.rstrip(".,:/")
            if core and core not in allowed_numbers and not (p.editing_style == "countdown" and core.isdigit() and int(core) <= 10):
                warnings.append(f'Shot {i} shows "{core}", which is not in the narration or facts.')
    for num in NUMBER.findall(board["on_screen_text"]):
        core = num.rstrip(".,:/")
        if core and core not in allowed_numbers:
            warnings.append(f'On-screen text shows "{core}", which is not in the narration or facts.')
    if p.editing_style == "countdown" and re.search(r"\b(number|#)\s*\d+", sc["narration"], re.IGNORECASE) \
            and not any(s["graphic"]["kind"] == "ranking_card" for s in board["shots"]):
        warnings.append("Countdown item without a ranking card.")
    return warnings


def validate(raw: dict, p: StudioProject, script: dict, research: dict | None) -> dict:
    try:
        out = BoardOut.model_validate(raw).model_dump()
    except ValidationError as exc:
        raise StoryboardError(f"The storyboard had an invalid structure: {exc.errors()[:3]}") from exc
    want = [sc["id"] for sc in script["scenes"]]
    got = [b["scene_id"] for b in out["scenes"]]
    if got != want:
        missing = [x for x in want if x not in got]
        extra = [x for x in got if x not in want]
        raise StoryboardError(f"Storyboard scenes don't match the script (missing {missing or '-'}, unexpected {extra or '-'}, "
                              f"order {'ok' if not missing and not extra else 'n/a'}).")
    facts_text = " ".join(f"{f['claim']} {f['detail']}" for f in (research or {}).get("facts", []))
    for board, sc in zip(out["scenes"], script["scenes"]):
        board["narration"] = sc["narration"]           # copied, never rewritten
        board["subtitle_text"] = sc["narration"]
        board["caption_style"] = st.style(p.editing_style)["caption_style"]
        board["seconds"] = sc.get("seconds", 5.0)
        board["purpose"] = sc["purpose"]
        board["emphasis"] = sc.get("emphasis", [])
        _shot_seconds(board["seconds"], board["shots"])
        board["warnings"] = check_scene(board, sc, p, f"{sc['narration']} {facts_text} {p.topic}")
    out["total_seconds"] = round(sum(b["seconds"] for b in out["scenes"]), 1)
    out["shot_count"] = sum(len(b["shots"]) for b in out["scenes"])
    return out


# --------------------------------------------------------------------------- generation
def generate(p: StudioProject, script: dict, research: dict | None, project_id: int | None = None) -> dict:
    system, user = system_prompt(p), user_prompt(p, script)
    raw = llm.chat_json(ss.STORYBOARD_MODEL, system, user, _schema(), name="storyboard", project_id=project_id, stage="storyboard")
    try:
        return validate(raw, p, script, research)
    except StoryboardError as exc:
        log.warning("storyboard retry: %s", exc)
        raw = llm.chat_json(ss.STORYBOARD_MODEL, system,
                            user + f"\n\nYour previous storyboard was rejected: {exc}\nReturn exactly one scene per script scene, "
                                   "same ids, same order.",
                            _schema(), name="storyboard", project_id=project_id, stage="storyboard")
        return validate(raw, p, script, research)


def regenerate_scene(p: StudioProject, script: dict, research: dict | None, board: dict, scene_id: str,
                     instruction: str | None, project_id: int | None = None) -> dict:
    idx = next(i for i, sc in enumerate(script["scenes"]) if sc["id"] == scene_id)
    sc = script["scenes"][idx]
    user = (user_prompt(p, script) + f"\n\nOverall visual style: {board['overall']['visual_style']}\n"
            f"Storyboard ONLY scene {scene_id} again" + (f", following this instruction: {instruction}" if instruction else "") + ".")
    raw = llm.chat_json(ss.STORYBOARD_MODEL, system_prompt(p), user, _scene_schema(), name="scene",
                        project_id=project_id, stage="storyboard")
    try:
        new = SceneBoard.model_validate(raw["scene"]).model_dump()
    except (ValidationError, KeyError) as exc:
        raise StoryboardError(f"The new scene had an invalid structure: {exc}") from exc
    new["scene_id"] = scene_id
    one = validate({"overall": board["overall"], "scenes": [new]}, p, {"scenes": [sc], "title": script["title"]}, research)
    return one["scenes"][0]


def save(project_id: int, board: dict) -> None:
    """Store the board on the project and mirror each scene into studio_scenes (used by later stages)."""
    with session_scope() as s:
        p = s.get(StudioProject, project_id)
        p.storyboard_json = json.dumps(board)
        existing = {sc.storyboard_json and json.loads(sc.storyboard_json).get("scene_id"): sc for sc in p.scenes}
        keep = set()
        for pos, b in enumerate(board["scenes"]):
            row = existing.get(b["scene_id"])
            if row is None:
                row = StudioScene(project_id=project_id)
                s.add(row)
            row.position = pos
            row.narration = b["narration"]
            row.duration = b["seconds"]
            row.storyboard_json = json.dumps(b)
            row.status = "storyboarded"
            keep.add(b["scene_id"])
        for sid, row in existing.items():
            if sid not in keep:
                s.delete(row)


@worker.register("storyboard")
def run(project_id: int, report) -> None:
    with session_scope() as s:
        p = s.get(StudioProject, project_id)
        s.expunge(p)
    if not p.script_json:
        raise StoryboardError("No approved script. Run and approve Script first.")
    script = json.loads(p.script_json)
    research = json.loads(p.research_json) if p.research_json else None
    report(0.1, f"Planning shots ({ss.STORYBOARD_MODEL})")
    board = generate(p, script, research, project_id)
    board["model"] = ss.STORYBOARD_MODEL
    report(0.9, "Saving storyboard")
    save(project_id, board)
