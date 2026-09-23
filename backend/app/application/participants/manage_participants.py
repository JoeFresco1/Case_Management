"""Add, update, and logically remove case participants (FR-004)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.domain.case import Case
from app.domain.context import UserContext
from app.domain.errors import NotFound
from app.domain.participant import Participant
from app.domain.ports import AuditEventStore, CaseRepository, OutboxWriter, ParticipantRepository

PARTICIPANT_ADDED = "ParticipantAdded"
PARTICIPANT_UPDATED = "ParticipantUpdated"
PARTICIPANT_REMOVED = "ParticipantRemoved"


class _Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class AddParticipantCommand:
    """Input for adding a participant."""

    tenant_id: UUID
    case_id: UUID
    actor: UserContext
    role: str
    display_name: str
    source: str
    external_ref: str | None = None
    is_primary: bool = False
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class UpdateParticipantCommand:
    """Input for changing participant metadata."""

    tenant_id: UUID
    participant_id: UUID
    case_id: UUID
    actor: UserContext
    role: str | None = None
    display_name: str | None = None
    source: str | None = None
    external_ref: str | None = None
    is_primary: bool | None = None
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class RemoveParticipantCommand:
    """Input for logically removing a participant."""

    tenant_id: UUID
    participant_id: UUID
    case_id: UUID
    actor: UserContext
    correlation_id: str = ""


async def add_participant(
    cmd: AddParticipantCommand,
    *,
    case_repo: CaseRepository,
    participant_repo: ParticipantRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: _Clock,
) -> Participant:
    """Authorize and atomically add a participant to an existing case."""
    case = await _authorize_case(cmd.tenant_id, cmd.case_id, cmd.actor, case_repo)
    now = clock.now()
    participant = Participant.create(
        tenant_id=cmd.tenant_id,
        case_id=case.id,
        role=cmd.role,
        display_name=cmd.display_name,
        source=cmd.source,
        external_ref=cmd.external_ref,
        is_primary=cmd.is_primary,
        added_at=now,
    )
    await participant_repo.add(participant)
    await _record(
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor.user_id,
        correlation_id=cmd.correlation_id,
        participant=participant,
        event_type=PARTICIPANT_ADDED,
        audit=audit,
        outbox=outbox,
        now=now,
        changes={"role": participant.role, "display_name": participant.display_name},
    )
    return participant


async def update_participant(
    cmd: UpdateParticipantCommand,
    *,
    participant_repo: ParticipantRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: _Clock,
) -> Participant:
    """Authorize and update participant metadata, retaining its identity."""
    participant = await participant_repo.get(cmd.tenant_id, cmd.participant_id)
    if participant is None:
        raise NotFound(f"participant {cmd.participant_id} not found")
    if participant.case_id != cmd.case_id:
        raise NotFound(f"participant {cmd.participant_id} not found")
    await authorize(
        Action.CASE_UPDATE,
        cmd.actor,
        Resource(tenant_id=cmd.tenant_id, kind="case", id=participant.case_id),
    )
    updated = participant.update(
        role=cmd.role,
        display_name=cmd.display_name,
        source=cmd.source,
        external_ref=cmd.external_ref,
        is_primary=cmd.is_primary,
    )
    now = clock.now()
    await participant_repo.update(updated)
    await _record(
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor.user_id,
        correlation_id=cmd.correlation_id,
        participant=updated,
        event_type=PARTICIPANT_UPDATED,
        audit=audit,
        outbox=outbox,
        now=now,
        changes={"role": updated.role, "display_name": updated.display_name},
    )
    return updated


async def remove_participant(
    cmd: RemoveParticipantCommand,
    *,
    participant_repo: ParticipantRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: _Clock,
) -> Participant:
    """Tombstone a participant so historical references remain resolvable."""
    participant = await participant_repo.get(cmd.tenant_id, cmd.participant_id)
    if participant is None:
        raise NotFound(f"participant {cmd.participant_id} not found")
    if participant.case_id != cmd.case_id:
        raise NotFound(f"participant {cmd.participant_id} not found")
    await authorize(
        Action.CASE_UPDATE,
        cmd.actor,
        Resource(tenant_id=cmd.tenant_id, kind="case", id=participant.case_id),
    )
    if participant.removed_at is not None:
        return participant
    now = clock.now()
    removed = participant.remove(removed_at=now)
    await participant_repo.update(removed)
    await _record(
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor.user_id,
        correlation_id=cmd.correlation_id,
        participant=removed,
        event_type=PARTICIPANT_REMOVED,
        audit=audit,
        outbox=outbox,
        now=now,
        changes={"role": removed.role, "display_name": removed.display_name},
    )
    return removed


async def _authorize_case(
    tenant_id: UUID, case_id: UUID, actor: UserContext, case_repo: CaseRepository
) -> Case:
    """Require update permission and tenant-scoped case existence."""
    await authorize(
        Action.CASE_UPDATE, actor, Resource(tenant_id=tenant_id, kind="case", id=case_id)
    )
    case = await case_repo.get(tenant_id, case_id)
    if case is None:
        raise NotFound(f"case {case_id} not found")
    return case


async def _record(
    *,
    tenant_id: UUID,
    actor_id: UUID,
    correlation_id: str,
    participant: Participant,
    event_type: str,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    now: datetime,
    changes: dict[str, str],
) -> None:
    """Stage audit and outbox records in the caller's transaction."""
    payload = {
        "participant_id": str(participant.id),
        "case_id": str(participant.case_id),
        **changes,
    }
    await audit.append(
        tenant_id=tenant_id,
        event_type=event_type,
        actor_id=actor_id,
        actor_type="user",
        subject_type="participant",
        subject_id=participant.id,
        case_id=participant.case_id,
        correlation_id=correlation_id,
        causation_id=None,
        payload=payload,
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=tenant_id, event_type=event_type, event_id=uuid4(), payload=payload
    )


__all__ = [
    "AddParticipantCommand",
    "UpdateParticipantCommand",
    "RemoveParticipantCommand",
    "add_participant",
    "update_participant",
    "remove_participant",
]
