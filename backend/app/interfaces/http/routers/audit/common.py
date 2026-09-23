"""Shared audit router, ISO timestamp parser, and timeline projection.

# Why this module (R2 structural split)

The former single-file ``audit.py`` module (747 lines) passed the R2
600-line soft limit. This module holds the pieces every route group in
the package shares:

- :data:`router` — the one ``APIRouter(tags=["audit"])`` that the route
  modules (:mod:`timeline`, :mod:`export`, :mod:`correction`,
  :mod:`access_decisions`, :mod:`signed_batches`) register onto.
  Registration order is irrelevant: no two audit paths overlap at the
  same segment depth and method, so Starlette's first-match rule
  resolves identically regardless of import order.
- :func:`_iso_to_naive_utc` — flexible ISO-8601 wire timestamp parser
  used to keep FastAPI's strict ``datetime`` parsing from turning a
  slightly-varied ``+00:00``/``Z`` timestamp into a confusing 422.
- :func:`_entry_to_response` — the application-layer timeline dataclass
  → wire schema projection (the Pydantic schema is the OpenAPI
  contract; hand-editing fields at a route is forbidden).

# INTEGRATION_SEAM: HTTP entry

This package is the only HTTP entry to the per-case audit timeline and
the audit read/write/report surface (see the package docstring in
:mod:`app.interfaces.http.routers.audit` for the endpoint inventory).
The handlers delegate the business decision to the application layer
under ``app.application.audit``; the router only parses the wire input
and maps results/errors. All persistence reaches the process through
the ``AuditHttpDependencyProvider`` session bundle, so SQL and Azure
adapters swap in at composition time without touching this package.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter

from app.application.audit import view_timeline as view_timeline_uc
from app.domain.errors import ValidationFailed
from app.interfaces.http.schemas.audit import TimelineEntryResponse

router = APIRouter(tags=["audit"])


def _iso_to_naive_utc(value: str) -> datetime:
    """Parse a wire ISO-8601 timestamp into a tz-aware ``datetime``.

    Why a helper: FastAPI's built-in ``datetime`` parser is strict
    (it rejects ``2026-08-31T12:45:00+00:00`` for some inputs, accepts
    others) and the failure mode is a 422 with a confusing
    validation error. This helper keeps the wire format flexible
    (``+00:00``, ``Z``, with or without microseconds) and surfaces a
    clear ``ValidationFailed`` on bad input.

    Args:
        value: Raw ISO-8601 timestamp from the wire.

    Returns:
        The parsed ``datetime``.

    Raises:
        ValidationFailed: when ``value`` is not parseable as ISO-8601.
    """
    try:
        # ``datetime.fromisoformat`` accepts ``+00:00`` but not ``Z`` on
        # Python <3.11; on 3.12 it accepts both.
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationFailed(
            f"invalid ISO-8601 timestamp: {value!r}",
            field="since_until",
        ) from exc


def _entry_to_response(entry: view_timeline_uc.TimelineEntry) -> TimelineEntryResponse:
    """Translate the application-layer dataclass into the wire model.

    Why centralized: the Pydantic schema is the OpenAPI contract;
    hand-editing fields at the router is forbidden (AGENTS.md §4.R7).

    Why a dict round-trip: Pydantic's ``model_validate`` accepts either
    a dict or another Pydantic model — not a plain dataclass — so we
    project the dataclass to a dict via ``dataclasses.asdict``. The
    field names match the schema, so the conversion is mechanical.

    Args:
        entry: Application-layer timeline entry dataclass.

    Returns:
        The wire ``TimelineEntryResponse``.
    """
    return TimelineEntryResponse.model_validate(asdict(entry))
