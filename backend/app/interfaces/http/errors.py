"""Structured error model and exception → response mapping.

No stack traces, no SQL, no protected data reaches the client.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.application.telemetry.redaction import redact_sensitive
from app.deps import CorrelationContext
from app.domain.errors import (
    Conflict,
    DomainError,
    Forbidden,
    InvariantViolation,
    NotFound,
    UnsupportedTransition,
    ValidationFailed,
)


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None
    correlation_id: str | None = None


def error_response(
    *,
    code: str,
    message: str,
    status_code: int,
    correlation_id: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    # why: error details render caller-supplied field names/values
    # (e.g. ValidationFailed messages echo the rejected payload). Run
    # them through the shared key-based redactor (FM-011,
    # ``app.application.telemetry.redaction``) so a sensitive value can
    # never leave the process inside a 4xx body. The redactor is pure
    # (no I/O, never raises for mapping input); the fallback below is
    # defense-in-depth so a 4xx path can never itself become a 500.
    safe_details = details
    if details is not None:
        try:
            safe_details = redact_sensitive(details)
        except Exception:  # pragma: no cover - redactor never raises
            safe_details = {"redacted": True}
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            code=code,
            message=message,
            details=safe_details,
            correlation_id=correlation_id,
        ).model_dump(exclude_none=True),
    )


def _correlation_id(request: Request) -> str:
    ctx = getattr(request.state, "correlation", None)
    if isinstance(ctx, CorrelationContext):
        return ctx.correlation_id
    return ""


async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    cid = _correlation_id(request)
    if isinstance(exc, ValidationFailed):
        return error_response(
            code="validation_failed",
            message=str(exc),
            status_code=422,
            correlation_id=cid,
            details={"field": exc.field} if exc.field else None,
        )
    if isinstance(exc, NotFound):
        return error_response(
            code="not_found",
            message=str(exc),
            status_code=404,
            correlation_id=cid,
        )
    if isinstance(exc, Forbidden):
        # why: SoD denials (FM-017) carry a machine-readable reason so the
        # client can render "needs a second actor" without re-reading the
        # workflow definition. Generic denials carry no details (no oracle).
        details = dict(getattr(exc, "details", {}) or {})
        return error_response(
            code="forbidden",
            message=str(exc),
            status_code=403,
            correlation_id=cid,
            details=details or None,
        )
    if isinstance(exc, (Conflict, InvariantViolation)):
        # why: concurrent config writes (double publish, edit of a
        # published version, invalid lifecycle transition) raise
        # InvariantViolation in domain/application. Without this branch
        # they fall through to 500 internal_error, hiding a client
        # retry conflict as a server fault and risking silent
        # overwrite assumptions. Map to 409 structured conflict so the
        # caller refetches and retries explicitly (FM-021 acceptance).
        #
        # T5 checkout conflicts (``CheckoutConflict``) carry the live
        # holder + expiry via ``to_details()``; include them so the
        # 409 body renders the structured ``conflict`` envelope
        # (holder + expiry) without the client probing lock state.
        details = None
        to_details = getattr(exc, "to_details", None)
        if callable(to_details):
            raw = to_details()
            if isinstance(raw, dict):
                details = {str(k): v for k, v in raw.items()}
        return error_response(
            code="conflict",
            message=str(exc),
            status_code=409,
            correlation_id=cid,
            details=details,
        )
    if isinstance(exc, UnsupportedTransition):
        return error_response(
            code="unsupported_transition",
            message=str(exc),
            status_code=409,
            correlation_id=cid,
        )
    return error_response(
        code="internal_error",
        message="an internal error occurred",
        status_code=500,
        correlation_id=cid,
    )
