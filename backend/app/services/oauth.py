"""Google OAuth 2.0 for the YouTube Data API.

* Uses the Desktop OAuth client in credentials/client_secret.json with a
  loopback redirect to this backend (http://localhost:8000/api/auth/callback).
* Requests offline access so Google returns a refresh token.
* Tokens are stored as files in credentials/tokens/ (outside the frontend,
  git-ignored, permissions restricted to the current user). They are never
  returned by the API or written to logs.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import oauth_scopes, settings
from ..db import session_scope
from ..events import record_event
from ..models import YouTubeAccount
from . import quota
from .youtube_errors import AuthError, classify_http_error

log = logging.getLogger("ytshorts.oauth")

# Pending OAuth flows keyed by `state` (holds the PKCE code verifier). Expire after 15 min.
_pending_flows: dict[str, tuple[Flow, float]] = {}
_lock = threading.Lock()

# Allows tests to inject a fake YouTube service: fn(credentials) -> service
service_factory = None


def redirect_uri() -> str:
    return settings.public_base_url.rstrip("/") + "/api/auth/callback"


def client_secret_available() -> bool:
    return settings.client_secret_file.is_file()


def _validate_client_secret() -> None:
    if not client_secret_available():
        raise AuthError(
            f"OAuth client file not found at {settings.client_secret_file}. "
            "Download your Desktop OAuth client JSON from Google Cloud Console and save it there.",
            status="disconnected",
        )
    try:
        data = json.loads(settings.client_secret_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AuthError(f"client_secret.json is not valid JSON: {exc}", status="error") from exc
    if "installed" not in data and "web" not in data:
        raise AuthError("client_secret.json must contain an 'installed' (Desktop) or 'web' client.", status="error")


def start_authorization() -> str:
    """Create an OAuth flow and return the Google consent URL."""
    _validate_client_secret()
    uri = redirect_uri()
    if uri.startswith("http://localhost") or uri.startswith("http://127.0.0.1"):
        # Loopback redirect over plain HTTP is what Google expects for desktop clients.
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = Flow.from_client_secrets_file(
        str(settings.client_secret_file), scopes=oauth_scopes(), redirect_uri=uri, autogenerate_code_verifier=True
    )
    state = secrets.token_urlsafe(24)
    auth_url, _ = flow.authorization_url(
        access_type="offline",       # refresh token
        prompt="consent",            # force a refresh token to be (re)issued
        include_granted_scopes="true",
        state=state,
    )
    with _lock:
        now = time.time()
        for k in [k for k, (_, t) in _pending_flows.items() if now - t > 900]:
            _pending_flows.pop(k, None)
        _pending_flows[state] = (flow, now)
    return auth_url


def complete_authorization(state: str | None, code: str | None, error: str | None) -> YouTubeAccount:
    """Handle the OAuth redirect: exchange the code, verify the channel, store tokens."""
    if error:
        msg = "Access was denied on the Google consent screen." if error == "access_denied" else f"Google returned an OAuth error: {error}"
        record_event("oauth", msg, level="warning")
        raise AuthError(msg, status="disconnected")
    with _lock:
        entry = _pending_flows.pop(state or "", None)
    if entry is None:
        raise AuthError("OAuth state is invalid or expired. Start the connection again.", status="error")
    flow, _ = entry
    if not code:
        raise AuthError("No authorization code returned by Google.", status="error")
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        raise AuthError(f"Token exchange failed: {exc.__class__.__name__}", status="error") from exc
    creds: Credentials = flow.credentials
    if not creds.refresh_token:
        raise AuthError(
            "Google did not return a refresh token. Remove this app's access at "
            "https://myaccount.google.com/permissions and connect again.",
            status="error",
        )
    granted = set(creds.scopes or [])
    missing = [s for s in oauth_scopes() if granted and s not in granted]
    if missing:
        raise AuthError(f"Required permissions were not granted: {', '.join(missing)}", status="error")

    channel = fetch_own_channel(creds)
    token_name = f"{channel['id']}.json"
    _save_token(token_name, creds)

    with session_scope() as s:
        acct = s.scalar(select(YouTubeAccount).where(YouTubeAccount.channel_id == channel["id"]))
        if acct is None:
            acct = YouTubeAccount(channel_id=channel["id"])
            s.add(acct)
        # Single active channel for now; the model supports several.
        for other in s.scalars(select(YouTubeAccount).where(YouTubeAccount.channel_id != channel["id"])):
            other.is_active = False
        acct.channel_name = channel["title"]
        acct.thumbnail_url = channel.get("thumbnail")
        acct.credential_reference = token_name
        acct.connection_status = "connected"
        acct.granted_scopes = " ".join(sorted(granted))
        acct.last_error = None
        acct.is_active = True
        s.flush()
        s.expunge(acct)
    record_event("oauth", f"Connected YouTube channel '{channel['title']}' ({channel['id']})")
    return acct


def _token_path(name: str) -> Path:
    # name is generated by us, but guard against traversal anyway
    p = (settings.tokens_dir / Path(name).name).resolve()
    if settings.tokens_dir.resolve() not in p.parents:
        raise AuthError("Invalid credential reference", status="error")
    return p


def _restrict_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
        if sys.platform == "win32":
            user = os.environ.get("USERNAME")
            if user:
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
                    capture_output=True, check=False, timeout=10,
                )
    except Exception:
        log.warning("Could not restrict token file permissions for %s", path.name)


def _save_token(name: str, creds: Credentials) -> None:
    settings.tokens_dir.mkdir(parents=True, exist_ok=True)
    path = _token_path(name)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(creds.to_json(), encoding="utf-8")
    os.replace(tmp, path)
    _restrict_permissions(path)


def _set_status(account_id: int, status: str, error: str | None) -> None:
    with session_scope() as s:
        acct = s.get(YouTubeAccount, account_id)
        if acct:
            acct.connection_status = status
            acct.last_error = error


def active_account(db: Session) -> YouTubeAccount | None:
    return db.scalar(select(YouTubeAccount).where(YouTubeAccount.is_active.is_(True)).order_by(YouTubeAccount.id.desc()))


def get_credentials(account: YouTubeAccount) -> Credentials:
    """Load credentials and refresh the access token if needed."""
    if not account.credential_reference:
        raise AuthError("No stored credentials. Connect your YouTube channel.", status="disconnected")
    path = _token_path(account.credential_reference)
    if not path.is_file():
        _set_status(account.id, "disconnected", "Token file missing")
        raise AuthError("Stored token file is missing. Reconnect your YouTube channel.", status="disconnected")
    try:
        creds = Credentials.from_authorized_user_info(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        _set_status(account.id, "error", "Token file unreadable")
        raise AuthError("Stored token file is unreadable. Reconnect your YouTube channel.", status="error") from exc

    if not creds.valid:
        if not creds.refresh_token:
            _set_status(account.id, "expired", "No refresh token")
            raise AuthError("No refresh token stored. Reconnect your YouTube channel.", status="expired")
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            text = str(exc)
            status = "revoked" if "revoked" in text.lower() else "expired"
            msg = (
                "Google rejected the refresh token (invalid_grant): it was revoked, expired, or the OAuth app "
                "is in 'Testing' mode (tokens expire after 7 days). Reconnect your YouTube channel."
                if "invalid_grant" in text else f"Token refresh failed: {text[:200]}"
            )
            _set_status(account.id, status, msg)
            record_event("oauth", msg, level="error")
            raise AuthError(msg, status=status) from exc
        except Exception as exc:  # network problem: not an auth failure
            raise ConnectionError(f"Could not reach Google to refresh the token: {exc.__class__.__name__}") from exc
        _save_token(account.credential_reference, creds)
    return creds


def build_service(creds: Credentials):
    if service_factory is not None:
        return service_factory(creds)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def fetch_own_channel(creds: Credentials) -> dict:
    """channels.list(mine=true): verify which channel the token belongs to."""
    service = build_service(creds)
    log_id = quota.begin_request("channels.list", detail="mine=true")
    try:
        resp = service.channels().list(part="snippet", mine=True, maxResults=1).execute()
    except HttpError as exc:
        info = classify_http_error(exc)
        quota.finish_request(log_id, False, info.status, info.reason, info.message)
        if info.kind == "quota":
            quota.mark_exhausted(quota.OTHER, info.message)
        raise AuthError(f"channels.list failed: {info.message}", status="error") from exc
    except Exception as exc:
        quota.finish_request(log_id, False, None, exc.__class__.__name__)
        raise
    quota.finish_request(log_id, True, 200)
    items = resp.get("items") or []
    if not items:
        raise AuthError("This Google account has no YouTube channel. Create a channel first, then reconnect.", status="error")
    item = items[0]
    thumbs = item.get("snippet", {}).get("thumbnails", {})
    thumb = (thumbs.get("default") or thumbs.get("medium") or {}).get("url")
    return {"id": item["id"], "title": item.get("snippet", {}).get("title", ""), "thumbnail": thumb}


def verify_connection(db: Session) -> dict:
    """Refresh the token (if needed) and re-verify the channel. Costs 1 'other' unit."""
    acct = active_account(db)
    if acct is None:
        raise AuthError("No YouTube channel connected.", status="disconnected")
    creds = get_credentials(acct)
    channel = fetch_own_channel(creds)
    if channel["id"] != acct.channel_id:
        _set_status(acct.id, "error", "Token belongs to a different channel")
        raise AuthError("The stored token belongs to a different channel. Reconnect.", status="error")
    _set_status(acct.id, "connected", None)
    return channel


def disconnect(db: Session) -> None:
    acct = active_account(db)
    if acct is None:
        return
    if acct.credential_reference:
        path = _token_path(acct.credential_reference)
        if path.exists():
            # Best-effort revoke at Google, then delete locally.
            try:
                creds = Credentials.from_authorized_user_info(json.loads(path.read_text(encoding="utf-8")))
                import urllib.request, urllib.parse  # noqa: E401
                token = creds.refresh_token or creds.token
                if token:
                    req = urllib.request.Request(
                        "https://oauth2.googleapis.com/revoke",
                        data=urllib.parse.urlencode({"token": token}).encode(),
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                    urllib.request.urlopen(req, timeout=10)
            except Exception:
                log.warning("Token revoke at Google failed (continuing with local removal)")
            path.unlink(missing_ok=True)
    acct.connection_status = "disconnected"
    acct.credential_reference = None
    acct.last_error = None
    record_event("oauth", f"Disconnected channel {acct.channel_name}")


def connection_status(db: Session) -> dict:
    acct = active_account(db)
    base = {
        "client_secret_present": client_secret_available(),
        "redirect_uri": redirect_uri(),
        "requested_scopes": oauth_scopes(),
    }
    if acct is None:
        return {**base, "connected": False, "status": "disconnected", "channel": None}
    token_present = bool(acct.credential_reference) and _token_path(acct.credential_reference).is_file()
    status = acct.connection_status if token_present or acct.connection_status != "connected" else "disconnected"
    return {
        **base,
        "connected": status == "connected",
        "status": status,
        "last_error": acct.last_error,
        "channel": {
            "id": acct.channel_id,
            "name": acct.channel_name,
            "thumbnail_url": acct.thumbnail_url,
        },
        "granted_scopes": (acct.granted_scopes or "").split(),
        "can_reschedule_uploaded": "https://www.googleapis.com/auth/youtube.force-ssl" in (acct.granted_scopes or ""),
    }
