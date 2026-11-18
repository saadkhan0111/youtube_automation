"""AI Information Video Studio API (/api/studio/*)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..events import record_event
from ..serializers import iso
from ..studio import assets, costs, narration, research, script, stages, storyboard, styles, worker  # importing registers the stage handlers
from ..studio import settings as ss
from ..studio.models import StudioProject

router = APIRouter(prefix="/api/studio")
DBDep = Depends(get_db, scope="function")


def project_dict(p: StudioProject, full: bool = False) -> dict:
    st = p.stages
    d = {
        "id": p.id, "topic": p.topic, "category": p.category, "video_format": p.video_format,
        "target_duration": p.target_duration, "audience": p.audience, "editing_style": p.editing_style,
        "narration_style": p.narration_style, "language": p.language, "voice": p.voice,
        "reference_urls": p.reference_urls, "instructions": p.instructions, "use_avatar": p.use_avatar,
        "stage": p.stage,
        "stages": [{"key": s, "label": ss.STAGE_LABELS[s], "implemented": s in worker.HANDLERS or s == "complete",
                    **(st.get(s) or {"status": stages.LOCKED})} for s in ss.STAGES],
        "created_at": iso(p.created_at), "updated_at": iso(p.updated_at),
    }
    if full:
        d["costs"] = costs.summary(p.id)
        d["scene_count"] = len(p.scenes)
        d["source_count"] = len(p.sources)
    return d


def _project(db: Session, pid: int) -> StudioProject:
    p = db.get(StudioProject, pid)
    if p is None:
        raise HTTPException(404, "Project not found")
    return p


@router.get("/options")
def options():
    return ss.options()


class ProjectIn(BaseModel):
    topic: str = Field(min_length=3, max_length=500)
    category: str
    video_format: Literal["short", "long"] = "short"
    target_duration: int = Field(60, ge=15, le=1800)
    audience: str | None = Field(None, max_length=255)
    editing_style: str = "viral"
    narration_style: str = "energetic"
    language: str = "en"
    voice: str | None = None
    reference_urls: list[str] = Field(default_factory=list, max_length=20)
    instructions: str | None = Field(None, max_length=4000)
    use_avatar: bool = False

    @field_validator("reference_urls")
    @classmethod
    def _urls(cls, v: list[str]) -> list[str]:
        out = []
        for u in v:
            u = u.strip()
            if not u:
                continue
            parsed = urlparse(u)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError(f"Not a valid web address: {u}")
            out.append(u)
        return out


def _validate(body: ProjectIn) -> None:
    checks = [("category", ss.CATEGORIES), ("editing_style", ss.EDITING_STYLES),
              ("narration_style", ss.NARRATION_STYLES), ("language", ss.LANGUAGES)]
    for field, allowed in checks:
        if getattr(body, field) not in allowed:
            raise HTTPException(400, f"Invalid {field.replace('_', ' ')}.")
    if body.voice and body.voice not in ss.VOICES:
        raise HTTPException(400, "Unknown voice.")
    lo, hi = ss.DURATION_LIMITS[body.video_format]
    if not lo <= body.target_duration <= hi:
        raise HTTPException(400, f"A {body.video_format} video must be {lo}-{hi} seconds long.")


@router.get("/projects")
def list_projects(db: Session = DBDep):
    return [project_dict(p) for p in db.scalars(select(StudioProject).order_by(StudioProject.id.desc()))]


@router.post("/projects")
def create_project(body: ProjectIn, db: Session = DBDep):
    _validate(body)
    p = StudioProject(
        topic=body.topic.strip(), category=body.category, video_format=body.video_format,
        target_duration=body.target_duration, audience=(body.audience or "").strip() or None,
        editing_style=body.editing_style, narration_style=body.narration_style, language=body.language,
        voice=body.voice or ss.DEFAULT_VOICE, instructions=(body.instructions or "").strip() or None,
        use_avatar=body.use_avatar, stage="research",
    )
    p.reference_urls = body.reference_urls
    p.stages = stages.initial_stages()
    db.add(p)
    db.flush()
    ss.project_dir(p.id)
    record_event("studio", f"New Studio project #{p.id}: {p.topic}")
    return project_dict(p, full=True)


@router.get("/projects/{pid}")
def get_project(pid: int, db: Session = DBDep):
    return project_dict(_project(db, pid), full=True)


class ProjectPatch(BaseModel):
    topic: str | None = Field(None, min_length=3, max_length=500)
    audience: str | None = Field(None, max_length=255)
    editing_style: str | None = None
    narration_style: str | None = None
    voice: str | None = None
    instructions: str | None = Field(None, max_length=4000)
    use_avatar: bool | None = None
    reference_urls: list[str] | None = None


@router.patch("/projects/{pid}")
def patch_project(pid: int, body: ProjectPatch, db: Session = DBDep):
    """Settings that can change at any time. Changing the topic or references re-opens research."""
    p = _project(db, pid)
    data = body.model_dump(exclude_unset=True)
    if "editing_style" in data and data["editing_style"] not in ss.EDITING_STYLES:
        raise HTTPException(400, "Invalid editing style.")
    if "narration_style" in data and data["narration_style"] not in ss.NARRATION_STYLES:
        raise HTTPException(400, "Invalid narration style.")
    if "voice" in data and data["voice"] not in ss.VOICES:
        raise HTTPException(400, "Unknown voice.")
    if "reference_urls" in data:
        data["reference_urls"] = ProjectIn.model_validate({"topic": "xxx", "category": "general",
                                                           "reference_urls": data["reference_urls"]}).reference_urls
    research_inputs_changed = any(k in data for k in ("topic", "reference_urls"))
    for k, v in data.items():
        setattr(p, k, v.strip() if isinstance(v, str) else v)
    if research_inputs_changed and stages.status(p, "research") in (stages.REVIEW, stages.APPROVED, stages.FAILED):
        stages.reopen(p, "research")
    return project_dict(p, full=True)


@router.delete("/projects/{pid}")
def delete_project(pid: int, db: Session = DBDep):
    p = _project(db, pid)
    if any((e or {}).get("status") == stages.RUNNING for e in p.stages.values()):
        raise HTTPException(400, "Wait until the running stage has finished.")
    shutil.rmtree(ss.studio_dir() / str(p.id), ignore_errors=True)
    db.delete(p)
    record_event("studio", f"Deleted Studio project #{pid}")
    return {"ok": True}


@router.post("/projects/{pid}/stages/{stage}/run")
def run_stage(pid: int, stage: str, db: Session = DBDep):
    p = _project(db, pid)
    if stage not in stages.WORK_STAGES:
        raise HTTPException(404, "Unknown stage")
    if stage not in worker.HANDLERS:
        raise HTTPException(501, f"The {ss.STAGE_LABELS[stage]} stage is not built yet.")
    if not ss.clip_settings.openai_available():
        raise HTTPException(400, "OPENAI_API_KEY is missing in backend/.env")
    try:
        stages.mark_running(p, stage)
    except stages.StageError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    worker.enqueue(p.id, stage)
    db.expire_all()
    return project_dict(_project(db, pid), full=True)


@router.post("/projects/{pid}/stages/{stage}/approve")
def approve_stage(pid: int, stage: str, db: Session = DBDep):
    p = _project(db, pid)
    if stage == "assets" and stages.status(p, stage) == stages.REVIEW:
        problems = assets.blocking_problems(pid)
        if problems:
            raise HTTPException(400, " ".join(problems))
    try:
        stages.approve(p, stage)
    except stages.StageError as exc:
        raise HTTPException(400, str(exc)) from exc
    record_event("studio", f"Project #{pid}: {stage} approved")
    return project_dict(p, full=True)


@router.get("/projects/{pid}/costs")
def project_costs(pid: int, db: Session = DBDep):
    _project(db, pid)
    return costs.summary(pid)


# ------------------------------------------------------------------ research
@router.get("/projects/{pid}/research")
def get_research(pid: int, db: Session = DBDep):
    p = _project(db, pid)
    if not p.research_json:
        raise HTTPException(404, "No research yet. Run the Research stage.")
    return json.loads(p.research_json)


class ResearchPatch(BaseModel):
    facts: list[research.FactEdit] = Field(default_factory=list)
    best_angle: str | None = Field(None, max_length=1000)


@router.patch("/projects/{pid}/research")
def patch_research(pid: int, body: ResearchPatch, db: Session = DBDep):
    """Edit, include/exclude or delete facts. Re-opens Research for review (later stages are locked)."""
    p = _project(db, pid)
    if not p.research_json:
        raise HTTPException(404, "No research yet.")
    if stages.status(p, "research") == stages.RUNNING:
        raise HTTPException(400, "Research is running.")
    data = json.loads(p.research_json)
    try:
        data = research.apply_edits(data, body.facts)
    except research.ResearchError as exc:
        raise HTTPException(400, str(exc)) from exc
    if body.best_angle is not None:
        data["best_angle"] = body.best_angle.strip()
    if not any(f.get("include") for f in data["facts"]):
        raise HTTPException(400, "Keep at least one fact included.")
    p.research_json = json.dumps(data)
    if stages.status(p, "research") == stages.APPROVED:
        stages.reopen(p, "research")
    return data


# ------------------------------------------------------------------ script
def _script_ctx(p: StudioProject) -> tuple[dict, dict]:
    if not p.script_json:
        raise HTTPException(404, "No script yet. Run the Script stage.")
    if not p.research_json:
        raise HTTPException(400, "Research is missing.")
    return json.loads(p.script_json), json.loads(p.research_json)


def _not_running(p: StudioProject, stage: str) -> None:
    if stages.status(p, stage) == stages.RUNNING:
        raise HTTPException(400, f"{stage} is running.")


def _save_script(p: StudioProject, data: dict, research_data: dict) -> dict:
    data["warnings"] = script.check(data, p, research_data)
    p.script_json = json.dumps(data)
    if stages.status(p, "script") in (stages.APPROVED, stages.REVIEW, stages.FAILED):
        stages.reopen(p, "script")
    return data


@router.get("/projects/{pid}/script")
def get_script(pid: int, db: Session = DBDep):
    data, research_data = _script_ctx(_project(db, pid))
    return data


class SceneIn(BaseModel):
    id: str | None = None
    purpose: Literal["hook", "setup", "point", "reveal", "transition", "payoff", "cta"]
    narration: str = Field(min_length=3, max_length=1500)
    fact_ids: list[str] = Field(default_factory=list)
    emphasis: list[str] = Field(default_factory=list, max_length=5)


class ThumbnailIn(BaseModel):
    concept: str = Field(max_length=500)
    text: str = Field(max_length=60)


class ScriptPatch(BaseModel):
    title: str | None = Field(None, min_length=3, max_length=100)
    description: str | None = Field(None, max_length=5000)
    thumbnail: ThumbnailIn | None = None
    scenes: list[SceneIn] | None = Field(None, min_length=2, max_length=80)


@router.patch("/projects/{pid}/script")
def patch_script(pid: int, body: ScriptPatch, db: Session = DBDep):
    """Edit the script. `scenes` replaces the whole list (edit, add, delete and reorder in one save).
    The checks run again; Script goes back to review and later stages are locked."""
    p = _project(db, pid)
    _not_running(p, "script")
    data, research_data = _script_ctx(p)
    if body.title is not None:
        data["title"] = body.title.strip()
    if body.description is not None:
        data["description"] = body.description.strip()
    if body.thumbnail is not None:
        data["thumbnail"] = body.thumbnail.model_dump()
    if body.scenes is not None:
        used = {sc.id for sc in body.scenes if sc.id}
        n = 0
        scenes = []
        for sc in body.scenes:
            d = sc.model_dump()
            d["narration"] = " ".join(d["narration"].split())
            if not d["id"]:
                n += 1
                while f"N{n}" in used:
                    n += 1
                d["id"] = f"N{n}"
                used.add(d["id"])
            scenes.append(d)
        data["scenes"] = scenes
    return _save_script(p, data, research_data)


class RegenerateIn(BaseModel):
    instruction: str | None = Field(None, max_length=1000)


@router.post("/projects/{pid}/script/scenes/{scene_id}/regenerate")
def regenerate_scene(pid: int, scene_id: str, body: RegenerateIn, db: Session = DBDep):
    """Rewrite one scene (optionally following an instruction) without touching the rest."""
    p = _project(db, pid)
    _not_running(p, "script")
    data, research_data = _script_ctx(p)
    idx = next((i for i, sc in enumerate(data["scenes"]) if sc["id"] == scene_id), None)
    if idx is None:
        raise HTTPException(404, "Scene not found")
    try:
        data["scenes"][idx] = script.regenerate_scene(p, research_data, data, idx, body.instruction, project_id=p.id)
    except script.ScriptError as exc:
        raise HTTPException(502, str(exc)) from exc
    return _save_script(p, data, research_data)


# ------------------------------------------------------------------ storyboard
def _board_ctx(p: StudioProject) -> tuple[dict, dict, dict | None]:
    if not p.storyboard_json:
        raise HTTPException(404, "No storyboard yet. Run the Storyboard stage.")
    if not p.script_json:
        raise HTTPException(400, "Script is missing.")
    return (json.loads(p.storyboard_json), json.loads(p.script_json),
            json.loads(p.research_json) if p.research_json else None)


def _save_board(p: StudioProject, db: Session, board: dict) -> dict:
    board["total_seconds"] = round(sum(b["seconds"] for b in board["scenes"]), 1)
    board["shot_count"] = sum(len(b["shots"]) for b in board["scenes"])
    db.commit()                                   # storyboard.save uses its own session
    storyboard.save(p.id, board)
    db.expire_all()
    p = db.get(StudioProject, p.id)
    if stages.status(p, "storyboard") in (stages.APPROVED, stages.REVIEW, stages.FAILED):
        stages.reopen(p, "storyboard")
    return board


@router.get("/projects/{pid}/storyboard")
def get_storyboard(pid: int, db: Session = DBDep):
    board, _, _ = _board_ctx(_project(db, pid))
    return board


class OverallIn(BaseModel):
    music_mood: str | None = None
    visual_style: str | None = Field(None, min_length=5, max_length=500)
    color_grade: str | None = Field(None, max_length=200)


@router.patch("/projects/{pid}/storyboard")
def patch_overall(pid: int, body: OverallIn, db: Session = DBDep):
    p = _project(db, pid)
    _not_running(p, "storyboard")
    board, _, _ = _board_ctx(p)
    data = body.model_dump(exclude_unset=True)
    if "music_mood" in data and data["music_mood"] not in styles.MUSIC_MOODS:
        raise HTTPException(400, "Unknown music mood.")
    board["overall"].update({k: v.strip() if isinstance(v, str) else v for k, v in data.items()})
    return _save_board(p, db, board)


@router.patch("/projects/{pid}/storyboard/scenes/{scene_id}")
def patch_board_scene(pid: int, scene_id: str, body: dict, db: Session = DBDep):
    """Replace one scene's visual plan (shots, sound effects, music mood, transition, text). Narration can't be
    changed here (edit the script for that). The scene is validated and checked like generated ones."""
    p = _project(db, pid)
    _not_running(p, "storyboard")
    board, script_data, research_data = _board_ctx(p)
    idx = next((i for i, b in enumerate(board["scenes"]) if b["scene_id"] == scene_id), None)
    if idx is None:
        raise HTTPException(404, "Scene not found")
    sc = next(s for s in script_data["scenes"] if s["id"] == scene_id)
    keys = ("shots", "on_screen_text", "animation", "sound_effects", "music_mood", "transition_out")
    merged = {**{k: board["scenes"][idx][k] for k in keys}, **{k: v for k, v in body.items() if k in keys}, "scene_id": scene_id}
    try:
        one = storyboard.validate({"overall": board["overall"], "scenes": [merged]}, p,
                                  {"scenes": [sc], "title": script_data["title"]}, research_data)
    except storyboard.StoryboardError as exc:
        raise HTTPException(400, str(exc)) from exc
    board["scenes"][idx] = {**one["scenes"][0], "edited": True}
    return _save_board(p, db, board)


@router.post("/projects/{pid}/storyboard/scenes/{scene_id}/regenerate")
def regenerate_board_scene(pid: int, scene_id: str, body: RegenerateIn, db: Session = DBDep):
    p = _project(db, pid)
    _not_running(p, "storyboard")
    board, script_data, research_data = _board_ctx(p)
    idx = next((i for i, b in enumerate(board["scenes"]) if b["scene_id"] == scene_id), None)
    if idx is None:
        raise HTTPException(404, "Scene not found")
    try:
        board["scenes"][idx] = storyboard.regenerate_scene(p, script_data, research_data, board, scene_id,
                                                           body.instruction, project_id=p.id)
    except storyboard.StoryboardError as exc:
        raise HTTPException(502, str(exc)) from exc
    return _save_board(p, db, board)


# ------------------------------------------------------------------ voiceover
@router.get("/projects/{pid}/voice")
def get_voice(pid: int, db: Session = DBDep):
    p = _project(db, pid)
    vs = narration.settings_of(p)
    return {"summary": vs.get("summary"), "pronunciations": vs["pronunciations"], "speed": vs["speed"],
            "direction": narration.direction(p)}


@router.get("/projects/{pid}/voice/scenes/{scene_id}/audio")
def voice_audio(pid: int, scene_id: str, db: Session = DBDep):
    p = _project(db, pid)
    row = next((r for r in p.scenes if r.storyboard_json and json.loads(r.storyboard_json)["scene_id"] == scene_id), None)
    if row is None or not row.audio_path:
        raise HTTPException(404, "No audio for this scene yet.")
    path = Path(row.audio_path).resolve()
    if ss.project_dir(p.id).resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "Audio file missing.")
    return FileResponse(path, media_type="audio/wav")


class VoiceSettingsIn(BaseModel):
    pronunciations: dict[str, str] | None = None
    speed: float | None = Field(None, ge=0.9, le=1.15)


@router.patch("/projects/{pid}/voice/settings")
def patch_voice_settings(pid: int, body: VoiceSettingsIn, db: Session = DBDep):
    """Pronunciations and speed. Takes effect on the next run (unchanged scenes are reused from the cache)."""
    p = _project(db, pid)
    _not_running(p, "voice")
    vs = narration.settings_of(p)
    if body.pronunciations is not None:
        clean = {k.strip(): v.strip() for k, v in body.pronunciations.items() if k.strip() and v.strip()}
        if len(clean) > 200:
            raise HTTPException(400, "Too many pronunciation entries.")
        vs["pronunciations"] = clean
    if body.speed is not None:
        vs["speed"] = round(body.speed, 3)
    p.voice_json = json.dumps(vs)
    if stages.status(p, "voice") in (stages.REVIEW, stages.APPROVED):
        stages.reopen(p, "voice")
    return {"pronunciations": vs["pronunciations"], "speed": vs["speed"], "summary": vs.get("summary"),
            "note": "Run Voiceover again to apply. Scenes whose spoken text didn't change are reused for free."}


@router.post("/projects/{pid}/voice/scenes/{scene_id}/regenerate")
def regenerate_voice_scene(pid: int, scene_id: str, db: Session = DBDep):
    p = _project(db, pid)
    _not_running(p, "voice")
    if stages.status(p, "voice") in (stages.LOCKED, stages.READY):
        raise HTTPException(400, "Run Voiceover first.")
    db.commit()
    try:
        summary = narration.regenerate_one(pid, scene_id)
    except narration.VoiceError as exc:
        raise HTTPException(404, str(exc)) from exc
    db.expire_all()
    p = _project(db, pid)
    if stages.status(p, "voice") == stages.APPROVED:
        stages.reopen(p, "voice")
    return summary


# ------------------------------------------------------------------ assets
def _assets_touch(db: Session, pid: int) -> None:
    """After a manual change: an approved Assets stage goes back to review."""
    db.expire_all()
    p = _project(db, pid)
    if stages.status(p, "assets") == stages.APPROVED:
        stages.reopen(p, "assets")


def _assets_ready(p: StudioProject) -> None:
    _not_running(p, "assets")
    if stages.status(p, "assets") in (stages.LOCKED, stages.READY):
        raise HTTPException(400, "Run Assets first.")


@router.get("/projects/{pid}/assets")
def get_assets(pid: int, db: Session = DBDep):
    p = _project(db, pid)
    if not p.storyboard_json:
        raise HTTPException(400, "No storyboard yet.")
    return assets.overview(pid)


@router.get("/projects/{pid}/assets/{aid}/file")
def asset_file(pid: int, aid: int, preview: bool = False, db: Session = DBDep):
    from ..studio.models import StudioAsset

    a = db.get(StudioAsset, aid)
    if a is None or a.project_id != pid:
        raise HTTPException(404, "Asset not found")
    f = a.preview_path if preview else a.local_path
    if not f or not Path(f).is_file():
        raise HTTPException(404, "File missing")
    path = Path(f).resolve()
    if a.origin != "library" and ss.project_dir(pid).resolve() not in path.parents:
        raise HTTPException(404, "File missing")
    return FileResponse(path)


class ReplaceIn(BaseModel):
    action: Literal["next", "ai_image", "stock_video", "stock_image", "library", "graphic"]


@router.post("/projects/{pid}/assets/shots/{scene_id}/{idx}/replace")
def replace_asset(pid: int, scene_id: str, idx: int, body: ReplaceIn, db: Session = DBDep):
    p = _project(db, pid)
    _assets_ready(p)
    if body.action == "ai_image" and not ss.clip_settings.openai_available():
        raise HTTPException(400, "OPENAI_API_KEY is missing in backend/.env")
    db.commit()
    try:
        out = assets.replace(pid, scene_id, idx, body.action)
    except assets.AssetError as exc:
        raise HTTPException(400, str(exc)) from exc
    _assets_touch(db, pid)
    return out


@router.post("/projects/{pid}/assets/shots/{scene_id}/{idx}/upload")
def upload_asset(pid: int, scene_id: str, idx: int, file: UploadFile = File(...), db: Session = DBDep):
    p = _project(db, pid)
    _assets_ready(p)
    db.commit()
    try:
        out = assets.save_upload(pid, scene_id, idx, file.filename or "upload", file.file)
    except assets.AssetError as exc:
        raise HTTPException(400, str(exc)) from exc
    _assets_touch(db, pid)
    return out


@router.post("/projects/{pid}/assets/fill-missing")
def fill_missing(pid: int, db: Session = DBDep):
    """Generate AI illustrations for every shot that has no visual (runs as an Assets job)."""
    p = _project(db, pid)
    _assets_ready(p)
    if not ss.clip_settings.openai_available():
        raise HTTPException(400, "OPENAI_API_KEY is missing in backend/.env")
    assets.request_ai_fill(pid)
    try:
        stages.mark_running(p, "assets")
    except stages.StageError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    worker.enqueue(pid, "assets")
    db.expire_all()
    return project_dict(_project(db, pid), full=True)
