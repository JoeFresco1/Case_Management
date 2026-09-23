"""Framework- and infrastructure-free request context values.

The HTTP dependency layer writes correlation, identity, and request metadata
here. Infrastructure adapters may project these values into their own logging
or tracing systems, but this module must remain safe for interface imports.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CorrelationContext:
    """Carry the correlation id associated with one HTTP request."""

    correlation_id: str


CORRELATION_ID = "correlation_id"
TENANT_ID = "tenant_id"
ACTOR_ID = "actor_id"
METHOD = "method"
PATH = "path"

_UNSET = object()
_values: dict[str, ContextVar[object]] = {
    key: ContextVar(f"request_context_{key}", default=_UNSET)
    for key in (CORRELATION_ID, TENANT_ID, ACTOR_ID, METHOD, PATH)
}


def bind_correlation(correlation_id: str) -> None:
    """Store the request correlation id in the current execution context."""
    _values[CORRELATION_ID].set(correlation_id)


def bind_request_metadata(*, method: str, path: str) -> None:
    """Store HTTP method and path in the current execution context."""
    _values[METHOD].set(method)
    _values[PATH].set(path)


def bind_actor(*, actor_id: str | None, tenant_id: str | None) -> None:
    """Store resolved actor and tenant values in the current context.

    ``None`` is intentionally retained when supplied so infrastructure
    telemetry preserves the prior behavior for unauthenticated requests.
    """
    _values[ACTOR_ID].set(actor_id)
    _values[TENANT_ID].set(tenant_id)


def clear() -> None:
    """Clear all request values from the current execution context.

    Why clear: request context must not bleed into the next request handled by
    the same worker, which would misattribute logs and audit diagnostics.
    """
    for value in _values.values():
        value.set(_UNSET)


def current_context() -> dict[str, Any]:
    """Return a snapshot of the values bound in the current context."""
    return {key: value.get() for key, value in _values.items() if value.get() is not _UNSET}


__all__ = [
    "ACTOR_ID",
    "CORRELATION_ID",
    "METHOD",
    "PATH",
    "TENANT_ID",
    "CorrelationContext",
    "bind_actor",
    "bind_correlation",
    "bind_request_metadata",
    "clear",
    "current_context",
]
