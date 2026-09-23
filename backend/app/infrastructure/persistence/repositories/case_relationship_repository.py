"""SQL repository for :class:`CaseRelationship` (FM-010).

Tenant isolation is mandatory (AGENTS.md §3 R1): every method takes
``tenant_id`` first and every SQL includes ``WHERE tenant_id = :tenant_id``.

The first-slice schema is intentionally minimal — the canonical table
catalog (AGENTS.md §16.1) is extended with ``case_relationships`` in a
later migration. The repository contracts are exercised by the
in-memory test fake in ``tests/application/cases/_fakes.py`` and by the
HTTP router; the SQL adapter is reserved for the migration that lands
the table.

# INTEGRATION_SEAM

This is the local adapter. An Azure SQL adapter would implement the
same ``CaseRelationshipRepository`` protocol defined in
``app.domain.ports``. The application use cases depend only on the
port so the adapter can be swapped without touching them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.case_relationship import (
    CaseRelationship,
    RelationshipKind,
)
from app.domain.errors import InvariantViolation


class SqlCaseRelationshipRepository:
    """SQL adapter for the ``CaseRelationshipRepository`` protocol.

    The first-slice implementation reserves the SQL schema; the
    migration that lands the ``case_relationships`` table is
    deliberately deferred. Every method enforces tenant isolation
    through the explicit ``tenant_id`` parameter; the SQL is composed
    with ``WHERE tenant_id = :tenant_id`` at every read so a missing
    listener (defense-in-depth) cannot silently leak rows across
    tenants.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add_pair(self, edges: tuple[CaseRelationship, CaseRelationship]) -> None:
        """Persist both halves of a bidirectional pair.

        Raises:
            InvariantViolation: when the pair is malformed (different
                tenants, distinct sources, mismatched relationship_id,
                or wrong kind).
        """
        if len(edges) != 2:
            raise InvariantViolation("add_pair requires exactly two edges")
        forward, backward = edges
        if forward.tenant_id != backward.tenant_id:
            raise InvariantViolation("edge pair must share a tenant")
        if forward.relationship_id != backward.relationship_id:
            raise InvariantViolation("edge pair must share a relationship_id")
        if forward.kind is not RelationshipKind.RELATED:
            raise InvariantViolation("add_pair is reserved for kind=RELATED")
        if forward.source_case_id == backward.source_case_id:
            raise InvariantViolation("edge pair must have distinct sources")
        if forward.source_case_id != backward.target_case_id:
            raise InvariantViolation("edge pair must be symmetric")
        # The migration that lands the ``case_relationships`` table is
        # deferred; the adapter stages the INSERTs on the session so
        # the caller's transaction commits both rows atomically. The
        # SQL column list below matches the canonical schema in
        # AGENTS.md §16.1 (id, tenant_id, source_case_id, target_case_id,
        # kind, relationship_id, reason, note, created_at, created_by).
        self._s.add(_row_from_domain(forward))
        self._s.add(_row_from_domain(backward))

    async def add(self, edge: CaseRelationship) -> None:
        """Persist a single-direction edge (split/merge)."""
        if edge.kind is RelationshipKind.RELATED:
            raise InvariantViolation("use add_pair for kind=RELATED")
        self._s.add(_row_from_domain(edge))

    async def list_for_case(
        self,
        tenant_id: UUID,
        case_id: UUID,
        *,
        kinds: Sequence[RelationshipKind] | None = None,
    ) -> Sequence[CaseRelationship]:
        """Return every outgoing edge for the case.

        Tenant-scoped. When ``kinds`` is provided the WHERE clause
        narrows to those kinds; otherwise every kind is returned.
        """
        stmt = select(_CaseRelationshipRow).where(
            _CaseRelationshipRow.tenant_id == tenant_id,
            _CaseRelationshipRow.source_case_id == case_id,
        )
        if kinds:
            stmt = stmt.where(_CaseRelationshipRow.kind.in_([k.value for k in kinds]))
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_domain_from_row(r) for r in rows]

    async def get_pair(self, tenant_id: UUID, relationship_id: UUID) -> Sequence[CaseRelationship]:
        """Return both halves of a relationship by the shared grouping id."""
        stmt = select(_CaseRelationshipRow).where(
            _CaseRelationshipRow.tenant_id == tenant_id,
            _CaseRelationshipRow.relationship_id == relationship_id,
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_domain_from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Inline ORM row + helpers
#
# Defining the row here keeps the repository self-contained while the
# ``case_relationships`` migration is deferred. When the migration
# lands, move the class to ``app.infrastructure.persistence.models``
# to match the canonical catalog (AGENTS.md §16.1).
# ---------------------------------------------------------------------------


import sqlalchemy as sa  # noqa: E402  - kept near the row definition
from sqlalchemy.orm import declarative_base  # noqa: E402

_RelationshipBase = declarative_base()


class _CaseRelationshipRow(_RelationshipBase):
    __tablename__ = "case_relationships"

    id: sa.orm.Mapped[UUID] = sa.orm.mapped_column(sa.String(36), primary_key=True)
    tenant_id: sa.orm.Mapped[UUID] = sa.orm.mapped_column(sa.String(36), nullable=False)
    source_case_id: sa.orm.Mapped[UUID] = sa.orm.mapped_column(sa.String(36), nullable=False)
    target_case_id: sa.orm.Mapped[UUID] = sa.orm.mapped_column(sa.String(36), nullable=False)
    kind: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(32), nullable=False)
    relationship_id: sa.orm.Mapped[UUID] = sa.orm.mapped_column(sa.String(36), nullable=False)
    reason: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(2000), default="")
    note: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(10000), default="")
    created_at: sa.orm.Mapped[datetime] = sa.orm.mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    created_by: sa.orm.Mapped[UUID] = sa.orm.mapped_column(sa.String(36), nullable=False)


def _row_from_domain(edge: CaseRelationship) -> _CaseRelationshipRow:
    return _CaseRelationshipRow(
        id=str(edge.id),
        tenant_id=str(edge.tenant_id),
        source_case_id=str(edge.source_case_id),
        target_case_id=str(edge.target_case_id),
        kind=edge.kind.value,
        relationship_id=str(edge.relationship_id),
        reason=edge.reason,
        note=edge.note,
        created_at=edge.created_at,
        created_by=str(edge.created_by),
    )


def _domain_from_row(row: _CaseRelationshipRow) -> CaseRelationship:
    return CaseRelationship(
        id=UUID(row.id),
        tenant_id=UUID(row.tenant_id),
        source_case_id=UUID(row.source_case_id),
        target_case_id=UUID(row.target_case_id),
        kind=RelationshipKind(row.kind),
        relationship_id=UUID(row.relationship_id),
        created_at=row.created_at,
        created_by=UUID(row.created_by),
        reason=row.reason,
        note=row.note,
    )


__all__ = ["SqlCaseRelationshipRepository"]
