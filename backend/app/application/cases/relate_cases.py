"""Relate two cases with a bidirectional peer edge (P01-US5, FM-010).

The use case is the canonical atomic relate flow:

1. **Authorize** the actor against ``Action.CASE_UPDATE`` on the source
   case (the operation mutates the case workspace; the actor must
   already have update rights — P11-US2 / FM-002).
2. **Resolve both cases**, tenant-scoped. A missing or cross-tenant id
   surfaces as :class:`NotFound`; the two cases must be distinct (the
   domain factory rejects self-loops at construction — defense-in-depth
   on top of the explicit check).
3. **Persist** both halves of the ``related`` pair, **one audit row
   per case** (so each case's timeline shows the link in the same
   transaction), and **one outbox event** describing the relationship
   so subscribers can fan out (search index, channel notifications).
4. **Idempotent replay**: a second call with the same ``(tenant_id,
   left, right)`` pair returns the prior relationship ids without
   staging a second edge — the dedup is at the application layer (see
   :func:`_already_related`).

# INTEGRATION_SEAM: HTTP entry

``POST /api/v1/cases/{case_id}/relations`` is the canonical entry
point. The router mounts it under the cases router (or a dedicated
``/cases/{id}/relations`` sub-resource); this use case is naive about
keys and trusts the router for HTTP-shape concerns (idempotency-key
replays, ETag, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.domain.audit import (
    ALL_EVENT_TYPES,
)
from app.domain.case_relationship import (
    CaseRelationship,
    RelationshipKind,
    make_related_pair,
)
from app.domain.errors import InvariantViolation, NotFound
from app.domain.ports import (
    AuditEventStore,
    CaseRelationshipRepository,
    CaseRepository,
    OutboxWriter,
)

# A new event type for relationship creation. We do not append to the
# frozen ``ALL_EVENT_TYPES`` set in ``app.domain.audit`` (the canonical
# catalog) — instead we extend the local string used by the audit +
# outbox writers. The chain replay verifier ignores event-type
# membership and accepts any string, so the relationship row is
# readable by the timeline endpoint regardless of catalog membership.
# Why: extending the canonical catalog requires touching every
# subscriber's allow-list; the relationship event has no such
# subscribers in the first slice.
RELATIONSHIP_CREATED: str = "CaseRelationshipCreated"


class _ClockLike(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class RelateCasesCommand:
    """Input to :func:`handle`."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    source_case_id: UUID
    target_case_id: UUID
    reason: str = ""
    note: str = ""
    correlation_id: str = ""
    causation_id: str | None = None


@dataclass(frozen=True, slots=True)
class RelateResult:
    """The post-commit outcome returned to the router."""

    source_edge: CaseRelationship
    target_edge: CaseRelationship


async def handle(
    cmd: RelateCasesCommand,
    *,
    case_repo: CaseRepository,
    relationship_repo: CaseRelationshipRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: _ClockLike,
) -> RelateResult:
    """Create a bidirectional ``related`` edge between two cases.

    Failure behavior:
    - Auth/not-found/self-loop errors are raised before any DB write.
    - The pair INSERT, the two audit rows, and the outbox row share one
      transaction; the caller commits once. If the audit or outbox
      write fails the case row inserts are rolled back.
    - A second call with the same pair returns the prior edges without
      staging a duplicate (application-level idempotency).
    """
    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )

    if cmd.source_case_id == cmd.target_case_id:
        # Domain factory also rejects this, but the explicit early check
        # produces a clearer error message at the use-case boundary.
        raise InvariantViolation("cannot relate a case to itself")

    # 1. Authorize against the source case. Both cases belong to the
    # actor's tenant by construction; the policy's cross-tenant check
    # is belt-and-suspenders.
    await authorize(
        Action.CASE_UPDATE,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind="case", id=cmd.source_case_id),
    )

    # 2. Resolve both cases. Cross-tenant ids return None so the API
    # never confirms cross-tenant existence (Constitution V).
    source = await case_repo.get(cmd.tenant_id, cmd.source_case_id)
    if source is None:
        raise NotFound(f"case {cmd.source_case_id} not found")
    target = await case_repo.get(cmd.tenant_id, cmd.target_case_id)
    if target is None:
        raise NotFound(f"case {cmd.target_case_id} not found")

    # 3. Idempotency at the application layer. A second call with the
    # same (tenant, source, target, kind=RELATED) pair returns the
    # prior forward edge + the reverse edge without staging a duplicate.
    existing = await _already_related(
        relationship_repo, cmd.tenant_id, cmd.source_case_id, cmd.target_case_id
    )
    if existing is not None:
        forward, backward = existing
        return RelateResult(source_edge=forward, target_edge=backward)

    now = clock.now()
    forward, backward = make_related_pair(
        tenant_id=cmd.tenant_id,
        left_case_id=cmd.source_case_id,
        right_case_id=cmd.target_case_id,
        actor_id=cmd.actor_id,
        now=now,
        reason=cmd.reason,
        note=cmd.note,
    )

    # 4. Persist the pair + two audit rows + outbox in one transaction.
    await relationship_repo.add_pair((forward, backward))

    correlation = cmd.correlation_id or _new_correlation_id()

    # Audit row on the source: "case A linked to case B".
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=RELATIONSHIP_CREATED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type="case",
        subject_id=forward.source_case_id,
        case_id=forward.source_case_id,
        correlation_id=correlation,
        causation_id=cmd.causation_id,
        payload={
            "relationship_id": str(forward.relationship_id),
            "kind": RelationshipKind.RELATED.value,
            "direction": "outgoing",
            "related_case_id": str(forward.target_case_id),
            "related_reference": target.reference.value,
            "reason": cmd.reason,
            "note": cmd.note,
        },
        occurred_at=now,
    )
    # Audit row on the target: "case B was linked to by case A".
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=RELATIONSHIP_CREATED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type="case",
        subject_id=backward.source_case_id,
        case_id=backward.source_case_id,
        correlation_id=correlation,
        causation_id=cmd.causation_id,
        payload={
            "relationship_id": str(backward.relationship_id),
            "kind": RelationshipKind.RELATED.value,
            "direction": "incoming",
            "related_case_id": str(backward.target_case_id),
            "related_reference": source.reference.value,
            "reason": cmd.reason,
            "note": cmd.note,
        },
        occurred_at=now,
    )

    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=RELATIONSHIP_CREATED,
        event_id=uuid4(),
        payload={
            "tenant_id": str(cmd.tenant_id),
            "relationship_id": str(forward.relationship_id),
            "kind": RelationshipKind.RELATED.value,
            "source_case_id": str(forward.source_case_id),
            "target_case_id": str(forward.target_case_id),
            "actor_id": str(cmd.actor_id),
            "occurred_at": now.isoformat(),
            "reason": cmd.reason,
            "note": cmd.note,
        },
    )

    return RelateResult(source_edge=forward, target_edge=backward)


# ---- helpers ---------------------------------------------------------------


async def _already_related(
    repo: CaseRelationshipRepository,
    tenant_id: UUID,
    source_case_id: UUID,
    target_case_id: UUID,
) -> tuple[CaseRelationship, CaseRelationship] | None:
    """Return ``(forward, backward)`` if the pair already exists.

    A pair is "already related" when an outgoing ``related`` edge from
    ``source`` to ``target`` exists. The reverse half is then located
    by the shared ``relationship_id``. Returns ``None`` when no such
    edge exists so the caller can stage a fresh INSERT.
    """
    outgoing = [
        e
        for e in await repo.list_for_case(
            tenant_id, source_case_id, kinds=[RelationshipKind.RELATED]
        )
        if e.target_case_id == target_case_id
    ]
    if not outgoing:
        return None
    forward = outgoing[0]
    pair = await repo.get_pair(tenant_id, forward.relationship_id)
    if len(pair) < 2:
        return None
    backward = next(e for e in pair if e.id != forward.id)
    return forward, backward


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy (see create_case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


def _new_correlation_id() -> str:
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())


# Re-export the catalog symbol so the import path is stable even though
# the relationship event type is local.
_ = ALL_EVENT_TYPES


__all__ = [
    "RELATIONSHIP_CREATED",
    "RelateCasesCommand",
    "RelateResult",
    "handle",
]
