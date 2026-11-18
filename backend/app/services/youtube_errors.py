"""Classification of YouTube Data API errors."""
from __future__ import annotations

import json
from dataclasses import dataclass

from googleapiclient.errors import HttpError

RETRYABLE_STATUS = {500, 502, 503, 504}
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded", "rateLimitExceeded_daily"}
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}


class AuthError(Exception):
    """Credentials are missing, expired or revoked. User must reconnect."""

    def __init__(self, message: str, status: str = "expired"):
        super().__init__(message)
        self.status = status  # expired | revoked | disconnected | error


@dataclass
class ApiErrorInfo:
    kind: str          # quota | rate | server | auth | permanent
    status: int | None
    reason: str | None
    message: str
    retryable: bool


def classify_http_error(err: HttpError) -> ApiErrorInfo:
    status = getattr(err.resp, "status", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    reason, message = None, str(err)
    try:
        payload = json.loads(err.content.decode("utf-8") if isinstance(err.content, bytes) else err.content)
        error = payload.get("error", {})
        message = error.get("message", message)
        errors = error.get("errors") or []
        if errors:
            reason = errors[0].get("reason")
    except Exception:
        pass

    if reason in QUOTA_REASONS:
        return ApiErrorInfo("quota", status, reason, message, retryable=False)
    if status == 401:
        return ApiErrorInfo("auth", status, reason, message, retryable=False)
    if status == 429 or reason in RATE_REASONS:
        return ApiErrorInfo("rate", status, reason, message, retryable=True)
    if status in RETRYABLE_STATUS:
        return ApiErrorInfo("server", status, reason, message, retryable=True)
    if status == 403 and reason in {"forbidden", "insufficientPermissions", "youtubeSignupRequired"}:
        return ApiErrorInfo("auth", status, reason, message, retryable=False)
    return ApiErrorInfo("permanent", status, reason, message, retryable=False)
