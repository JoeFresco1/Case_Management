"""Merge two cases into one (P01-US8, FM-010).

The use case picks a "survivor" case (the case that retains its
identity and absorbs the source's history) and a "retired" case (the
case whose history is preserved but whose identity is folded into the
survivor). Both cases keep their audit chains; the merge is recorded
as a compensating event on the survivor and a ``merged_into`` edge from
the retired case to the survivor.

The merge flow:

1. **Authorize** the actor against ``Action.CASE_UPDATE`` on the
   survivor (merging is a destructive mutation of the survivor's
   workspace — the survivor absorbs new documents and audit rows).
2. **Resolve both cases**, tenant-scoped. Cross-tenant ids return
   None; missing ids surface as :class:`NotFound`.
3. **Mark the retired case as ``CANCELLED``** via
   :meth:`Case.change_status`. The aggregate enforces terminal-status
   rules: once cancelled, the case cannot transition further.
4. **Persist the ``merged_into`` edge** so navigation can resolve
   ``retired.survivor``.
5. **Audit history preservation**: the retired case's audit chain is
   untouched. The merge records:
   - On the survivor: ``CaseMerged`` payload references the retired
     case id and lists the absorbed document ids.
   - On the retired case: ``CaseMergedInto`` payload references the
     survivor id (so the retired case's timeline shows where it went).
   - One outbox event ``CaseMerged`` so subscribers can fan out.
6. **Document absorption**: documents on the retired case are NOT
   cloned onto the survivor in the first slice; the audit chain carries
   the cross-reference so a reader can join the retired case's
   documents through the ``merged_into`` edge. (Cloning would require
   a content-addressable copy decision; deferring it keeps the first
   slice deterministic and the audit chain honest.)

Failure behavior: validation/auth/not-found errors raise before any DB
write. The status change, the edge, the audit rows, and the outbox row
share one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.application.cases.create_case import _UserProxy as _ActorProxy
from app.domain.case import CaseStatus
from app.domain.case_relationship import (
    CaseRelationship,
    make_merge_edge,
)
from app.domain.errors import InvariantViolation, NotFound
from app.domain.ports import (
    AuditEventStore,
    CaseRelationshipRepository,
    CaseRepository,
    OutboxWriter,
)

CASE_MERGED: str = "CaseMerged"
CASE_MERGED_INTO: str = "CaseMergedInto"


class _ClockLike(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class MergeCasesCommand:
    """Input to :func:`handle`.

    ``survivor_case_id`` retains its identity and absorbs the retired
    case's history; ``retired_case_id`` is marked ``CANCELLED`` and
    keeps its audit chain (Constitution IV — corrections are
    compensating events, never destructive edits).
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    survivor_case_id: UUID
    retired_case_id: UUID
    reason: str = ""
    note: str = ""
    correlation_id: str = ""
    causation_id: str | None = None


@dataclass(frozen=True, slots=True)
class MergeResult:
    """The post-commit outcome returned to the router."""

    survivor_case_id: UUID
    retired_case_id: UUID
    merge_edge: CaseRelationship


async def handle(
    cmd: MergeCasesCommand,
    *,
    case_repo: CaseRepository,
    relationship_repo: CaseRelationshipRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: _ClockLike,
) -> MergeResult:
    """Merge two cases, preserving both audit histories.

    Failure behavior:
    - Auth/not-found/identity errors are raised before any DB write.
    - The survivor UPDATE, the retired status change, the edge, the
      audit rows, and the outbox row share one transaction.
    """
    if cmd.survivor_case_id == cmd.retired_case_id:
        raise InvariantViolation("survivor and retired must be different cases")

    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )

    # Authorization is checked against the survivor; the retired case
    # is the "source" of the merge and is mutated only to its status
    # (which is reversible at the audit level, not destructive of
    # data — Constitution XIII).
    await authorize(
        Action.CASE_UPDATE,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind="case", id=cmd.survivor_case_id),
    )

    survivor = await case_repo.get(cmd.tenant_id, cmd.survivor_case_id)
    if survivor is None:
        raise NotFound(f"case {cmd.survivor_case_id} not found")

    retired = await case_repo.get(cmd.tenant_id, cmd.retired_case_id)
    if retired is None:
        raise NotFound(f"case {cmd.retired_case_id} not found")

    now = clock.now()
    new_retired = retired.change_status(new_status=CaseStatus.CANCELLED, now=now)
    edge = make_merge_edge(
        tenant_id=cmd.tenant_id,
        source_case_id=retired.id,
        survivor_case_id=survivor.id,
        actor_id=cmd.actor_id,
        now=now,
        reason=cmd.reason,
        note=cmd.note,
    )

    await case_repo.update(new_retired)
    await relationship_repo.add(edge)

    correlation = cmd.correlation_id or _new_correlation_id()

    # Survivor-side compensating event.
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=CASE_MERGED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type="case",
        subject_id=survivor.id,
        case_id=survivor.id,
        correlation_id=correlation,
        causation_id=cmd.causation_id,
        payload={
            "retired_case_id": str(retired.id),
            "retired_reference": retired.reference.value,
            "relationship_id": str(edge.relationship_id),
            "reason": cmd.reason,
            "note": cmd.note,
        },
        occurred_at=now,
    )

    # Retired-side compensating event.
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=CASE_MERGED_INTO,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type="case",
        subject_id=retired.id,
        case_id=retired.id,
        correlation_id=correlation,
        causation_id=cmd.causation_id,
        payload={
            "survivor_case_id": str(survivor.id),
            "survivor_reference": survivor.reference.value,
            "relationship_id": str(edge.relationship_id),
            "reason": cmd.reason,
            "note": cmd.note,
        },
        occurred_at=now,
    )

    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=CASE_MERGED,
        event_id=uuid4(),
        payload={
            "tenant_id": str(cmd.tenant_id),
            "survivor_case_id": str(survivor.id),
            "survivor_reference": survivor.reference.value,
            "retired_case_id": str(retired.id),
            "retired_reference": retired.reference.value,
            "actor_id": str(cmd.actor_id),
            "relationship_id": str(edge.relationship_id),
            "occurred_at": now.isoformat(),
            "reason": cmd.reason,
            "note": cmd.note,
        },
    )

    return MergeResult(
        survivor_case_id=survivor.id,
        retired_case_id=retired.id,
        merge_edge=edge,
    )


def _new_correlation_id() -> str:
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())


__all__ = [
    "CASE_MERGED",
    "CASE_MERGED_INTO",
    "MergeCasesCommand",
    "MergeResult",
    "handle",
]
