"""Tests for the participant management use cases (P01-US4 / FR-004).

Acceptance scenarios:

- **Add** — a reviewer adds a participant to an open case; the row,
  a ``ParticipantAdded`` audit event, and an outbox event land in one
  logical transaction.
- **Authorization denial** — an actor without ``case.update`` is
  rejected with ``Forbidden`` and nothing is staged.
- **Update** — editable metadata changes in place with identity
  preserved; a ``ParticipantUpdated`` audit event is recorded.
- **Remove** — removal is a tombstone (``removed_at`` set), never a
  delete: the row stays resolvable and a ``ParticipantRemoved``
  audit event is recorded. Re-removing is a no-op.
- **Tenant isolation** — a cross-tenant participant id surfaces as
  ``NotFound``; a participant bound to another case is rejected.

The domain invariants themselves (blank-field rejection, tombstone
ordering) are covered by ``tests/domain/test_participant.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.participants.manage_participants import (
    AddParticipantCommand,
    RemoveParticipantCommand,
    UpdateParticipantCommand,
    add_participant,
    remove_participant,
    update_participant,
)
from app.domain.case import Case
from app.domain.context import UserContext
from app.domain.errors import Forbidden, NotFound
from app.domain.participant import Participant

from .cases._fakes import (
    InMemoryAuditStore,
    InMemoryCaseRepository,
    InMemoryCaseTypeRepository,
    InMemoryCaseTypeVersionRepository,
    InMemoryOutbox,
    make_case_type,
)

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
REVIEWER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
INTAKE = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


class _FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class InMemoryParticipantRepository:
    """In-memory `ParticipantRepository` for manage_participants tests."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, Participant] = {}

    async def add(self, participant: Participant) -> None:
        self._by_id[participant.id] = participant

    async def get(self, tenant_id: UUID, participant_id: UUID) -> Participant | None:
        participant = self._by_id.get(participant_id)
        if participant is None or participant.tenant_id != tenant_id:
            return None
        return participant

    async def list_for_case(self, tenant_id: UUID, case_id: UUID) -> list[Participant]:
        return [
            p for p in self._by_id.values() if p.tenant_id == tenant_id and p.case_id == case_id
        ]

    async def update(self, participant: Participant) -> None:
        self._by_id[participant.id] = participant


def _actor(user_id: UUID, roles: tuple[str, ...]) -> UserContext:
    return UserContext(
        user_id=user_id,
        tenant_id=ACME_TENANT,
        display_name="Test Actor",
        roles=roles,
    )


@pytest.fixture
def world() -> dict[str, Any]:
    """Seed an ACME tenant with one open case and empty participant stores."""
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case_repo.set_tenant_short(OTHER_TENANT, "OTHER")

    case_type_repo = InMemoryCaseTypeRepository()
    version_repo = InMemoryCaseTypeVersionRepository()
    case_type, version = make_case_type(
        tenant_id=ACME_TENANT,
        required_keys=("title",),
    )
    case_type_repo.add(case_type)
    version_repo.add(version)

    from app.domain.case import CaseReference  # noqa: PLC0415

    case = Case.create(
        tenant_id=ACME_TENANT,
        reference=CaseReference("C-ACME-2026-000001"),
        case_type_version=version,
        title="Subject under review",
        description="",
        data={"title": "Subject under review"},
        created_by=INTAKE,
        now=FIXED_NOW,
    )
    case_repo._cases.append(case)  # noqa: SLF001 - test fixture

    return {
        "case_repo": case_repo,
        "participant_repo": InMemoryParticipantRepository(),
        "audit": InMemoryAuditStore(),
        "outbox": InMemoryOutbox(),
        "clock": _FixedClock(FIXED_NOW),
        "case": case,
    }


async def _add(
    world: dict[str, Any],
    *,
    role: str = "requester",
    display_name: str = "Alex Example",
) -> Participant:
    return await add_participant(
        AddParticipantCommand(
            tenant_id=ACME_TENANT,
            case_id=world["case"].id,
            actor=_actor(REVIEWER, ("reviewer",)),
            role=role,
            display_name=display_name,
            source="synthetic",
            correlation_id="corr-add-1",
        ),
        case_repo=world["case_repo"],
        participant_repo=world["participant_repo"],
        audit=world["audit"],
        outbox=world["outbox"],
        clock=world["clock"],
    )


async def test_add_participant__persists_row_audit_and_outbox(
    world: dict[str, Any],
) -> None:
    """A reviewer adding a participant stages row + audit + outbox together."""
    participant = await _add(world)

    stored = await world["participant_repo"].get(ACME_TENANT, participant.id)
    assert stored is not None
    assert stored.role == "requester"
    assert stored.display_name == "Alex Example"
    assert stored.removed_at is None

    assert [e.event_type for e in world["audit"].events] == ["ParticipantAdded"]
    assert world["audit"].events[0].case_id == world["case"].id
    assert [e.event_type for e in world["outbox"].events] == ["ParticipantAdded"]


async def test_add_participant__without_update_grant__forbidden(
    world: dict[str, Any],
) -> None:
    """An intake actor (no ``case.update``) is rejected with nothing staged."""
    with pytest.raises(Forbidden):
        await add_participant(
            AddParticipantCommand(
                tenant_id=ACME_TENANT,
                case_id=world["case"].id,
                actor=_actor(INTAKE, ("intake",)),
                role="requester",
                display_name="Alex Example",
                source="synthetic",
            ),
            case_repo=world["case_repo"],
            participant_repo=world["participant_repo"],
            audit=world["audit"],
            outbox=world["outbox"],
            clock=world["clock"],
        )

    assert world["audit"].events == []
    assert world["outbox"].events == []


async def test_add_participant__missing_case__not_found(
    world: dict[str, Any],
) -> None:
    """Adding to a nonexistent case surfaces ``NotFound``."""
    with pytest.raises(NotFound):
        await add_participant(
            AddParticipantCommand(
                tenant_id=ACME_TENANT,
                case_id=uuid4(),
                actor=_actor(REVIEWER, ("reviewer",)),
                role="requester",
                display_name="Alex Example",
                source="synthetic",
            ),
            case_repo=world["case_repo"],
            participant_repo=world["participant_repo"],
            audit=world["audit"],
            outbox=world["outbox"],
            clock=world["clock"],
        )


async def test_update_participant__changes_metadata_preserves_identity(
    world: dict[str, Any],
) -> None:
    """Editable metadata changes in place; identity is preserved."""
    participant = await _add(world)

    updated = await update_participant(
        UpdateParticipantCommand(
            tenant_id=ACME_TENANT,
            participant_id=participant.id,
            case_id=world["case"].id,
            actor=_actor(REVIEWER, ("reviewer",)),
            role="respondent",
            display_name="Alex Updated",
            correlation_id="corr-update-1",
        ),
        participant_repo=world["participant_repo"],
        audit=world["audit"],
        outbox=world["outbox"],
        clock=world["clock"],
    )

    assert updated.id == participant.id
    assert updated.role == "respondent"
    assert updated.display_name == "Alex Updated"
    assert updated.source == "synthetic"
    assert [e.event_type for e in world["audit"].events][-1:] == ["ParticipantUpdated"]


async def test_update_participant__missing_or_other_case__not_found(
    world: dict[str, Any],
) -> None:
    """Unknown ids — or ids bound to another case — surface ``NotFound``."""
    with pytest.raises(NotFound):
        await update_participant(
            UpdateParticipantCommand(
                tenant_id=ACME_TENANT,
                participant_id=uuid4(),
                case_id=world["case"].id,
                actor=_actor(REVIEWER, ("reviewer",)),
                role="respondent",
            ),
            participant_repo=world["participant_repo"],
            audit=world["audit"],
            outbox=world["outbox"],
            clock=world["clock"],
        )

    participant = await _add(world)
    with pytest.raises(NotFound):
        await update_participant(
            UpdateParticipantCommand(
                tenant_id=ACME_TENANT,
                participant_id=participant.id,
                case_id=uuid4(),
                actor=_actor(REVIEWER, ("reviewer",)),
                role="respondent",
            ),
            participant_repo=world["participant_repo"],
            audit=world["audit"],
            outbox=world["outbox"],
            clock=world["clock"],
        )


async def test_remove_participant__tombstones_and_preserves_history(
    world: dict[str, Any],
) -> None:
    """Removal sets ``removed_at``; the row stays resolvable for history."""
    participant = await _add(world)

    removed = await remove_participant(
        RemoveParticipantCommand(
            tenant_id=ACME_TENANT,
            participant_id=participant.id,
            case_id=world["case"].id,
            actor=_actor(REVIEWER, ("reviewer",)),
            correlation_id="corr-remove-1",
        ),
        participant_repo=world["participant_repo"],
        audit=world["audit"],
        outbox=world["outbox"],
        clock=world["clock"],
    )

    assert removed.id == participant.id
    assert removed.removed_at == FIXED_NOW
    # Historical reference still resolves after removal.
    stored = await world["participant_repo"].get(ACME_TENANT, participant.id)
    assert stored is not None
    assert stored.display_name == "Alex Example"
    assert stored.removed_at == FIXED_NOW
    assert [e.event_type for e in world["audit"].events][-1:] == ["ParticipantRemoved"]


async def test_remove_participant__already_removed__no_duplicate_audit(
    world: dict[str, Any],
) -> None:
    """Re-removing a tombstoned participant is a no-op without new rows."""
    participant = await _add(world)
    cmd = RemoveParticipantCommand(
        tenant_id=ACME_TENANT,
        participant_id=participant.id,
        case_id=world["case"].id,
        actor=_actor(REVIEWER, ("reviewer",)),
    )
    kwargs = {
        "participant_repo": world["participant_repo"],
        "audit": world["audit"],
        "outbox": world["outbox"],
        "clock": world["clock"],
    }
    await remove_participant(cmd, **kwargs)  # type: ignore[arg-type]
    audit_count = len(world["audit"].events)

    again = await remove_participant(cmd, **kwargs)  # type: ignore[arg-type]

    assert again.removed_at == FIXED_NOW
    assert len(world["audit"].events) == audit_count


async def test_participant__cross_tenant_id__not_found(
    world: dict[str, Any],
) -> None:
    """A participant id from another tenant never leaks across tenants."""
    participant = await _add(world)

    assert await world["participant_repo"].get(OTHER_TENANT, participant.id) is None
    with pytest.raises(NotFound):
        await remove_participant(
            RemoveParticipantCommand(
                tenant_id=OTHER_TENANT,
                participant_id=participant.id,
                case_id=world["case"].id,
                actor=_actor(REVIEWER, ("reviewer",)),
            ),
            participant_repo=world["participant_repo"],
            audit=world["audit"],
            outbox=world["outbox"],
            clock=world["clock"],
        )
