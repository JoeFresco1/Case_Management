"""Retention-clock pause semantics + approval-gate acceptance (002 T6 / FM-040, FM-041).

Gate decision under test (002-leftovers spec §9 Q5): **pause and resume**.
While a case is under active legal hold the disposition clock is frozen;
on release the clock resumes — the horizon shifts forward by the total
hold duration (never resets to a full new period, never counts hold time
toward eligibility).

Acceptance groups (task F acceptance):

- policy immutability: published rows are frozen; republishing a retired
  policy raises; retiring a draft raises; a second publish bumps the
  per-target version without mutating v1.
- gate-selected clock semantics: frozen while held, shifted due after
  release, legacy horizon with no holds.
- required approval: only an ``approved`` request may execute.
- stale-token: a second decision on a decided request raises
  ``Conflict`` (no silent overwrite) at use-case and repository level.
- denial: non-admin actors are refused before any write (no audit row).
- replay: executing twice with the same idempotency key appends one
  audit row and returns the stored outcome.
- rollback: gate refusals (hold / freeze / archival) leave the request
  ``pending`` with zero new audit rows — the transaction commits
  nothing (SQL atomicity in the router; zero-write refusal here).

Style: ``test_<unit>_<scenario>__<expectation>`` with
given/when/then; ``asyncio_mode = auto`` so no markers are needed.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.application.holds.manage_holds import (
    PlaceHoldCommand,
    ReleaseHoldCommand,
    place_hold,
    release_hold,
)
from app.application.retention.archival import (
    ArchiveCaseCommand,
    CancelDisposalFreezeCommand,
    DeclareDisposalFreezeCommand,
    archive_case,
    cancel_disposal_freeze,
    declare_disposal_freeze,
)
from app.application.retention.disposition import (
    CASE_FROZEN_CODE,
    DISPOSAL_FROZEN_CODE,
    DispositionDecisionCommand,
    ExecuteDispositionCommand,
    PublishRetentionPolicyCommand,
    RouteDispositionCommand,
    approve_disposition,
    execute_disposition,
    list_eligible_for_disposition,
    publish_retention_policy,
    reject_disposition,
    route_disposition_for_case,
)
from app.domain.case import Case, CaseReference
from app.domain.errors import Conflict, Forbidden, InvariantViolation, ValidationFailed
from app.domain.legal_hold import REFUSED_HOLD_CODE
from app.domain.retention import (
    ArchivalTier,
    HoldPeriod,
    RetentionCategory,
    RetentionPolicy,
    RetentionPolicyStatus,
    effective_due_at,
    is_eligible_with_hold_pause,
    total_hold_pause,
)
from app.infrastructure.persistence.legal_hold_repository import (
    InMemoryLegalHoldRepository,
)
from app.infrastructure.persistence.retention_repository import (
    InMemoryCaseArchivalRepository,
    InMemoryDisposalFreezeRepository,
    InMemoryDispositionRequestRepository,
    InMemoryRetentionPolicyRepository,
)
from tests.application.cases._fakes import (
    InMemoryAuditStore,
    InMemoryCaseRepository,
    InMemoryIdempotencyStore,
    make_case_type,
)

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
SECURITY_ADMIN = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
INTAKE = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

CREATED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
PERIOD_DAYS = 365
HORIZON = CREATED_AT + timedelta(days=PERIOD_DAYS)


@dataclass
class _MutableClock:
    """A clock the test can advance to simulate elapsed time."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance_to(self, now: datetime) -> None:
        self._now = now


async def _seed_case(
    case_repo: InMemoryCaseRepository,
    *,
    created_at: datetime = CREATED_AT,
    reference: str = "C-ACME-2026-000001",
) -> Case:
    """Seed one ACME case with a fixed creation timestamp."""
    # given: tenant short-code + case type version for issuance
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    _, version = make_case_type(tenant_id=ACME_TENANT, required_keys=("title", "priority"))
    case = Case.create(
        tenant_id=ACME_TENANT,
        reference=CaseReference(reference),
        case_type_version=version,
        title="Retention candidate",
        description="",
        data={"title": "Retention candidate", "priority": "normal"},
        created_by=INTAKE,
        now=created_at,
    )
    await case_repo.add(case)
    return case


async def _publish(
    policy_repo: InMemoryRetentionPolicyRepository,
    clock: _MutableClock,
    *,
    period_days: int = PERIOD_DAYS,
) -> RetentionPolicy:
    """Publish the case-level policy; return the persisted row."""
    result = await publish_retention_policy(
        PublishRetentionPolicyCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            category=RetentionCategory.CASE,
            target_key="",
            period_days=period_days,
        ),
        policy_repo=policy_repo,
        audit=InMemoryAuditStore(),
        clock=clock,
    )
    return result.policy


async def _route(
    case_repo: InMemoryCaseRepository,
    hold_repo: InMemoryLegalHoldRepository,
    policy_repo: InMemoryRetentionPolicyRepository,
    disposition_repo: InMemoryDispositionRequestRepository,
    audit: InMemoryAuditStore,
    clock: _MutableClock,
    case: Case,
) -> UUID:
    """Route a case; return the pending request id."""
    result = await route_disposition_for_case(
        RouteDispositionCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            case_id=case.id,
        ),
        case_repo=case_repo,
        hold_repo=hold_repo,
        policy_repo=policy_repo,
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )
    return result.request.id


# ---------------------------------------------------------------------------
# Policy immutability
# ---------------------------------------------------------------------------


async def test_policy_publish__second_version__supersedes_without_mutating_v1() -> None:
    """A second publish bumps the per-target version; v1 is untouched."""
    # given: published v1
    clock = _MutableClock(HORIZON)
    policy_repo = InMemoryRetentionPolicyRepository()
    v1 = await _publish(policy_repo, clock)

    # when: a second policy is published for the same target
    clock.advance_to(HORIZON + timedelta(days=1))
    v2 = await _publish(policy_repo, clock)

    # then: monotonic bump, v1 row unchanged in the store
    assert v1.version_number == 1
    assert v2.version_number == 2
    assert v2.id != v1.id
    stored_v1 = await policy_repo.get(ACME_TENANT, v1.id)
    assert stored_v1 is not None
    assert stored_v1.version_number == 1
    assert stored_v1.status is RetentionPolicyStatus.PUBLISHED


async def test_policy_publish__retired__raises_invariant() -> None:
    """Republishing a retired policy raises; retirement is one-way."""
    # given: a retired policy
    clock = _MutableClock(HORIZON)
    policy = (await _publish(InMemoryRetentionPolicyRepository(), clock)).retire()

    # when / then: publish refuses
    with pytest.raises(InvariantViolation):
        policy.publish(actor_id=SECURITY_ADMIN, at=HORIZON)


async def test_policy_retire__draft__raises_invariant() -> None:
    """Retiring an unpublished draft raises; drafts are discarded, not retired."""
    # given: a fresh draft
    draft = RetentionPolicy.draft(
        tenant_id=ACME_TENANT,
        category=RetentionCategory.CASE,
        target_key="",
        period=timedelta(days=PERIOD_DAYS),
        created_at=CREATED_AT,
    )

    # when / then
    with pytest.raises(InvariantViolation):
        draft.retire()


async def test_policy_row__published_is_frozen__raises_on_mutation() -> None:
    """The aggregate is frozen: attribute writes raise (no silent edit)."""
    # given: a published policy
    clock = _MutableClock(HORIZON)
    policy = await _publish(InMemoryRetentionPolicyRepository(), clock)

    # when / then: in-place mutation is impossible
    with pytest.raises(FrozenInstanceError):
        policy.version_number = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Gate-selected clock semantics (pause and resume)
# ---------------------------------------------------------------------------


async def test_clock_pause__no_holds__matches_legacy_horizon() -> None:
    """With no holds the adjusted due equals ``created_at + period``."""
    # given: published policy, no holds
    clock = _MutableClock(HORIZON)
    policy = await _publish(InMemoryRetentionPolicyRepository(), clock)

    # when / then
    assert (
        effective_due_at(case_created_at=CREATED_AT, policy=policy, periods=[], now=HORIZON)
        == HORIZON
    )
    assert total_hold_pause(periods=[], now=HORIZON) == timedelta(0)


async def test_clock_pause__held_case__never_eligible_while_held() -> None:
    """An active hold blocks eligibility even far past the horizon."""
    # given: policy + a case twice past its horizon
    clock = _MutableClock(HORIZON)
    policy = await _publish(InMemoryRetentionPolicyRepository(), clock)
    far_future = HORIZON + timedelta(days=730)

    # when / then: active hold wins over age
    assert (
        is_eligible_with_hold_pause(
            case_created_at=CREATED_AT,
            policy=policy,
            now=far_future,
            legal_hold_active=True,
            periods=[HoldPeriod(started_at=HORIZON)],
        )
        is False
    )


async def test_clock_pause__released_hold__shifts_due_by_hold_duration() -> None:
    """A 30-day released hold shifts the horizon 30 days forward (resume)."""
    # given: policy + a 30-day hold fully released before the legacy horizon
    clock = _MutableClock(HORIZON)
    policy = await _publish(InMemoryRetentionPolicyRepository(), clock)
    hold_start = HORIZON - timedelta(days=60)
    hold_end = HORIZON - timedelta(days=30)
    periods = [HoldPeriod(started_at=hold_start, ended_at=hold_end)]

    # when: due is recomputed after release
    shifted_due = effective_due_at(
        case_created_at=CREATED_AT, policy=policy, periods=periods, now=hold_end
    )

    # then: horizon shifted by exactly the hold duration (pause, not reset)
    assert shifted_due == HORIZON + timedelta(days=30)
    assert total_hold_pause(periods=periods, now=hold_end) == timedelta(days=30)
    # not yet eligible one day before the shifted due ...
    assert (
        is_eligible_with_hold_pause(
            case_created_at=CREATED_AT,
            policy=policy,
            now=shifted_due - timedelta(days=1),
            legal_hold_active=False,
            periods=periods,
        )
        is False
    )
    # ... eligible exactly at the shifted due
    assert (
        is_eligible_with_hold_pause(
            case_created_at=CREATED_AT,
            policy=policy,
            now=shifted_due,
            legal_hold_active=False,
            periods=periods,
        )
        is True
    )


async def test_clock_pause__route_after_release__uses_shifted_due() -> None:
    """End-to-end: place+release a hold, route only after the shifted due."""
    # given: policy + case + 30-day hold fully released before the horizon
    clock = _MutableClock(HORIZON - timedelta(days=61))
    case_repo = InMemoryCaseRepository()
    case = await _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    audit = InMemoryAuditStore()
    await _publish(policy_repo, clock)
    clock.advance_to(HORIZON - timedelta(days=60))
    await place_hold(
        PlaceHoldCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            case_id=case.id,
            reason="Litigation preservation",
        ),
        case_repo=case_repo,
        hold_repo=hold_repo,
        audit=audit,
        clock=clock,
    )
    clock.advance_to(HORIZON - timedelta(days=30))
    await release_hold(
        ReleaseHoldCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            case_id=case.id,
            reason="Matter closed",
        ),
        case_repo=case_repo,
        hold_repo=hold_repo,
        audit=audit,
        clock=clock,
    )

    # when: history-aware listing runs one day before the shifted due
    clock.advance_to(HORIZON + timedelta(days=29))
    early = await list_eligible_for_disposition(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_repo=case_repo,
        hold_repo=hold_repo,
        policy_repo=policy_repo,
        disposition_repo=disposition_repo,
        clock=clock,
    )

    # then: not eligible yet (clock paused 30 days, not reset, not ignored)
    assert early.items == []

    # when: listing runs at the shifted due
    clock.advance_to(HORIZON + timedelta(days=30))
    late = await list_eligible_for_disposition(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_repo=case_repo,
        hold_repo=hold_repo,
        policy_repo=policy_repo,
        disposition_repo=disposition_repo,
        clock=clock,
    )

    # then: eligible with the shifted due cited
    assert [e.case_id for e in late.items] == [case.id]
    assert late.items[0].due_at == HORIZON + timedelta(days=30)


# ---------------------------------------------------------------------------
# Required approval
# ---------------------------------------------------------------------------


async def test_execute__pending_request__conflict_without_audit() -> None:
    """Executing before approval raises; nothing is written."""
    # given: a routed-but-unapproved request
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = await _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    audit = InMemoryAuditStore()
    await _publish(policy_repo, clock)
    request_id = await _route(
        case_repo, hold_repo, policy_repo, disposition_repo, audit, clock, case
    )
    before = len(audit.events)

    # when / then
    with pytest.raises(Conflict):
        await execute_disposition(
            ExecuteDispositionCommand(
                tenant_id=ACME_TENANT,
                actor_id=SECURITY_ADMIN,
                actor_roles=("process_admin",),
                request_id=request_id,
            ),
            disposition_repo=disposition_repo,
            policy_repo=policy_repo,
            case_repo=case_repo,
            hold_repo=hold_repo,
            audit=audit,
            clock=clock,
        )
    assert len(audit.events) == before


async def test_execute__rejected_request__conflict() -> None:
    """A rejected request is explicitly retained: execution raises."""
    # given: a rejected request
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = await _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    audit = InMemoryAuditStore()
    await _publish(policy_repo, clock)
    request_id = await _route(
        case_repo, hold_repo, policy_repo, disposition_repo, audit, clock, case
    )
    await reject_disposition(
        DispositionDecisionCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            request_id=request_id,
            reason="Still under review",
        ),
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )

    # when / then
    with pytest.raises(Conflict):
        await execute_disposition(
            ExecuteDispositionCommand(
                tenant_id=ACME_TENANT,
                actor_id=SECURITY_ADMIN,
                actor_roles=("process_admin",),
                request_id=request_id,
            ),
            disposition_repo=disposition_repo,
            policy_repo=policy_repo,
            case_repo=case_repo,
            hold_repo=hold_repo,
            audit=audit,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# Stale-token (concurrent-decision guard)
# ---------------------------------------------------------------------------


async def test_approve__second_approve__conflict_with_single_audit() -> None:
    """A stale second approval raises; exactly one approval audit exists."""
    # given: an approved request
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = await _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    audit = InMemoryAuditStore()
    await _publish(policy_repo, clock)
    request_id = await _route(
        case_repo, hold_repo, policy_repo, disposition_repo, audit, clock, case
    )
    cmd = DispositionDecisionCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        request_id=request_id,
        reason="End of retention",
    )
    await approve_disposition(cmd, disposition_repo=disposition_repo, audit=audit, clock=clock)
    approvals = [e for e in audit.events if e.event_type == "DispositionApproved"]

    # when / then: stale retry conflicts, no second audit row
    with pytest.raises(Conflict):
        await approve_disposition(cmd, disposition_repo=disposition_repo, audit=audit, clock=clock)
    assert [e for e in audit.events if e.event_type == "DispositionApproved"] == approvals
    assert len(approvals) == 1


async def test_reject__after_approve__conflict() -> None:
    """Rejecting an approved request raises (terminal states are final)."""
    # given: an approved request
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = await _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    audit = InMemoryAuditStore()
    await _publish(policy_repo, clock)
    request_id = await _route(
        case_repo, hold_repo, policy_repo, disposition_repo, audit, clock, case
    )
    await approve_disposition(
        DispositionDecisionCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            request_id=request_id,
            reason="End of retention",
        ),
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )

    # when / then
    with pytest.raises(Conflict):
        await reject_disposition(
            DispositionDecisionCommand(
                tenant_id=ACME_TENANT,
                actor_id=SECURITY_ADMIN,
                actor_roles=("process_admin",),
                request_id=request_id,
                reason="Second thoughts",
            ),
            disposition_repo=disposition_repo,
            audit=audit,
            clock=clock,
        )


async def test_disposition_update__on_decided_row__conflict() -> None:
    """The repository refuses a stale update (defense-in-depth)."""
    # given: an approved request already persisted
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = await _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    audit = InMemoryAuditStore()
    await _publish(policy_repo, clock)
    request_id = await _route(
        case_repo, hold_repo, policy_repo, disposition_repo, audit, clock, case
    )
    await approve_disposition(
        DispositionDecisionCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            request_id=request_id,
            reason="End of retention",
        ),
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )
    stored = await disposition_repo.get(ACME_TENANT, request_id)
    assert stored is not None

    # when / then: direct stale write conflicts
    with pytest.raises(Conflict):
        await disposition_repo.update(stored)


# ---------------------------------------------------------------------------
# Denial (fail-closed authorization)
# ---------------------------------------------------------------------------


async def test_publish__non_admin__forbidden_without_write() -> None:
    """A worker cannot publish: no policy row, no audit row."""
    # given
    clock = _MutableClock(HORIZON)
    policy_repo = InMemoryRetentionPolicyRepository()
    audit = InMemoryAuditStore()

    # when / then
    with pytest.raises(Forbidden):
        await publish_retention_policy(
            PublishRetentionPolicyCommand(
                tenant_id=ACME_TENANT,
                actor_id=uuid4(),
                actor_roles=("reviewer",),
                category=RetentionCategory.CASE,
                target_key="",
                period_days=PERIOD_DAYS,
            ),
            policy_repo=policy_repo,
            audit=audit,
            clock=clock,
        )
    assert await policy_repo.list_published(ACME_TENANT) == []
    assert audit.events == []


async def test_approve__non_admin__forbidden_without_mutation() -> None:
    """A worker cannot approve: request stays pending, no audit row."""
    # given: a pending request
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = await _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    audit = InMemoryAuditStore()
    await _publish(policy_repo, clock)
    request_id = await _route(
        case_repo, hold_repo, policy_repo, disposition_repo, audit, clock, case
    )
    before = len(audit.events)

    # when / then
    with pytest.raises(Forbidden):
        await approve_disposition(
            DispositionDecisionCommand(
                tenant_id=ACME_TENANT,
                actor_id=uuid4(),
                actor_roles=("reviewer",),
                request_id=request_id,
                reason="I approve",
            ),
            disposition_repo=disposition_repo,
            audit=audit,
            clock=clock,
        )
    stored = await disposition_repo.get(ACME_TENANT, request_id)
    assert stored is not None
    assert stored.status.value == "pending"
    assert len(audit.events) == before


# ---------------------------------------------------------------------------
# Replay (idempotent execution)
# ---------------------------------------------------------------------------


async def test_execute__replay_same_key__single_audit_row() -> None:
    """Re-executing an approved disposition replays the stored outcome."""
    # given: an approved request
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = await _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    audit = InMemoryAuditStore()
    store = InMemoryIdempotencyStore()
    await _publish(policy_repo, clock)
    request_id = await _route(
        case_repo, hold_repo, policy_repo, disposition_repo, audit, clock, case
    )
    await approve_disposition(
        DispositionDecisionCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            request_id=request_id,
            reason="End of retention",
        ),
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )

    # when: executed twice under one key
    cmd = ExecuteDispositionCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        request_id=request_id,
        idempotency_key="exec-approved-1",
    )
    first = await execute_disposition(
        cmd,
        disposition_repo=disposition_repo,
        policy_repo=policy_repo,
        case_repo=case_repo,
        hold_repo=hold_repo,
        audit=audit,
        clock=clock,
        idempotency_store=store,  # type: ignore[arg-type]
    )
    count_after_first = len(audit.events)
    second = await execute_disposition(
        cmd,
        disposition_repo=disposition_repo,
        policy_repo=policy_repo,
        case_repo=case_repo,
        hold_repo=hold_repo,
        audit=audit,
        clock=clock,
        idempotency_store=store,  # type: ignore[arg-type]
    )

    # then: same outcome, no second audit row, case untouched
    assert first.outcome == second.outcome == "executed"
    assert first.policy_version == second.policy_version == 1
    assert len(audit.events) == count_after_first
    assert (await case_repo.get(ACME_TENANT, case.id)) is not None


# ---------------------------------------------------------------------------
# Rollback (gate refusals commit nothing)
# ---------------------------------------------------------------------------


async def test_approve__held_case__pending_unchanged_no_audit() -> None:
    """Approving under active hold refuses with ``refused_hold``; zero writes."""
    # given: pending request + freshly placed hold
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = await _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    audit = InMemoryAuditStore()
    await _publish(policy_repo, clock)
    request_id = await _route(
        case_repo, hold_repo, policy_repo, disposition_repo, audit, clock, case
    )
    await place_hold(
        PlaceHoldCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            case_id=case.id,
            reason="Litigation preservation",
        ),
        case_repo=case_repo,
        hold_repo=hold_repo,
        audit=audit,
        clock=clock,
    )
    before = len(audit.events)

    # when / then
    with pytest.raises(Conflict, match=REFUSED_HOLD_CODE):
        await approve_disposition(
            DispositionDecisionCommand(
                tenant_id=ACME_TENANT,
                actor_id=SECURITY_ADMIN,
                actor_roles=("process_admin",),
                request_id=request_id,
                reason="End of retention",
            ),
            disposition_repo=disposition_repo,
            audit=audit,
            clock=clock,
            hold_repo=hold_repo,
        )
    stored = await disposition_repo.get(ACME_TENANT, request_id)
    assert stored is not None
    assert stored.status.value == "pending"
    assert len(audit.events) == before


async def test_approve__frozen_window__pending_unchanged_no_audit() -> None:
    """Approving inside a freeze window refuses; zero writes (rollback)."""
    # given: pending request + covering freeze window
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = await _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    freeze_repo = InMemoryDisposalFreezeRepository()
    audit = InMemoryAuditStore()
    await _publish(policy_repo, clock)
    request_id = await _route(
        case_repo, hold_repo, policy_repo, disposition_repo, audit, clock, case
    )
    await declare_disposal_freeze(
        DeclareDisposalFreezeCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            starts_at=HORIZON,
            ends_at=HORIZON + timedelta(days=60),
            reason="Regulatory review window",
        ),
        freeze_repo=freeze_repo,
        audit=audit,
        clock=clock,
    )
    before = len(audit.events)

    # when / then
    with pytest.raises(Conflict, match=DISPOSAL_FROZEN_CODE):
        await approve_disposition(
            DispositionDecisionCommand(
                tenant_id=ACME_TENANT,
                actor_id=SECURITY_ADMIN,
                actor_roles=("process_admin",),
                request_id=request_id,
                reason="End of retention",
            ),
            disposition_repo=disposition_repo,
            audit=audit,
            clock=clock,
            freeze_repo=freeze_repo,
        )
    stored = await disposition_repo.get(ACME_TENANT, request_id)
    assert stored is not None
    assert stored.status.value == "pending"
    assert len(audit.events) == before


async def test_approve__archived_case__pending_unchanged_no_audit() -> None:
    """Approving an archived (frozen) case refuses; zero writes (rollback)."""
    # given: pending request + archived case
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = await _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    archival_repo = InMemoryCaseArchivalRepository()
    audit = InMemoryAuditStore()
    await _publish(policy_repo, clock)
    request_id = await _route(
        case_repo, hold_repo, policy_repo, disposition_repo, audit, clock, case
    )
    await archive_case(
        ArchiveCaseCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            case_id=case.id,
            tier=ArchivalTier.WARM,
            reason="Past its active lifecycle",
        ),
        case_repo=case_repo,
        archival_repo=archival_repo,
        audit=audit,
        clock=clock,
    )
    before = len(audit.events)

    # when / then
    with pytest.raises(Conflict, match=CASE_FROZEN_CODE):
        await approve_disposition(
            DispositionDecisionCommand(
                tenant_id=ACME_TENANT,
                actor_id=SECURITY_ADMIN,
                actor_roles=("process_admin",),
                request_id=request_id,
                reason="End of retention",
            ),
            disposition_repo=disposition_repo,
            audit=audit,
            clock=clock,
            archival_repo=archival_repo,
        )
    stored = await disposition_repo.get(ACME_TENANT, request_id)
    assert stored is not None
    assert stored.status.value == "pending"
    assert len(audit.events) == before


async def test_approve__after_freeze_cancel__succeeds() -> None:
    """Rollback is recoverable: cancelling the freeze unblocks approval."""
    # given: pending request + freeze that is then cancelled
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = await _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    freeze_repo = InMemoryDisposalFreezeRepository()
    audit = InMemoryAuditStore()
    await _publish(policy_repo, clock)
    request_id = await _route(
        case_repo, hold_repo, policy_repo, disposition_repo, audit, clock, case
    )
    declared = await declare_disposal_freeze(
        DeclareDisposalFreezeCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            starts_at=HORIZON,
            ends_at=HORIZON + timedelta(days=60),
            reason="Regulatory review window",
        ),
        freeze_repo=freeze_repo,
        audit=audit,
        clock=clock,
    )
    await cancel_disposal_freeze(
        CancelDisposalFreezeCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            freeze_id=declared.freeze.id,
            reason="Review complete",
        ),
        freeze_repo=freeze_repo,
        audit=audit,
        clock=clock,
    )

    # when
    result = await approve_disposition(
        DispositionDecisionCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            request_id=request_id,
            reason="End of retention",
        ),
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
        freeze_repo=freeze_repo,
    )

    # then: approval lands once the blocker clears
    assert result.request.status.value == "approved"


async def test_reject__missing_reason__validation_without_mutation() -> None:
    """Rejecting without a reason is refused; request stays pending."""
    # given: a pending request
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = await _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    audit = InMemoryAuditStore()
    await _publish(policy_repo, clock)
    request_id = await _route(
        case_repo, hold_repo, policy_repo, disposition_repo, audit, clock, case
    )
    before = len(audit.events)

    # when / then
    with pytest.raises(ValidationFailed):
        await reject_disposition(
            DispositionDecisionCommand(
                tenant_id=ACME_TENANT,
                actor_id=SECURITY_ADMIN,
                actor_roles=("process_admin",),
                request_id=request_id,
                reason="  ",
            ),
            disposition_repo=disposition_repo,
            audit=audit,
            clock=clock,
        )
    stored = await disposition_repo.get(ACME_TENANT, request_id)
    assert stored is not None
    assert stored.status.value == "pending"
    assert len(audit.events) == before


__all__: list[str] = []
