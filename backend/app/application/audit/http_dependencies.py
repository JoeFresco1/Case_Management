"""Dependency contracts for the audit HTTP composition boundary.

The audit router needs several ports and persistence-owned codecs bound to
one request transaction. This module defines that bundle without importing
FastAPI, SQLAlchemy, or a concrete infrastructure adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.application.audit.record_event import Dedupe
from app.domain.ports import AuditEventStore, CaseRepository

CursorEncoder = Callable[[datetime, Any], str]
CursorDecoder = Callable[[str], tuple[datetime, UUID]]


class AuditCorrectionLookup(Protocol):
    """Read-only lookup for the id of a newly appended correction row."""

    async def latest_correction_id(
        self,
        *,
        tenant_id: UUID,
        correlation_id: str,
    ) -> UUID | None:
        """Return the latest correction id for a tenant-scoped correlation."""

        ...


@dataclass(frozen=True, slots=True)
class AuditHttpRepositories:
    """Ports and codecs required by the audit HTTP endpoints."""

    audit: AuditEventStore
    case: CaseRepository
    dedupe: Dedupe
    cursor_encoder: CursorEncoder
    cursor_decoder: CursorDecoder
    correction_lookup: AuditCorrectionLookup


class AuditHttpDependencyProvider(Protocol):
    """Create a request-scoped audit port bundle from a persistence session."""

    def for_session(self, session: object) -> AuditHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "AuditCorrectionLookup",
    "AuditHttpDependencyProvider",
    "AuditHttpRepositories",
    "CursorDecoder",
    "CursorEncoder",
]
