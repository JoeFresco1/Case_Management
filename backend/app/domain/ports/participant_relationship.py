"""Participant + Case-relationship ports (P01-US2, US5, US7, US8 / FM-010)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from app.domain.case_relationship import CaseRelationship, RelationshipKind
from app.domain.participant import Participant
from app.domain.ports.core import TenantScoped


class ParticipantRepository(TenantScoped, Protocol):
    """Tenant-scoped persistence for case participants."""

    async def add(self, participant: Participant) -> None:
        """Stage a participant insert in the caller's transaction."""

    async def get(self, tenant_id: UUID, participant_id: UUID) -> Participant | None:
        """Return a participant or ``None`` for missing/cross-tenant ids."""

    async def list_for_case(self, tenant_id: UUID, case_id: UUID) -> Sequence[Participant]:
        """Return active and removed participants for a tenant-scoped case."""

    async def update(self, participant: Participant) -> None:
        """Persist participant metadata or its removal tombstone."""


# ---------------------------------------------------------------------------
# Case-relationship port (P01-US5, US7, US8 / FM-010)
#
# Bidirectional ``related`` edges are stored as TWO rows that share a
# common ``relationship_id``; split/merge edges are stored as ONE row
# each (see ``app.domain.case_relationship`` for the rationale). The
# repository never silently dedupes -- the application layer owns that
# policy -- so the surface is intentionally append-only.
#
# Tenant isolation is mandatory (AGENTS.md §3 R1, §12): every method
# takes ``tenant_id`` first and the SQL adapter MUST include ``WHERE
# tenant_id = :tenant_id`` on every query.
# ---------------------------------------------------------------------------


class CaseRelationshipRepository(TenantScoped, Protocol):
    """Append-only :class:`CaseRelationship` persistence (FM-010).

    The repository is the only mutation surface for the edges. Adding
    both halves of a ``related`` pair in one call lets the SQL adapter
    INSERT both rows in the same transaction so a partial commit never
    leaves a one-sided edge (navigation would break for one of the two
    cases). The application use case authorizes the write before the
    add; the repository never silently rewrites history.
    """

    async def add_pair(self, edges: tuple[CaseRelationship, CaseRelationship]) -> None:
        """Persist both halves of a bidirectional ``related`` pair.

        The pair must share the same ``relationship_id`` and
        ``tenant_id`` and the two ``source_case_id`` values must be
        distinct (the domain rejects self-loops at construction).
        """

    async def add(self, edge: CaseRelationship) -> None:
        """Persist a single-direction edge (``split_from`` / ``merged_into``)."""

    async def list_for_case(
        self,
        tenant_id: UUID,
        case_id: UUID,
        *,
        kinds: Sequence[RelationshipKind] | None = None,
    ) -> Sequence[CaseRelationship]:
        """Return every edge where ``case_id`` appears as the source.

        Tenant-scoped (``WHERE tenant_id = :tenant_id``). When ``kinds``
        is supplied, only edges of those kinds are returned; otherwise
        every kind is included. Bidirectional ``related`` edges appear
        exactly once per ``source`` (the half whose ``source_case_id``
        matches ``case_id``); the symmetric half is reachable via
        ``list_for_case(case_id=target)``.
        """

    async def get_pair(self, tenant_id: UUID, relationship_id: UUID) -> Sequence[CaseRelationship]:
        """Return both halves of a relationship by their shared grouping id.

        Used by the operator-console "unlink" flow and by any future
        compensating-write path that needs to find every row the
        relationship owns. Returns an empty sequence for missing or
        cross-tenant ids so the API never confirms cross-tenant
        existence (Constitution V).
        """


__all__ = [
    "CaseRelationshipRepository",
    "ParticipantRepository",
]
