"""Database models for the AI Information Video Studio.

Registered on the shared SQLAlchemy Base (imported from app.models so create_all() creates them).
JSON documents (research report, script, storyboard, QA report) are stored as text columns.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ..timeutil import utcnow


def _loads(s: str | None, default):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


class StudioProject(Base):
    __tablename__ = "studio_projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic: Mapped[str] = mapped_column(String(500))
    category: Mapped[str] = mapped_column(String(32))
    video_format: Mapped[str] = mapped_column(String(8))            # short | long
    target_duration: Mapped[int] = mapped_column(Integer)          # seconds
    audience: Mapped[str | None] = mapped_column(String(255))
    editing_style: Mapped[str] = mapped_column(String(32))
    narration_style: Mapped[str] = mapped_column(String(32))
    language: Mapped[str] = mapped_column(String(16), default="en")
    voice: Mapped[str | None] = mapped_column(String(32))
    reference_urls_json: Mapped[str] = mapped_column(Text, default="[]")
    instructions: Mapped[str | None] = mapped_column(Text)
    use_avatar: Mapped[bool] = mapped_column(Boolean, default=False)

    # workflow: one current stage; each stage has its own status in stages_json
    stage: Mapped[str] = mapped_column(String(16), default="research", index=True)
    stages_json: Mapped[str] = mapped_column(Text, default="{}")    # {stage: {status, progress, message, error, updated_at}}

    # stage outputs (validated JSON)
    research_json: Mapped[str | None] = mapped_column(Text)
    script_json: Mapped[str | None] = mapped_column(Text)
    storyboard_json: Mapped[str | None] = mapped_column(Text)       # overall look + shot plans per scene
    voice_json: Mapped[str | None] = mapped_column(Text)            # voiceover summary + pronunciation dictionary
    outputs_json: Mapped[str | None] = mapped_column(Text)          # title/description/tags/thumbnail/...

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    scenes: Mapped[list["StudioScene"]] = relationship(back_populates="project", order_by="StudioScene.position",
                                                       cascade="all, delete-orphan")
    sources: Mapped[list["StudioSource"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    assets: Mapped[list["StudioAsset"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    renders: Mapped[list["StudioRender"]] = relationship(back_populates="project", cascade="all, delete-orphan")

    @property
    def reference_urls(self) -> list[str]:
        return _loads(self.reference_urls_json, [])

    @reference_urls.setter
    def reference_urls(self, v: list[str]) -> None:
        self.reference_urls_json = json.dumps(v or [])

    @property
    def stages(self) -> dict:
        return _loads(self.stages_json, {})

    @stages.setter
    def stages(self, v: dict) -> None:
        self.stages_json = json.dumps(v)


class StudioScene(Base):
    __tablename__ = "studio_scenes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("studio_projects.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    narration: Mapped[str] = mapped_column(Text, default="")
    duration: Mapped[float | None] = mapped_column(Float)          # planned (storyboard) or measured (voice)
    storyboard_json: Mapped[str] = mapped_column(Text, default="{}")  # validated storyboard scene
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("studio_assets.id", ondelete="SET NULL"))
    audio_path: Mapped[str | None] = mapped_column(String(1024))
    timing_json: Mapped[str | None] = mapped_column(Text)          # word timings of the narration
    cache_key: Mapped[str | None] = mapped_column(String(64))      # hash of inputs -> reuse renders
    status: Mapped[str] = mapped_column(String(16), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    project: Mapped[StudioProject] = relationship(back_populates="scenes")


class StudioSource(Base):
    __tablename__ = "studio_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("studio_projects.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str | None] = mapped_column(String(512))
    publisher: Mapped[str | None] = mapped_column(String(255))
    source_type: Mapped[str | None] = mapped_column(String(32))    # primary | official | publication | research | other
    reliability: Mapped[str | None] = mapped_column(String(16))    # high | medium | low
    notes: Mapped[str | None] = mapped_column(Text)
    user_provided: Mapped[bool] = mapped_column(Boolean, default=False)

    project: Mapped[StudioProject] = relationship(back_populates="sources")


class StudioAsset(Base):
    __tablename__ = "studio_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("studio_projects.id", ondelete="CASCADE"), index=True)
    scene_id: Mapped[str | None] = mapped_column(String(32), index=True)   # storyboard shot this fills
    shot_index: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="ready")         # ready | missing | failed
    note: Mapped[str | None] = mapped_column(Text)                           # why missing/failed, or a hint
    kind: Mapped[str] = mapped_column(String(16))                  # video | image | graphic | audio
    origin: Mapped[str] = mapped_column(String(16))                # upload | pexels | library | ai | graphic | music
    local_path: Mapped[str | None] = mapped_column(String(1024))
    preview_path: Mapped[str | None] = mapped_column(String(1024))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    license: Mapped[str | None] = mapped_column(String(255))
    attribution: Mapped[str | None] = mapped_column(String(512))
    prompt: Mapped[str | None] = mapped_column(Text)               # for AI images
    meta_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    project: Mapped[StudioProject] = relationship(back_populates="assets")


class StudioRender(Base):
    __tablename__ = "studio_renders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("studio_projects.id", ondelete="CASCADE"), index=True)
    aspect: Mapped[str] = mapped_column(String(8))                 # 9:16 | 16:9
    status: Mapped[str] = mapped_column(String(16), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    file_path: Mapped[str | None] = mapped_column(String(1024))
    qa_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    project: Mapped[StudioProject] = relationship(back_populates="renders")


class CostEvent(Base):
    """One billable (or measurable) unit of work: tokens, TTS characters, transcription minutes, images, render time."""

    __tablename__ = "cost_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    feature: Mapped[str] = mapped_column(String(16), default="studio")   # studio | clipper
    project_id: Mapped[int | None] = mapped_column(Integer, index=True)
    stage: Mapped[str | None] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(32))                    # openai | pexels | jamendo | local
    model: Mapped[str | None] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(32))                   # chat | web_search | tts | transcribe | image | render
    units_json: Mapped[str] = mapped_column(Text, default="{}")          # e.g. {"input_tokens": 1200, "output_tokens": 300}
    usd: Mapped[float | None] = mapped_column(Float)                     # None when no price is configured
    note: Mapped[str | None] = mapped_column(String(255))
