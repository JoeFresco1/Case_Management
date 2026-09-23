"""Tests for the FM-040 retention + disposition use cases (P08-US3, US4).

Acceptance scenarios (FM-040 / pillar P08 §6):

- A retention policy v1 is published; cases past their retention
  horizon are surfaced as eligible on the list endpoint.
- An approver approves a disposition; the case is marked for
  destruction and the audit row records the policy version, the
  reviewer, and the timestamp.
- A reviewer rejects a disposition with a reason; the case is not
  deleted and the rejection is audited with the rationale.
- A case under active legal hold is never eligible, regardless of
  the retention horizon.

These tests cover the application-layer contract end-to-end through
the use cases (publish, list eligible, route, approve, reject) plus
the eligibility guard. The domain refusal rules are covered by
``tests/domain/test_retention.py`` (the test for the aggregate's
``is_eligible_for_disposition`` helper).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.application.holds.manage_holds import (
    PlaceHoldCommand,
    place_hold,
)
from app.application.retention.disposition import (
    DispositionDecisionCommand,
    PublishRetentionPolicyCommand,
    RouteDispositionCommand,
    approve_disposition,
    list_eligible_for_disposition,
    publish_retention_policy,
    reject_disposition,
    route_disposition_for_case,
)
from app.domain.case import Case, CaseReference
from app.domain.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.domain.retention import (
    RetentionCategory,
    RetentionPolicy,
    RetentionPolicyStatus,
    is_eligible_for_disposition,
)
from app.infrastructure.persistence.legal_hold_repository import (
    InMemoryLegalHoldRepository,
)
from app.infrastructure.persistence.retention_repository import (
    InMemoryDispositionRequestRepository,
    InMemoryRetentionPolicyRepository,
)
from tests.application.cases._fakes import (
    InMemoryAuditStore,
    InMemoryCaseRepository,
    make_case_type,
)

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
SECURITY_ADMIN = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
INTAKE = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

CREATED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
DUE_AT = CREATED_AT + timedelta(days=365)


# ---------------------------------------------------------------------------
# Fakes + fixtures
# ---------------------------------------------------------------------------


@dataclass
class _MutableClock:
    """A clock the test can advance to simulate elapsed time."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance_to(self, now: datetime) -> None:
        self._now = now


@pytest.fixture
def clock() -> _MutableClock:
    """Clock pinned to the post-retention horizon so seeded cases are eligible."""
    return _MutableClock(DUE_AT + timedelta(days=1))


@pytest.fixture
def seeded(
    clock: _MutableClock,
) -> tuple[
    InMemoryCaseRepository,
    InMemoryRetentionPolicyRepository,
    InMemoryDispositionRequestRepository,
    InMemoryLegalHoldRepository,
    InMemoryAuditStore,
    Case,
]:
    """Seed an ACME tenant with one open case past its retention horizon."""
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case_repo.set_tenant_short(OTHER_TENANT, "OTHER")

    _, version = make_case_type(tenant_id=ACME_TENANT, required_keys=("title", "priority"))
    # Backdate the case so it sits past the default horizon
    # (``created_at = 2026-01-01``; the default fixture creates the
    # case at ``now`` so we re-build it explicitly below).
    case = Case.create(
        tenant_id=ACME_TENANT,
        reference=CaseReference("C-ACME-2026-000001"),
        case_type_version=version,
        title="Subject under review",
        description="",
        data={"title": "Subject under review", "priority": "normal"},
        created_by=INTAKE,
        now=CREATED_AT,
    )
    case_repo._cases.append(case)  # noqa: SLF001

    return (
        case_repo,
        InMemoryRetentionPolicyRepository(),
        InMemoryDispositionRequestRepository(),
        InMemoryLegalHoldRepository(),
        InMemoryAuditStore(),
        case,
    )


def _publish_cmd(
    *,
    actor_id: UUID = SECURITY_ADMIN,
    actor_roles: tuple[str, ...] = ("process_admin",),
    tenant_id: UUID = ACME_TENANT,
    period_days: int = 365,
    category: RetentionCategory = RetentionCategory.CASE,
    target_key: str = "",
    correlation_id: str = "corr-publish-1",
) -> PublishRetentionPolicyCommand:
    return PublishRetentionPolicyCommand(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_roles=actor_roles,
        category=category,
        target_key=target_key,
        period_days=period_days,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# 1. Eligibility — pure function
# ---------------------------------------------------------------------------


def test_eligibility__case_past_horizon__returns_eligible() -> None:
    """A case older than the policy's period is eligible (no hold)."""
    policy = RetentionPolicy.draft(
        tenant_id=ACME_TENANT,
        category=RetentionCategory.CASE,
        target_key="",
        period=timedelta(days=365),
        created_at=CREATED_AT,
    )
    published = policy.publish(actor_id=SECURITY_ADMIN, at=CREATED_AT)

    assert (
        is_eligible_for_disposition(
            case_created_at=CREATED_AT,
            policy=published,
            now=DUE_AT + timedelta(seconds=1),
            legal_hold_active=False,
        )
        is True
    )


def test_eligibility__legal_hold_blocks__returns_false() -> None:
    """A case under active legal hold is never eligible."""
    policy = RetentionPolicy.draft(
        tenant_id=ACME_TENANT,
        category=RetentionCategory.CASE,
        target_key="",
        period=timedelta(days=365),
        created_at=CREATED_AT,
    )
    published = policy.publish(actor_id=SECURITY_ADMIN, at=CREATED_AT)

    assert (
        is_eligible_for_disposition(
            case_created_at=CREATED_AT,
            policy=published,
            now=DUE_AT + timedelta(days=1),
            legal_hold_active=True,
        )
        is False
    )


# ---------------------------------------------------------------------------
# 2. Publish retention policy v1
# ---------------------------------------------------------------------------


async def test_publish_retention_policy__v1__persists_policy_and_writes_audit(
    clock: _MutableClock,
) -> None:
    """Publishing v1 of the case-level retention policy persists the
    row in ``published`` status and writes a
    ``RetentionPolicyPublished`` audit row in the same logical
    transaction.
    """
    policy_repo = InMemoryRetentionPolicyRepository()
    audit = InMemoryAuditStore()

    cmd = _publish_cmd(period_days=365)
    result = await publish_retention_policy(cmd, policy_repo=policy_repo, audit=audit, clock=clock)

    assert result.policy.status is RetentionPolicyStatus.PUBLISHED
    assert result.policy.version_number == 1
    assert result.policy.period == timedelta(days=365)
    assert result.policy.published_by == SECURITY_ADMIN

    stored = await policy_repo.get(ACME_TENANT, result.policy.id)
    assert stored is not None
    assert stored.id == result.policy.id

    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.event_type == "RetentionPolicyPublished"
    assert event.tenant_id == ACME_TENANT
    assert event.subject_id == result.policy.id
    assert event.payload["category"] == "case"
    assert event.payload["period_days"] == 365
    assert event.payload["version_number"] == 1


# ---------------------------------------------------------------------------
# 3. List eligible — case eligible after due date routed to approval queue
# ---------------------------------------------------------------------------


async def test_eligible_list__case_past_due_date__appears_in_eligible_items(
    clock: _MutableClock,
    seeded: tuple[
        InMemoryCaseRepository,
        InMemoryRetentionPolicyRepository,
        InMemoryDispositionRequestRepository,
        InMemoryLegalHoldRepository,
        InMemoryAuditStore,
        Case,
    ],
) -> None:
    """A case whose age exceeds the active retention policy's period
    appears in the eligible-list result. The records admin can then
    route it into the approval queue.
    """
    case_repo, policy_repo, _, hold_repo, _, case = seeded
    publish = await publish_retention_policy(
        _publish_cmd(period_days=365),
        policy_repo=policy_repo,
        audit=InMemoryAuditStore(),
        clock=clock,
    )

    result = await list_eligible_for_disposition(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_repo=case_repo,
        hold_repo=hold_repo,
        policy_repo=policy_repo,
        disposition_repo=InMemoryDispositionRequestRepository(),
        clock=clock,
    )

    assert len(result.items) == 1
    entry = result.items[0]
    assert entry.case_id == case.id
    assert entry.case_reference == "C-ACME-2026-000001"
    assert entry.policy_id == publish.policy.id
    assert result.already_pending == []


# ---------------------------------------------------------------------------
# 4. Route + Approve disposition
# ---------------------------------------------------------------------------


async def test_route_and_approve__approver_authorizes_disposition(
    clock: _MutableClock,
    seeded: tuple[
        InMemoryCaseRepository,
        InMemoryRetentionPolicyRepository,
        InMemoryDispositionRequestRepository,
        InMemoryLegalHoldRepository,
        InMemoryAuditStore,
        Case,
    ],
) -> None:
    """A records admin routes an eligible case into the queue; an
    approver authorizes the disposition; the request lands in
    ``approved`` status and the audit row records the policy version
    and the reviewer.
    """
    (
        case_repo,
        policy_repo,
        disposition_repo,
        hold_repo,
        audit,
        case,
    ) = seeded
    await publish_retention_policy(
        _publish_cmd(period_days=365),
        policy_repo=policy_repo,
        audit=InMemoryAuditStore(),
        clock=clock,
    )

    route_result = await route_disposition_for_case(
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
    assert route_result.request.status.value == "pending"
    assert route_result.request.case_id == case.id

    approve_result = await approve_disposition(
        DispositionDecisionCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            request_id=route_result.request.id,
            reason="End of retention — proceed",
        ),
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )
    assert approve_result.request.status.value == "approved"
    assert approve_result.request.reviewed_by == SECURITY_ADMIN
    assert approve_result.request.review_reason == "End of retention — proceed"

    event_types = [e.event_type for e in audit.events]
    assert "DispositionRequested" in event_types
    assert "DispositionApproved" in event_types


# ---------------------------------------------------------------------------
# 5. Reject disposition — case is not deleted
# ---------------------------------------------------------------------------


async def test_reject_disposition__case_retained_and_reason_audited(
    clock: _MutableClock,
    seeded: tuple[
        InMemoryCaseRepository,
        InMemoryRetentionPolicyRepository,
        InMemoryDispositionRequestRepository,
        InMemoryLegalHoldRepository,
        InMemoryAuditStore,
        Case,
    ],
) -> None:
    """An approver rejects a disposition with a reason; the case is
    not deleted and the rejection carries the rationale on the
    audit row + the disposition request.
    """
    (
        case_repo,
        policy_repo,
        disposition_repo,
        hold_repo,
        audit,
        case,
    ) = seeded
    await publish_retention_policy(
        _publish_cmd(period_days=365),
        policy_repo=policy_repo,
        audit=InMemoryAuditStore(),
        clock=clock,
    )
    route_result = await route_disposition_for_case(
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

    reject_result = await reject_disposition(
        DispositionDecisionCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            request_id=route_result.request.id,
            reason="Records still needed for active litigation",
        ),
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )
    assert reject_result.request.status.value == "rejected"
    assert reject_result.request.review_reason == "Records still needed for active litigation"

    # The case is not deleted: the case repo still holds the row
    # untouched.
    still_there = await case_repo.get(ACME_TENANT, case.id)
    assert still_there is not None
    assert still_there.id == case.id

    event_types = [e.event_type for e in audit.events]
    assert "DispositionRejected" in event_types
    rejection_event = next(e for e in audit.events if e.event_type == "DispositionRejected")
    assert rejection_event.payload["review_reason"] == "Records still needed for active litigation"


async def test_reject_disposition__missing_reason__raises_validation(
    clock: _MutableClock,
    seeded: tuple[
        InMemoryCaseRepository,
        InMemoryRetentionPolicyRepository,
        InMemoryDispositionRequestRepository,
        InMemoryLegalHoldRepository,
        InMemoryAuditStore,
        Case,
    ],
) -> None:
    """A rejection without a reason raises :class:`ValidationFailed`."""
    (
        case_repo,
        policy_repo,
        disposition_repo,
        hold_repo,
        audit,
        case,
    ) = seeded
    await publish_retention_policy(
        _publish_cmd(period_days=365),
        policy_repo=policy_repo,
        audit=InMemoryAuditStore(),
        clock=clock,
    )
    route_result = await route_disposition_for_case(
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

    with pytest.raises(ValidationFailed):
        await reject_disposition(
            DispositionDecisionCommand(
                tenant_id=ACME_TENANT,
                actor_id=SECURITY_ADMIN,
                actor_roles=("process_admin",),
                request_id=route_result.request.id,
                reason="   ",
            ),
            disposition_repo=disposition_repo,
            audit=audit,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 6. Legal hold blocks disposition
# ---------------------------------------------------------------------------


async def test_legal_hold_blocks_disposition__held_case_never_eligible(
    clock: _MutableClock,
    seeded: tuple[
        InMemoryCaseRepository,
        InMemoryRetentionPolicyRepository,
        InMemoryDispositionRequestRepository,
        InMemoryLegalHoldRepository,
        InMemoryAuditStore,
        Case,
    ],
) -> None:
    """A case under active legal hold does not appear on the eligible
    list, and an explicit attempt to route it for disposition is
    refused with a 409-equivalent error.
    """
    (
        case_repo,
        policy_repo,
        disposition_repo,
        hold_repo,
        audit,
        case,
    ) = seeded
    await publish_retention_policy(
        _publish_cmd(period_days=365),
        policy_repo=policy_repo,
        audit=InMemoryAuditStore(),
        clock=clock,
    )

    # Place the hold BEFORE the eligibility scan.
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

    eligible = await list_eligible_for_disposition(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_repo=case_repo,
        hold_repo=hold_repo,
        policy_repo=policy_repo,
        disposition_repo=disposition_repo,
        clock=clock,
    )
    assert eligible.items == []

    with pytest.raises(Conflict):
        await route_disposition_for_case(
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
            audit=InMemoryAuditStore(),
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 7. Auth / not-found refusals
# ---------------------------------------------------------------------------


async def test_publish_retention_policy__non_admin_actor__raises_forbidden(
    clock: _MutableClock,
) -> None:
    """A reviewer without ``retention.policy_publish`` is refused."""
    policy_repo = InMemoryRetentionPolicyRepository()
    audit = InMemoryAuditStore()
    cmd = _publish_cmd(actor_id=uuid4(), actor_roles=("reviewer",))

    with pytest.raises(Forbidden):
        await publish_retention_policy(cmd, policy_repo=policy_repo, audit=audit, clock=clock)

    assert audit.events == []


async def test_approve_disposition__missing_request_id__raises_not_found(
    clock: _MutableClock,
) -> None:
    """An approve against a non-existent request id surfaces as
    :class:`NotFound` — the API never confirms cross-tenant existence.
    """
    disposition_repo = InMemoryDispositionRequestRepository()
    with pytest.raises(NotFound):
        await approve_disposition(
            DispositionDecisionCommand(
                tenant_id=ACME_TENANT,
                actor_id=SECURITY_ADMIN,
                actor_roles=("process_admin",),
                request_id=uuid4(),
            ),
            disposition_repo=disposition_repo,
            audit=InMemoryAuditStore(),
            clock=clock,
        )


__all__ = []
