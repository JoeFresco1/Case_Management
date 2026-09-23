"""Fakes for the governance use-case tests (FM-021).

The fakes mirror the shape of the production adapters so the tests
exercise the same observable contract:

- :class:`InMemoryPublicationRepository` implements the Protocol from
  ``app.application.configuration.governance``; the SQL adapter
  (``SqlConfigurationPublicationRepository``) implements the same
  shape in production.
- :class:`InMemoryGovernancePolicy` answers the second-admin question
  per tenant; the local adapter (``LocalGovernancePolicy``) is the
  production shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.application.configuration.governance import PublicationRecord


class FixedClock:
    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now


class InMemoryPublicationRepository:
    """Append-only in-memory store for ``PublicationRecord``.

    Mirrors the production repository shape: append-only, tenant-
    scoped, no UPDATE/DELETE. The newest-first ordering and the
    artifact filter both happen at read time so the assertions can
    exercise the same path the router would.
    """

    def __init__(self) -> None:
        self._rows: list[PublicationRecord] = []

    async def add(
        self,
        *,
        tenant_id: UUID,
        artifact_type: str,
        artifact_id: UUID,
        version_id: UUID,
        action: str,
        actor_id: UUID,
        change_summary: str,
        occurred_at: datetime,
    ) -> UUID:
        from uuid_extensions import uuid7  # noqa: PLC0415

        new_id = uuid7()
        self._rows.append(
            PublicationRecord(
                id=new_id,
                tenant_id=tenant_id,
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                version_id=version_id,
                action=action,
                actor_id=actor_id,
                occurred_at=occurred_at,
                change_summary=change_summary,
            )
        )
        return new_id

    async def list_for_artifact(
        self,
        tenant_id: UUID,
        artifact_type: str,
        artifact_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PublicationRecord]:
        rows = [
            r
            for r in self._rows
            if r.tenant_id == tenant_id
            and r.artifact_type == artifact_type
            and r.artifact_id == artifact_id
        ]
        # Match the SQL repository: newest-first on (occurred_at, id).
        # The id tie-breaker keeps order stable when the fixed clock
        # produces identical timestamps — the most recently inserted
        # row is the newest.
        rows.sort(key=lambda r: (r.occurred_at, r.id), reverse=True)
        return rows[offset : offset + limit]

    async def list_tenant_history(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        artifact_type: str | None = None,
    ) -> list[PublicationRecord]:
        rows = [r for r in self._rows if r.tenant_id == tenant_id]
        if artifact_type is not None:
            rows = [r for r in rows if r.artifact_type == artifact_type]
        rows.sort(key=lambda r: (r.occurred_at, r.id), reverse=True)
        return rows[offset : offset + limit]


class InMemoryGovernancePolicy:
    """Per-tenant toggle for the second-admin approval gate (P10-US8)."""

    def __init__(self, *, require_second_admin_for: set[UUID] | None = None) -> None:
        self._tenants: set[UUID] = set(require_second_admin_for or ())

    async def require_second_admin(self, tenant_id: UUID) -> bool:
        return tenant_id in self._tenants


__all__ = [
    "FixedClock",
    "InMemoryGovernancePolicy",
    "InMemoryPublicationRepository",
]
