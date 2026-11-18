"""Application configuration.

Values come from environment variables (optionally loaded from backend/.env).
Secrets (OAuth client secret, refresh tokens, API keys) are never stored here
or returned through the API.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    p = Path(raw) if raw else default
    if not p.is_absolute():
        p = BACKEND_DIR / p
    return p.resolve()


@dataclass
class Settings:
    # Storage locations
    data_dir: Path = field(default_factory=lambda: _path("DATA_DIR", BACKEND_DIR / "data"))
    credentials_dir: Path = field(default_factory=lambda: _path("CREDENTIALS_DIR", BACKEND_DIR / "credentials"))
    incoming_dir: Path | None = None   # default: <DATA_DIR>/incoming  (env INCOMING_DIR)
    library_dir: Path | None = None    # default: <DATA_DIR>/library   (env LIBRARY_DIR)
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", ""))

    # Google OAuth
    client_secret_file: Path = field(
        default_factory=lambda: _path("GOOGLE_CLIENT_SECRET_FILE", BACKEND_DIR / "credentials" / "client_secret.json")
    )
    # Public base URL of the backend; used to build the OAuth loopback redirect URI.
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "http://localhost:8000"))
    # Where to send the browser after OAuth completes (the dashboard).
    dashboard_url: str = field(default_factory=lambda: _env("DASHBOARD_URL", "http://localhost:8000"))
    # Optional: also request youtube.force-ssl so already-uploaded videos can be
    # rescheduled (videos.update). Off by default to keep scopes minimal.
    enable_manage_scope: bool = field(default_factory=lambda: _env_bool("YOUTUBE_ENABLE_MANAGE_SCOPE", False))

    # Anthropic (metadata generation). The SDK reads ANTHROPIC_API_KEY itself.
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-opus-5-5"))
    metadata_ai_enabled: bool = field(default_factory=lambda: _env_bool("METADATA_AI_ENABLED", True))

    # Server / security
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8000))
    # If set, every /api request (except the OAuth callback) must carry
    # "Authorization: Bearer <DASHBOARD_TOKEN>". Required when HOST is not localhost.
    dashboard_token: str = field(default_factory=lambda: _env("DASHBOARD_TOKEN", ""))
    # Token for the agent integration API (/api/agent/*). Empty = agent API disabled.
    agent_api_token: str = field(default_factory=lambda: _env("AGENT_API_TOKEN", ""))
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            o.strip()
            for o in _env("ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000,http://localhost:5173,http://127.0.0.1:5173").split(",")
            if o.strip()
        ]
    )

    # Tools
    ffprobe_path: str = field(default_factory=lambda: _env("FFPROBE_PATH", "ffprobe"))

    # Upload engine
    upload_chunk_size_mb: int = field(default_factory=lambda: _env_int("UPLOAD_CHUNK_SIZE_MB", 8))
    upload_max_retries: int = field(default_factory=lambda: _env_int("UPLOAD_MAX_RETRIES", 8))

    # Background jobs (disable in tests)
    scheduler_enabled: bool = field(default_factory=lambda: _env_bool("SCHEDULER_ENABLED", True))
    ingest_interval_seconds: int = field(default_factory=lambda: _env_int("INGEST_INTERVAL_SECONDS", 30))

    def __post_init__(self) -> None:
        if self.incoming_dir is None:
            self.incoming_dir = _path("INCOMING_DIR", self.data_dir / "incoming")
        if self.library_dir is None:
            self.library_dir = _path("LIBRARY_DIR", self.data_dir / "library")

    @property
    def tokens_dir(self) -> Path:
        return self.credentials_dir / "tokens"

    @property
    def resolved_database_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'app.db').as_posix()}"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.credentials_dir, self.tokens_dir, self.incoming_dir, self.library_dir, self.data_dir / "logs"):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()

# YouTube OAuth scopes.
# youtube.upload  -> videos.insert
# youtube.readonly -> channels.list(mine=true) / videos.list to verify channel and upload status.
# (youtube.upload alone cannot call channels.list.)
BASE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
MANAGE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"


def oauth_scopes() -> list[str]:
    return BASE_SCOPES + ([MANAGE_SCOPE] if settings.enable_manage_scope else [])


# Default application safety limits (NOT Google's quotas). Editable in the dashboard.
DEFAULT_MAX_DAILY_UPLOAD_CALLS = 80
DEFAULT_MAX_DAILY_SEARCH_CALLS = 80
DEFAULT_MAX_DAILY_OTHER_QUOTA_UNITS = 8000

DEFAULT_TIMEZONE = "Asia/Karachi"
QUOTA_TIMEZONE = "America/Los_Angeles"  # YouTube quotas reset at midnight Pacific Time
