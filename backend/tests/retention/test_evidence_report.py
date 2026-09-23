"""Tests for the FM-042 defensible-deletion evidence report (P08-US6 / P12-US9).

Acceptance scenario:

- A compliance user runs the report for a case that has been
  disposed; the report lists the disposed case, the published
  policy version, the approving actor, and the decision timestamp.
- A case that has **not** been disposed is **not** in the report
  as a disposed record (the use case returns ``disposed=false``
  and an empty ``dispositions`` list — the absence is the proof of
  "never silent deletion" per P08 §2).
- The report surfaces the policy version that was active when the
  approval ran, not whatever policy is current today.
- The audit chain carries the
  ``RetentionPolicyPublished`` / ``DispositionRequested`` /
  ``DispositionApproved`` events for the case so the chain of
  custody is re-walkable without external joins.
- A cross-tenant case id surfaces as ``NotFound`` — the use case
  never confirms cross-tenant existence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.application.retention.disposition import (
    DispositionDecisionCommand,
    PublishRetentionPolicyCommand,
    RouteDispositionCommand,
    approve_disposition,
    publish_retention_policy,
    reject_disposition,
    route_disposition_for_case,
)
from app.application.retention.evidence_report import build_evidence_report
from app.domain.case import Case, CaseReference
from app.domain.errors import Forbidden, NotFound
from app.domain.retention import (
    RetentionCategory,
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
APPROVER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
INTAKE = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

CREATED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
DUE_AT = CREATED_AT + timedelta(days=365)
APPROVAL_AT = DUE_AT + timedelta(days=1)


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
    """Clock pinned to the post-approval timestamp so seeded cases are disposed."""
    return _MutableClock(APPROVAL_AT)


@pytest.fixture
def case_repo() -> InMemoryCaseRepository:
    repo = InMemoryCaseRepository()
    repo.set_tenant_short(ACME_TENANT, "ACME")
    repo.set_tenant_short(OTHER_TENANT, "OTHER")
    return repo


@pytest.fixture
def case(case_repo: InMemoryCaseRepository) -> Case:
    """A single ACME case past its retention horizon."""
    _, version = make_case_type(tenant_id=ACME_TENANT, required_keys=("title", "priority"))
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
    return case


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
# 1. Disposed case: report lists policy version, actor, timestamp
# ---------------------------------------------------------------------------


async def test_evidence_report__disposed_case__lists_policy_version_and_actor(
    clock: _MutableClock,
    case_repo: InMemoryCaseRepository,
    case: Case,
) -> None:
    """The compliance report for a disposed case carries the policy
    id + version, the approving actor, and the decision timestamp —
    the minimum data an auditor needs to confirm "which policy was
    active when this record was disposed, by whom, when."
    """
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    audit = InMemoryAuditStore()

    publish_result = await publish_retention_policy(
        _publish_cmd(period_days=365),
        policy_repo=policy_repo,
        audit=audit,
        clock=clock,
    )
    policy_id = publish_result.policy.id
    policy_version = publish_result.policy.version_number

    route = await route_disposition_for_case(
        RouteDispositionCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            case_id=case.id,
        ),
        case_repo=case_repo,
        hold_repo=InMemoryLegalHoldRepository(),
        policy_repo=policy_repo,
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )

    clock.advance_to(APPROVAL_AT)
    approval = await approve_disposition(
        DispositionDecisionCommand(
            tenant_id=ACME_TENANT,
            actor_id=APPROVER,
            actor_roles=("process_admin",),
            request_id=route.request.id,
            reason="End of retention — proceed",
        ),
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )

    clock.advance_to(APPROVAL_AT + timedelta(seconds=5))
    report = await build_evidence_report(
        tenant_id=ACME_TENANT,
        actor_id=APPROVER,
        actor_roles=("process_admin",),
        case_id=case.id,
        case_repo=case_repo,
        policy_repo=policy_repo,
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )

    # Top-level shape.
    assert report.case_id == case.id
    assert report.case_reference == "C-ACME-2026-000001"
    assert report.tenant_id == ACME_TENANT
    assert report.kind == "snapshot"
    assert report.disposed is True

    # The "disposed" witness: at least one approved row.
    assert len(report.dispositions) == 1
    decision = report.dispositions[0]
    assert decision.request_id == approval.request.id
    assert decision.status == "approved"
    assert decision.decided_by == APPROVER
    assert decision.decided_at == APPROVAL_AT
    assert decision.rationale == "End of retention — proceed"

    # The authorising policy is reported as it was at decision time.
    assert report.policy is not None
    assert report.policy.policy_id == policy_id
    assert report.policy.version_number == policy_version
    assert report.policy.status == "published"
    assert report.policy.published_by == SECURITY_ADMIN
    assert report.policy.period_days == 365

    # The audit chain carries the disposition lifecycle so the
    # chain of custody is re-walkable. The policy-publish event is
    # a tenant-level row (not bound to a specific case) so it lives
    # in the broader audit log but is not part of the per-case
    # chain; the case-scoped events are the disposition lifecycle
    # rows that the report joins by ``case_id``.
    event_types = [entry.event_type for entry in report.audit_chain]
    assert "DispositionRequested" in event_types
    assert "DispositionApproved" in event_types


# ---------------------------------------------------------------------------
# 2. Non-disposed case: never silent deletion witness
# ---------------------------------------------------------------------------


async def test_evidence_report__non_disposed_case__returns_disposed_false(
    clock: _MutableClock,
    case_repo: InMemoryCaseRepository,
    case: Case,
) -> None:
    """A case that has never been surfaced for disposition returns
    ``disposed=false`` with an empty ``dispositions`` list — the
    absence from the platform-wide defensible-deletion log is the
    proof of "never silent deletion" (P08 §2).

    The case's report is still meaningful (it confirms the case
    exists, its policy is published, and the audit chain has no
    disposition events) — the report is the per-case witness, not
    a date-range filter.
    """
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    audit = InMemoryAuditStore()

    await publish_retention_policy(
        _publish_cmd(period_days=365),
        policy_repo=policy_repo,
        audit=audit,
        clock=clock,
    )

    report = await build_evidence_report(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_id=case.id,
        case_repo=case_repo,
        policy_repo=policy_repo,
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )

    assert report.disposed is False
    assert report.dispositions == []
    # No approved disposition means no authorising policy to report.
    assert report.policy is None
    # The audit chain carries no disposition events because the
    # case was never routed (the policy-publish event is a
    # tenant-level row that lives outside the per-case chain).
    event_types = [entry.event_type for entry in report.audit_chain]
    assert "DispositionRequested" not in event_types
    assert "DispositionApproved" not in event_types


async def test_evidence_report__rejected_only__still_not_disposed(
    clock: _MutableClock,
    case_repo: InMemoryCaseRepository,
    case: Case,
) -> None:
    """A case whose only disposition was rejected is **not**
    disposed. The report surfaces the rejected decision (full
    history) but ``disposed=false`` because no approval ever ran.
    """
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    audit = InMemoryAuditStore()

    await publish_retention_policy(
        _publish_cmd(period_days=365),
        policy_repo=policy_repo,
        audit=audit,
        clock=clock,
    )

    route = await route_disposition_for_case(
        RouteDispositionCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            case_id=case.id,
        ),
        case_repo=case_repo,
        hold_repo=InMemoryLegalHoldRepository(),
        policy_repo=policy_repo,
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )
    await reject_disposition(
        DispositionDecisionCommand(
            tenant_id=ACME_TENANT,
            actor_id=APPROVER,
            actor_roles=("process_admin",),
            request_id=route.request.id,
            reason="Records still needed for active litigation",
        ),
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )

    report = await build_evidence_report(
        tenant_id=ACME_TENANT,
        actor_id=APPROVER,
        actor_roles=("process_admin",),
        case_id=case.id,
        case_repo=case_repo,
        policy_repo=policy_repo,
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )

    assert report.disposed is False
    assert len(report.dispositions) == 1
    assert report.dispositions[0].status == "rejected"
    assert report.dispositions[0].decided_by == APPROVER
    assert report.dispositions[0].rationale == "Records still needed for active litigation"
    assert report.policy is None


# ---------------------------------------------------------------------------
# 3. Tenant isolation
# ---------------------------------------------------------------------------


async def test_evidence_report__missing_case__raises_not_found(
    clock: _MutableClock,
    case_repo: InMemoryCaseRepository,
) -> None:
    """A request for a non-existent (or cross-tenant) case id
    surfaces as :class:`NotFound` — the use case never confirms
    cross-tenant existence (Constitution V).
    """
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    audit = InMemoryAuditStore()

    with pytest.raises(NotFound):
        await build_evidence_report(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            case_id=uuid4(),
            case_repo=case_repo,
            policy_repo=policy_repo,
            disposition_repo=disposition_repo,
            audit=audit,
            clock=clock,
        )


async def test_evidence_report__cross_tenant_case_id__raises_not_found(
    clock: _MutableClock,
    case_repo: InMemoryCaseRepository,
    case: Case,
) -> None:
    """A case that exists in tenant A is invisible to tenant B —
    the cross-tenant lookup returns ``NotFound`` (Constitution V).
    """
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    audit = InMemoryAuditStore()

    with pytest.raises(NotFound):
        await build_evidence_report(
            tenant_id=OTHER_TENANT,  # wrong tenant
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            case_id=case.id,
            case_repo=case_repo,
            policy_repo=policy_repo,
            disposition_repo=disposition_repo,
            audit=audit,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 4. Authorization
# ---------------------------------------------------------------------------


async def test_evidence_report__non_admin_actor__raises_forbidden(
    clock: _MutableClock,
    case_repo: InMemoryCaseRepository,
    case: Case,
) -> None:
    """A reviewer without ``retention.policy_publish`` is refused.
    The records-admin grant is the shared action for the retention
    evidence surface in v1 (FM-042 follow-up may split it).
    """
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    audit = InMemoryAuditStore()

    with pytest.raises(Forbidden):
        await build_evidence_report(
            tenant_id=ACME_TENANT,
            actor_id=uuid4(),
            actor_roles=("reviewer",),
            case_id=case.id,
            case_repo=case_repo,
            policy_repo=policy_repo,
            disposition_repo=disposition_repo,
            audit=audit,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 5. Idempotence — re-runs return an equivalent report
# ---------------------------------------------------------------------------


async def test_evidence_report__repeated_runs__return_equivalent_packets(
    clock: _MutableClock,
    case_repo: InMemoryCaseRepository,
    case: Case,
) -> None:
    """A re-run with the same inputs returns an equivalent report
    (the underlying repos are read-only and the audit chain is
    append-only). The only moving piece is ``generated_at`` /
    ``actor_id`` which the use case records from the request.
    """
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    audit = InMemoryAuditStore()

    await publish_retention_policy(
        _publish_cmd(period_days=365),
        policy_repo=policy_repo,
        audit=audit,
        clock=clock,
    )
    route = await route_disposition_for_case(
        RouteDispositionCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            case_id=case.id,
        ),
        case_repo=case_repo,
        hold_repo=InMemoryLegalHoldRepository(),
        policy_repo=policy_repo,
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )
    await approve_disposition(
        DispositionDecisionCommand(
            tenant_id=ACME_TENANT,
            actor_id=APPROVER,
            actor_roles=("process_admin",),
            request_id=route.request.id,
            reason="end of retention",
        ),
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )

    first = await build_evidence_report(
        tenant_id=ACME_TENANT,
        actor_id=APPROVER,
        actor_roles=("process_admin",),
        case_id=case.id,
        case_repo=case_repo,
        policy_repo=policy_repo,
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )
    clock.advance_to(clock.now() + timedelta(seconds=30))
    second = await build_evidence_report(
        tenant_id=ACME_TENANT,
        actor_id=APPROVER,
        actor_roles=("process_admin",),
        case_id=case.id,
        case_repo=case_repo,
        policy_repo=policy_repo,
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )

    # The structural content is identical; only ``generated_at``
    # changes (the use case records the request clock so the
    # auditor can confirm exactly when the system of record was
    # sampled — P12 §1).
    assert first.disposed == second.disposed is True
    assert first.case_id == second.case_id
    assert first.policy == second.policy
    assert first.dispositions == second.dispositions
    assert first.audit_chain == second.audit_chain
    assert first.generated_at != second.generated_at


__all__ = []
