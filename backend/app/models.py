"""SQLAlchemy models. All datetimes are naive UTC."""
from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .timeutil import utcnow


class VideoStatus:
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    QUEUED = "queued"          # approved and waiting in the upload queue
    UPLOADING = "uploading"
    UPLOADED = "uploaded"      # uploaded as private/unlisted (or public "publish now")
    SCHEDULED = "scheduled"    # uploaded private with publishAt in the future
    PUBLISHED = "published"
    FAILED = "failed"

    ALL = [PENDING_REVIEW, APPROVED, REJECTED, QUEUED, UPLOADING, UPLOADED, SCHEDULED, PUBLISHED, FAILED]


class PermissionStatus:
    UNKNOWN = "unknown"
    ORIGINAL = "original"          # footage you recorded yourself
    LICENSED = "licensed"
    AUTHORIZED = "authorized"      # explicit permission from the creator
    FAIR_USE_CLAIMED = "fair_use_claimed"  # your judgement; not a guarantee
    NOT_AUTHORIZED = "not_authorized"

    ALL = [UNKNOWN, ORIGINAL, LICENSED, AUTHORIZED, FAIR_USE_CLAIMED, NOT_AUTHORIZED]
    CLEARED = {ORIGINAL, LICENSED, AUTHORIZED, FAIR_USE_CLAIMED}


class RightsReview:
    PENDING = "pending"
    CLEARED = "cleared"
    BLOCKED = "blocked"
    ALL = [PENDING, CLEARED, BLOCKED]


class UploadStatus:
    QUEUED = "queued"
    UPLOADING = "uploading"
    PAUSED = "paused"          # interrupted; resumable session kept for recovery
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ACTIVE = {QUEUED, UPLOADING, PAUSED}


class PublishMode:
    PRIVATE = "private"
    UNLISTED = "unlisted"
    SCHEDULED = "scheduled"
    PUBLIC = "public"          # "publish now"; gated by allow_public_publishing
    ALL = [PRIVATE, UNLISTED, SCHEDULED, PUBLIC]


class YouTubeAccount(Base):
    __tablename__ = "youtube_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    channel_name: Mapped[str | None] = mapped_column(String(255))
    thumbnail_url: Mapped[str | None] = mapped_column(String(512))
    # connected | expired | revoked | disconnected | error
    connection_status: Mapped[str] = mapped_column(String(32), default="disconnected")
    # File name of the token in credentials/tokens (never the token itself).
    credential_reference: Mapped[str | None] = mapped_column(String(255))
    granted_scopes: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Source rights record
    original_source_url: Mapped[str | None] = mapped_column(String(1024))
    source_creator: Mapped[str | None] = mapped_column(String(255))
    permission_status: Mapped[str] = mapped_column(String(32), default=PermissionStatus.UNKNOWN)
    permission_notes: Mapped[str | None] = mapped_column(Text)
    footage_type: Mapped[str | None] = mapped_column(String(32))  # original | licensed | authorized | third_party
    rights_review_status: Mapped[str] = mapped_column(String(32), default=RightsReview.PENDING)
    original_commentary: Mapped[str | None] = mapped_column(Text)  # what original value this clip adds

    clip_start_time: Mapped[float | None] = mapped_column(Float)
    clip_end_time: Mapped[float | None] = mapped_column(Float)
    file_path: Mapped[str] = mapped_column(String(1024))
    file_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    file_size: Mapped[int | None] = mapped_column(Integer)

    # Context from the clipper used for metadata generation
    context_notes: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    topic: Mapped[str | None] = mapped_column(String(64))

    # Metadata
    title: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    hashtags_json: Mapped[str] = mapped_column(Text, default="[]")
    category_id: Mapped[str] = mapped_column(String(8), default="20")  # 20 = Gaming
    metadata_source: Mapped[str | None] = mapped_column(String(32))  # ai | template | manual
    suggested_publish_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Technical properties (from ffprobe)
    duration: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    video_codec: Mapped[str | None] = mapped_column(String(32))
    audio_codec: Mapped[str | None] = mapped_column(String(32))
    frame_rate: Mapped[float | None] = mapped_column(Float)
    validation_ok: Mapped[bool | None] = mapped_column(Boolean)
    validation_errors_json: Mapped[str] = mapped_column(Text, default="[]")
    validation_warnings_json: Mapped[str] = mapped_column(Text, default="[]")

    status: Mapped[str] = mapped_column(String(32), default=VideoStatus.PENDING_REVIEW, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    uploads: Mapped[list["Upload"]] = relationship(back_populates="video", order_by="Upload.id")

    # JSON list helpers
    @property
    def tags(self) -> list[str]:
        return json.loads(self.tags_json or "[]")

    @tags.setter
    def tags(self, value: list[str]) -> None:
        self.tags_json = json.dumps(value or [])

    @property
    def hashtags(self) -> list[str]:
        return json.loads(self.hashtags_json or "[]")

    @hashtags.setter
    def hashtags(self, value: list[str]) -> None:
        self.hashtags_json = json.dumps(value or [])

    @property
    def resolution(self) -> str | None:
        return f"{self.width}x{self.height}" if self.width and self.height else None


class Upload(Base):
    __tablename__ = "uploads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("youtube_accounts.id"))
    youtube_video_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    upload_status: Mapped[str] = mapped_column(String(32), default=UploadStatus.QUEUED, index=True)
    publish_mode: Mapped[str] = mapped_column(String(16), default=PublishMode.PRIVATE)
    privacy_status: Mapped[str] = mapped_column(String(16), default="private")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime)  # publishAt (UTC)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Resumable upload state (persisted so an interrupted upload can be resumed)
    resumable_uri: Mapped[str | None] = mapped_column(Text)
    bytes_uploaded: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int | None] = mapped_column(Integer)
    upload_attempts: Mapped[int] = mapped_column(Integer, default=0)
    insert_calls: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    error_kind: Mapped[str | None] = mapped_column(String(32))  # auth | quota | network | permanent | validation
    requested_by: Mapped[str | None] = mapped_column(String(32))  # dashboard | agent | auto
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    video: Mapped[Video] = relationship(back_populates="uploads")


class QuotaUsage(Base):
    """Aggregated per Pacific quota day and quota bucket (upload | search | other)."""

    __tablename__ = "quota_usage"
    __table_args__ = (UniqueConstraint("quota_date", "quota_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    quota_date: Mapped[date] = mapped_column(Date, index=True)
    quota_type: Mapped[str] = mapped_column(String(16))
    requests_used: Mapped[int] = mapped_column(Integer, default=0)
    units_used: Mapped[int] = mapped_column(Integer, default=0)
    failed_requests: Mapped[int] = mapped_column(Integer, default=0)
    retry_attempts: Mapped[int] = mapped_column(Integer, default=0)
    successful_uploads: Mapped[int] = mapped_column(Integer, default=0)
    # Set when Google reports quotaExceeded/uploadLimitExceeded: hard stop until reset.
    exhausted: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set when a request's outcome is unknown (e.g. crash mid-request).
    uncertain: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ApiRequestLog(Base):
    """Every YouTube Data API request: method, timestamp, estimated cost and result."""

    __tablename__ = "api_request_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    quota_date: Mapped[date] = mapped_column(Date, index=True)
    method: Mapped[str] = mapped_column(String(64))
    quota_type: Mapped[str] = mapped_column(String(16))
    estimated_cost: Mapped[int] = mapped_column(Integer)
    result: Mapped[str] = mapped_column(String(16), default="pending")  # pending | success | error
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_reason: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[str | None] = mapped_column(Text)
    is_retry: Mapped[bool] = mapped_column(Boolean, default=False)
    upload_id: Mapped[int | None] = mapped_column(Integer)


class PublishingSchedule(Base):
    __tablename__ = "publishing_schedule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # auto-assign approved clips to slots
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Karachi")
    daily_upload_limit: Mapped[int] = mapped_column(Integer, default=3)
    publishing_times_json: Mapped[str] = mapped_column(Text, default='["13:00", "18:00", "21:00"]')
    approval_required: Mapped[bool] = mapped_column(Boolean, default=True)
    default_privacy: Mapped[str] = mapped_column(String(16), default="private")
    # Public publishing (scheduled publish & publish-now) is OFF until explicitly enabled.
    allow_public_publishing: Mapped[bool] = mapped_column(Boolean, default=False)
    min_lead_minutes: Mapped[int] = mapped_column(Integer, default=30)

    # Quota safety limits (application limits, not Google's quotas)
    max_daily_upload_calls: Mapped[int] = mapped_column(Integer, default=80)
    max_daily_search_calls: Mapped[int] = mapped_column(Integer, default=80)
    max_daily_other_quota_units: Mapped[int] = mapped_column(Integer, default=8000)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def publishing_times(self) -> list[str]:
        return json.loads(self.publishing_times_json or "[]")

    @publishing_times.setter
    def publishing_times(self, value: list[str]) -> None:
        self.publishing_times_json = json.dumps(sorted(value))


class EventLog(Base):
    """Human-readable audit trail (approvals, errors, recoveries)."""

    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    category: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    video_id: Mapped[int | None] = mapped_column(Integer)
    upload_id: Mapped[int | None] = mapped_column(Integer)
    actor: Mapped[str | None] = mapped_column(String(32))


# --------------------------------------------------------------------------- clipping engine
class ProjectStatus:
    CREATED = "created"
    DOWNLOADING = "downloading"          # YouTube import in progress
    EXTRACTING = "extracting_audio"
    TRANSCRIBING = "transcribing"
    ANALYZING = "finding_moments"
    READY = "ready"
    FAILED = "failed"
    BUSY = {DOWNLOADING, EXTRACTING, TRANSCRIBING, ANALYZING}


class CandidateStatus:
    PROPOSED = "proposed"
    QUEUED = "queued"
    RENDERING = "rendering"
    RENDERED = "rendered"
    FAILED = "failed"
    DISCARDED = "discarded"


class ClipProject(Base):
    """A long source video (stream VOD, walkthrough...) that Shorts are cut from."""

    __tablename__ = "clip_projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    source_path: Mapped[str] = mapped_column(String(1024))
    source_mode: Mapped[str] = mapped_column(String(16), default="linked")  # linked | uploaded | youtube
    clip_range_start: Mapped[float | None] = mapped_column(Float)   # YouTube import: section of the original video
    clip_range_end: Mapped[float | None] = mapped_column(Float)
    plain: Mapped[bool] = mapped_column(Boolean, default=False)       # quick mode: no transcript, no AI voice/captions
    live_recording_id: Mapped[int | None] = mapped_column(Integer, index=True)   # part of a live recording
    live_part: Mapped[int | None] = mapped_column(Integer)
    source_url: Mapped[str | None] = mapped_column(String(1024))
    source_creator: Mapped[str | None] = mapped_column(String(255))
    permission_status: Mapped[str] = mapped_column(String(32), default="unknown")
    permission_notes: Mapped[str | None] = mapped_column(Text)
    topic_hint: Mapped[str | None] = mapped_column(String(255))  # e.g. "GTA 5 mission walkthrough"
    duration: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default=ProjectStatus.CREATED, index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    stage_message: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    max_candidates: Mapped[int] = mapped_column(Integer, default=8)
    voice: Mapped[str | None] = mapped_column(String(32))          # OpenAI TTS voice for hook/outro
    min_clip_seconds: Mapped[int] = mapped_column(Integer, default=20)
    max_clip_seconds: Mapped[int] = mapped_column(Integer, default=60)
    est_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    candidates: Mapped[list["ClipCandidate"]] = relationship(back_populates="project", order_by="ClipCandidate.start_time",
                                                             cascade="all, delete-orphan")


class ClipCandidate(Base):
    """A moment the AI proposed; becomes a rendered Short (Video) when you choose it."""

    __tablename__ = "clip_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("clip_projects.id", ondelete="CASCADE"), index=True)
    start_time: Mapped[float] = mapped_column(Float)
    end_time: Mapped[float] = mapped_column(Float)
    score: Mapped[float] = mapped_column(Float, default=0)
    category: Mapped[str | None] = mapped_column(String(32))
    headline: Mapped[str | None] = mapped_column(String(120))   # big title text on the video
    hook_line: Mapped[str | None] = mapped_column(Text)          # spoken by the avatar at the start
    outro_line: Mapped[str | None] = mapped_column(Text)         # spoken at the end
    reason: Mapped[str | None] = mapped_column(Text)
    excerpt: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=CandidateStatus.PROPOSED, index=True)
    render_progress: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)
    video_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    project: Mapped[ClipProject] = relationship(back_populates="candidates")


class LiveStatus:
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"
    FINISHED = "finished"      # stream ended, or stopped by the user / time limit
    FAILED = "failed"
    ACTIVE = {STARTING, RECORDING, STOPPING}


class LiveRecording(Base):
    """A YouTube live stream being recorded in parts; every finished part becomes a quick-mode Clipper project."""

    __tablename__ = "live_recordings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(1024))
    title: Mapped[str | None] = mapped_column(String(512))
    channel: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default=LiveStatus.STARTING, index=True)
    message: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    part_minutes: Mapped[int] = mapped_column(Integer, default=30)
    max_minutes: Mapped[int] = mapped_column(Integer, default=360)
    recorded_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    parts_done: Mapped[int] = mapped_column(Integer, default=0)
    bytes_written: Mapped[int] = mapped_column(Integer, default=0)
    permission_status: Mapped[str] = mapped_column(String(32), default="authorized")
    permission_notes: Mapped[str | None] = mapped_column(Text)
    source_creator: Mapped[str | None] = mapped_column(String(255))
    max_candidates: Mapped[int] = mapped_column(Integer, default=5)
    min_clip_seconds: Mapped[int] = mapped_column(Integer, default=20)
    max_clip_seconds: Mapped[int] = mapped_column(Integer, default=45)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
