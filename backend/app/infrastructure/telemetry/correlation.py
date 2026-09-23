"""Telemetry adapter for correlation/causation IDs and request metadata.

AGENTS.md §18 observability + R12 + plan.md:39 (X-Correlation-Id).

The ambient values live in :mod:`app.context`, which is intentionally free of
structlog and FastAPI so interface modules can depend on ``app.deps`` without
an interface-to-infrastructure import path. This adapter retains the stable
telemetry-facing functions used by the composition root and tests.
"""

from __future__ import annotations

import structlog

from app import context as request_context

# Standard contextvar key names (AGENTS.md §18). These are the keys that
# MUST appear on every log line emitted inside a request scope.
ACTOR_ID = request_context.ACTOR_ID
CORRELATION_ID = request_context.CORRELATION_ID
METHOD = request_context.METHOD
PATH = request_context.PATH
TENANT_ID = request_context.TENANT_ID


def bind_correlation(correlation_id: str) -> None:
    """Bind the request's correlation id for telemetry consumers."""
    request_context.bind_correlation(correlation_id)


def bind_request_metadata(*, method: str, path: str) -> None:
    """Bind HTTP method and path so log lines can be filtered by route."""
    request_context.bind_request_metadata(method=method, path=path)


def bind_actor(*, actor_id: str | None, tenant_id: str | None) -> None:
    """Bind actor + tenant when the identity dependency resolves them.

    Both values are strings (UUID hex). Tenant and actor are passed as
    ``None`` until the identity dependency runs, so a log line emitted
    before authentication may carry empty values — this is acceptable
    because ``/api/v1/health`` and startup logs intentionally run before
    any user context exists.
    """
    request_context.bind_actor(actor_id=actor_id, tenant_id=tenant_id)


def clear() -> None:
    """Clear all bound contextvars at the end of a request.

    Why clear: structlog's contextvars are process-global. Without
    clearing, the next request would inherit the previous request's
    correlation_id, which would silently misattribute log lines.
    """
    request_context.clear()
    structlog.contextvars.clear_contextvars()


def current_context() -> dict[str, object]:
    """Snapshot the currently bound context — used by tests."""
    return request_context.current_context()


__all__ = [
    "ACTOR_ID",
    "CORRELATION_ID",
    "METHOD",
    "PATH",
    "TENANT_ID",
    "bind_actor",
    "bind_correlation",
    "bind_request_metadata",
    "clear",
    "current_context",
]
