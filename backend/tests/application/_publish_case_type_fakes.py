"""Fakes for the publish_case_type tests.

Mirrors the style of ``tests/application/cases/_fakes.py``. Each fake
captures the exact shape of the protocol method so the test can assert
on every observable side effect (status flip, current-version pointer,
audit row).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.domain.case_type import (
    CaseType,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.errors import InvariantViolation


class FixedClock:
    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now


@dataclass
class StagedAudit:
    tenant_id: UUID
    event_type: str
    actor_id: UUID
    actor_type: str
    subject_type: str
    subject_id: UUID
    case_id: UUID | None
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any]
    occurred_at: datetime


class InMemoryAuditStore:
    def __init__(self) -> None:
        self.events: list[StagedAudit] = []

    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID,
        actor_type: str,
        subject_type: str,
        subject_id: UUID,
        case_id: UUID | None,
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        self.events.append(
            StagedAudit(
                tenant_id=tenant_id,
                event_type=event_type,
                actor_id=actor_id,
                actor_type=actor_type,
                subject_type=subject_type,
                subject_id=subject_id,
                case_id=case_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                payload=payload,
                occurred_at=occurred_at,
            )
        )


class InMemoryCaseTypeRepository:
    """Captures CaseType header state and current_version promotion."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, CaseType] = {}
        self._by_key: dict[tuple[UUID, str], CaseType] = {}
        # Each call to ``set_current_version`` is recorded so a test can
        # assert it was performed in the right order.
        self.set_current_version_calls: list[tuple[UUID, UUID, UUID]] = []

    def add(self, case_type: CaseType) -> None:
        self._by_id[case_type.id] = case_type
        self._by_key[(case_type.tenant_id, case_type.key)] = case_type

    async def get(self, tenant_id: UUID, case_type_id: UUID) -> CaseType | None:
        ct = self._by_id.get(case_type_id)
        if ct is None or ct.tenant_id != tenant_id:
            return None
        return ct

    async def get_by_key(self, tenant_id: UUID, key: str) -> CaseType | None:
        return self._by_key.get((tenant_id, key))

    async def set_current_version(
        self, tenant_id: UUID, case_type_id: UUID, version_id: UUID
    ) -> None:
        ct = self._by_id.get(case_type_id)
        if ct is None or ct.tenant_id != tenant_id:
            raise LookupError("case type not found")
        self._by_id[case_type_id] = replace(ct, current_version_id=version_id)
        self._by_key[(tenant_id, ct.key)] = self._by_id[case_type_id]
        self.set_current_version_calls.append((tenant_id, case_type_id, version_id))


class InMemoryCaseTypeVersionRepository:
    """Captures CaseTypeVersion rows with status transitions."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, CaseTypeVersion] = {}
        # ``mark_status`` transitions are logged so a test can assert the
        # publish use case tried to flip the row.
        self.status_transitions: list[
            tuple[UUID, CaseTypeVersionStatus, CaseTypeVersionStatus]
        ] = []
        self.update_draft_calls: list[UUID] = []

    async def add(self, version: CaseTypeVersion) -> None:
        self._by_id[version.id] = version

    async def get(self, tenant_id: UUID, case_type_version_id: UUID) -> CaseTypeVersion | None:
        v = self._by_id.get(case_type_version_id)
        if v is None or v.tenant_id != tenant_id:
            return None
        return v

    async def get_published_for_case_type(
        self, tenant_id: UUID, case_type_id: UUID
    ) -> CaseTypeVersion | None:
        for v in self._by_id.values():
            if (
                v.tenant_id == tenant_id
                and v.case_type_id == case_type_id
                and v.status is CaseTypeVersionStatus.PUBLISHED
            ):
                return v
        return None

    async def get_latest_for_case_type(
        self, tenant_id: UUID, case_type_id: UUID
    ) -> CaseTypeVersion | None:
        candidates = [
            v
            for v in self._by_id.values()
            if v.tenant_id == tenant_id and v.case_type_id == case_type_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda v: v.version_number)

    async def list_for_case_type(
        self,
        tenant_id: UUID,
        case_type_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CaseTypeVersion]:
        rows = [
            v
            for v in self._by_id.values()
            if v.tenant_id == tenant_id and v.case_type_id == case_type_id
        ]
        rows.sort(key=lambda v: v.version_number, reverse=True)
        return rows[offset : offset + limit]

    async def mark_status(
        self,
        tenant_id: UUID,
        version_id: UUID,
        *,
        new_status: CaseTypeVersionStatus,
        published_at: datetime | None,
        published_by: UUID | None,
    ) -> None:
        v = self._by_id.get(version_id)
        if v is None or v.tenant_id != tenant_id:
            raise LookupError("case type version not found")
        if v.status is not CaseTypeVersionStatus.DRAFT:
            raise InvariantViolation(f"cannot update a {v.status.value} version")
        if new_status is not CaseTypeVersionStatus.PUBLISHED:
            raise InvariantViolation(f"unexpected transition to {new_status.value}")
        self.status_transitions.append((version_id, v.status, new_status))
        self._by_id[version_id] = replace(
            v,
            status=new_status,
            published_at=published_at,
            published_by=published_by,
        )

    async def update_draft(self, version: CaseTypeVersion) -> None:
        self.update_draft_calls.append(version.id)
        v = self._by_id.get(version.id)
        if v is None:
            return
        if v.status is not CaseTypeVersionStatus.DRAFT:
            raise InvariantViolation(f"cannot update a {v.status.value} version")
        self._by_id[version.id] = replace(v, schema=version.schema)


__all__ = [
    "FixedClock",
    "InMemoryAuditStore",
    "InMemoryCaseTypeRepository",
    "InMemoryCaseTypeVersionRepository",
    "StagedAudit",
]
