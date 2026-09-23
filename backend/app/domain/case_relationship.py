"""Case relationships and restructuring (P01-US5, US7, US8, FM-010).

A :class:`CaseRelationship` is the first-class, append-only edge between
two cases. The relationship is **bidirectional**: storing one row per
direction makes ``list_related(source)`` and ``list_related(target)``
symmetric without a join. Splitting and merging build on the same edge
primitive so navigation (``related``/``split_from``/``merged_into``)
behaves the same way for every relationship shape.

Why the domain owns the relationship types and invariants rather than
the application layer: the application use cases are orchestrations
that compose the edge with authorization, audit, and outbox; the
*meaning* of the edge (``related`` vs ``split_from`` vs ``merged_into``)
and the prohibition on certain transitions (e.g. a self-loop, a
``related`` chain that points to itself) belong here. Putting them in
the domain makes the rules testable without any FastAPI / SQLAlchemy /
audit imports (AGENTS.md §4.R6).

# INTEGRATION_SEAM: relational DB

The :class:`CaseRelationshipRepository` port is the persistence seam;
the SQL adapter lives in
``app.infrastructure.persistence.repositories.case_relationship_repository``
and is reserved. The application use cases depend only on the port so
the SQL adapter can be swapped without touching this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation


class RelationshipKind(str, Enum):  # noqa: UP042 - intentional str+Enum for JSON
    """The semantic kind of an edge between two cases (FM-010).

    Three kinds cover the P01 user stories:

    - ``related`` — symmetric peer association (US5). Stored as one row
      per direction so each case can navigate its peers without a join.
    - ``split_from`` — the source case was split into the target case
      (US7). Stored as one row ``(source=original, target=new)``;
      ``original.children`` is just ``list_related(original,
      kind=split_from)``.
    - ``merged_into`` — the source case was merged into the target case
      (US8). Stored as one row ``(source=old, target=survivor)``;
      ``old.survivor`` resolves the survivor.
    """

    RELATED = "related"
    SPLIT_FROM = "split_from"
    MERGED_INTO = "merged_into"


# A relationship must always point from one case to a *different* case.
# Self-loops are rejected at construction time so navigation code never
# has to defend against them.
def _ensure_distinct(left: UUID, right: UUID) -> None:
    if left == right:
        raise InvariantViolation("a case cannot relate to itself")


@dataclass(frozen=True, slots=True)
class CaseRelationship:
    """One direction of an edge between two cases (FM-010).

    Bidirectional ``related`` links are stored as TWO rows: one with
    ``source=left, target=right`` and one with ``source=right,
    target=left``. The pair shares the same ``relationship_id`` so a
    delete-of-the-relationship can find both halves in one query.
    Split/merge edges are stored as ONE row each (see the docstring on
    :class:`RelationshipKind`).

    The aggregate is append-only; updates to ``kind`` would corrupt
    history and are not allowed. Deletion is reserved for the operator
    console ("unlink" user story); the application layer uses a
    compensating :class:`AuditCorrectionRecorded`-style event so the
    audit chain stays intact (Constitution IV / P09-US7).
    """

    id: UUID
    tenant_id: UUID
    source_case_id: UUID
    target_case_id: UUID
    kind: RelationshipKind
    relationship_id: UUID  # groups both halves of a bidirectional pair
    created_at: datetime
    created_by: UUID
    # P01-US5 / FM-010 provenance: optional human-readable reason and a
    # free-form ``note`` recorded at creation time so the audit payload
    # always carries the *why* (not only the *what*). Kept on the
    # aggregate so navigation reads can surface provenance without a
    # join to the audit chain.
    reason: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        _ensure_distinct(self.source_case_id, self.target_case_id)
        if not self.relationship_id:
            raise InvariantViolation("relationship_id is required")


# ---- factories --------------------------------------------------------------


def new_relationship_id() -> UUID:
    """Allocate the grouping id that both halves of a bidirectional pair share.

    Why a dedicated helper: a single id groups the two directions of a
    ``related`` link so a future "delete this relationship" operation
    can find both rows in one query. Split/merge edges each allocate
    one id (single direction).
    """
    return uuid4()


def make_related_pair(
    *,
    tenant_id: UUID,
    left_case_id: UUID,
    right_case_id: UUID,
    actor_id: UUID,
    now: datetime,
    reason: str = "",
    note: str = "",
) -> tuple[CaseRelationship, CaseRelationship]:
    """Create the two halves of a bidirectional ``related`` link (P01-US5).

    Returns a tuple ``(left_to_right, right_to_left)``. Both rows share
    the same :attr:`relationship_id` so navigation can ignore direction
    when listing the peer set.
    """
    rel_id = new_relationship_id()
    forward = CaseRelationship(
        id=uuid4(),
        tenant_id=tenant_id,
        source_case_id=left_case_id,
        target_case_id=right_case_id,
        kind=RelationshipKind.RELATED,
        relationship_id=rel_id,
        created_at=now,
        created_by=actor_id,
        reason=reason,
        note=note,
    )
    backward = CaseRelationship(
        id=uuid4(),
        tenant_id=tenant_id,
        source_case_id=right_case_id,
        target_case_id=left_case_id,
        kind=RelationshipKind.RELATED,
        relationship_id=rel_id,
        created_at=now,
        created_by=actor_id,
        reason=reason,
        note=note,
    )
    return forward, backward


def make_split_edge(
    *,
    tenant_id: UUID,
    original_case_id: UUID,
    new_case_id: UUID,
    actor_id: UUID,
    now: datetime,
    reason: str = "",
    note: str = "",
) -> CaseRelationship:
    """Create the single ``split_from`` edge (P01-US7).

    Direction: ``original -> new``. The reverse navigation
    (``new.split_from``) is computed by the application layer as the
    inverse query (``source=new, kind=split_from`` -> target).
    """
    return CaseRelationship(
        id=uuid4(),
        tenant_id=tenant_id,
        source_case_id=original_case_id,
        target_case_id=new_case_id,
        kind=RelationshipKind.SPLIT_FROM,
        relationship_id=new_relationship_id(),
        created_at=now,
        created_by=actor_id,
        reason=reason,
        note=note,
    )


def make_merge_edge(
    *,
    tenant_id: UUID,
    source_case_id: UUID,
    survivor_case_id: UUID,
    actor_id: UUID,
    now: datetime,
    reason: str = "",
    note: str = "",
) -> CaseRelationship:
    """Create the single ``merged_into`` edge (P01-US8).

    Direction: ``source (retired) -> survivor``. The survivor carries
    no incoming merge edge from itself; only the retired case's history
    points at the survivor.
    """
    return CaseRelationship(
        id=uuid4(),
        tenant_id=tenant_id,
        source_case_id=source_case_id,
        target_case_id=survivor_case_id,
        kind=RelationshipKind.MERGED_INTO,
        relationship_id=new_relationship_id(),
        created_at=now,
        created_by=actor_id,
        reason=reason,
        note=note,
    )


__all__ = [
    "CaseRelationship",
    "RelationshipKind",
    "make_related_pair",
    "make_merge_edge",
    "make_split_edge",
    "new_relationship_id",
]
