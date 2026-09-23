"""Held-disposition refusal across case/document/audit (FM-039 x FM-040).

Acceptance (002-leftovers T6):

- Held disposition returns ``refused_hold`` with the cited policy
  version; no case, document, or audit rows are deleted.
- Refusal + execution audit rows are atomic with the decision.
- Fail-closed authorization + tenant scope (no audit on deny).
- Idempotent replay with the same key returns the stored outcome
  without a second audit row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.application.holds.manage_holds import (
    PlaceHoldCommand,
    ReleaseHoldCommand,
    place_hold,
    release_hold,
)
from app.application.retention.disposition import (
    DispositionDecisionCommand,
    ExecuteDispositionCommand,
    PublishRetentionPolicyCommand,
    RouteDispositionCommand,
    approve_disposition,
    execute_disposition,
    publish_retention_policy,
    route_disposition_for_case,
)
from app.domain.case import Case, CaseReference
from app.domain.errors import Conflict, Forbidden, NotFound
from app.domain.legal_hold import REFUSED_HOLD_CODE
from app.domain.retention import RetentionCategory
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
    InMemoryDocumentRepository,
    InMemoryIdempotencyStore,
    make_case_type,
)

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
SECURITY_ADMIN = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
INTAKE = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

CREATED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
HORIZON = CREATED_AT + timedelta(days=365)


@dataclass
class _MutableClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


@pytest.fixture
def clock() -> _MutableClock:
    return _MutableClock(HORIZON + timedelta(days=1))


def _seed_case(case_repo: InMemoryCaseRepository) -> Case:
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case_repo.set_tenant_short(OTHER_TENANT, "OTHER")
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


async def _publish(policy_repo: InMemoryRetentionPolicyRepository, clock: _MutableClock) -> None:
    await publish_retention_policy(
        PublishRetentionPolicyCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            category=RetentionCategory.CASE,
            target_key="",
            period_days=365,
            correlation_id="corr-publish-held",
        ),
        policy_repo=policy_repo,
        audit=InMemoryAuditStore(),
        clock=clock,
    )


async def _route_and_approve(
    case_repo: InMemoryCaseRepository,
    policy_repo: InMemoryRetentionPolicyRepository,
    disposition_repo: InMemoryDispositionRequestRepository,
    hold_repo: InMemoryLegalHoldRepository,
    audit: InMemoryAuditStore,
    clock: _MutableClock,
    case: Case,
) -> UUID:
    route = await route_disposition_for_case(
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
    approved = await approve_disposition(
        DispositionDecisionCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            request_id=route.request.id,
            reason="End of retention",
        ),
        disposition_repo=disposition_repo,
        audit=audit,
        clock=clock,
    )
    return approved.request.id


async def test_execute__held__returns_refused_hold_with_policy_version() -> None:
    """Held execution cites the policy version and deletes nothing."""
    # given: an approved request plus one document + audit history
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    audit = InMemoryAuditStore()
    docs = InMemoryDocumentRepository()
    await _publish(policy_repo, clock)
    request_id = await _route_and_approve(
        case_repo, policy_repo, disposition_repo, hold_repo, audit, clock, case
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
    audits_before = len(audit.events)

    # when: execution is attempted under hold
    result = await execute_disposition(
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
        document_repo=docs,  # type: ignore[arg-type]
    )

    # then: refused_hold cites policy v1, covers all scopes, nothing deleted
    assert result.outcome == REFUSED_HOLD_CODE == "refused_hold"
    assert result.policy_version == 1
    assert set(result.blocked_scopes) == {"case", "document", "audit"}
    assert result.hold_id is not None
    assert (await case_repo.get(ACME_TENANT, case.id)) is not None
    assert await docs.list_for_case(tenant_id=ACME_TENANT, case_id=case.id) == []
    assert len(audit.events) == audits_before + 1
    refused = audit.events[-1]
    assert refused.event_type == "DispositionRefused"
    assert refused.payload["code"] == "refused_hold"
    assert refused.payload["policy_version"] == 1


async def test_execute__release_then_execute__succeeds() -> None:
    """Releasing the hold unblocks execution (place/release lifecycle)."""
    # given: held then released
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    audit = InMemoryAuditStore()
    await _publish(policy_repo, clock)
    request_id = await _route_and_approve(
        case_repo, policy_repo, disposition_repo, hold_repo, audit, clock, case
    )
    await place_hold(
        PlaceHoldCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            case_id=case.id,
            reason="Hold for review",
        ),
        case_repo=case_repo,
        hold_repo=hold_repo,
        audit=audit,
        clock=clock,
    )
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

    # when
    result = await execute_disposition(
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

    # then
    assert result.outcome == "executed"
    assert result.policy_version == 1
    assert result.hold_id is None
    assert audit.events[-1].event_type == "DispositionExecuted"


async def test_execute__forbidden_actor__fail_closed_no_audit() -> None:
    """Non-admin execution is denied before any read or write."""
    # given
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    audit = InMemoryAuditStore()
    await _publish(policy_repo, clock)
    request_id = await _route_and_approve(
        case_repo, policy_repo, disposition_repo, hold_repo, audit, clock, case
    )
    before = len(audit.events)

    # when / then
    with pytest.raises(Forbidden):
        await execute_disposition(
            ExecuteDispositionCommand(
                tenant_id=ACME_TENANT,
                actor_id=uuid4(),
                actor_roles=("reviewer",),
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


async def test_execute__cross_tenant__not_found_no_oracle() -> None:
    """Cross-tenant request id never confirms existence."""
    # given
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    audit = InMemoryAuditStore()
    await _publish(policy_repo, clock)
    request_id = await _route_and_approve(
        case_repo, policy_repo, disposition_repo, hold_repo, audit, clock, case
    )
    before = len(audit.events)

    # when / then
    with pytest.raises(NotFound):
        await execute_disposition(
            ExecuteDispositionCommand(
                tenant_id=OTHER_TENANT,
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


async def test_execute__idempotent_replay__single_audit_row() -> None:
    """Same key replays the refusal without a second audit row."""
    # given: held execution with an idempotency key
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    audit = InMemoryAuditStore()
    store = InMemoryIdempotencyStore()
    await _publish(policy_repo, clock)
    request_id = await _route_and_approve(
        case_repo, policy_repo, disposition_repo, hold_repo, audit, clock, case
    )
    await place_hold(
        PlaceHoldCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            case_id=case.id,
            reason="Hold for replay",
        ),
        case_repo=case_repo,
        hold_repo=hold_repo,
        audit=audit,
        clock=clock,
    )

    # when
    first = await execute_disposition(
        ExecuteDispositionCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            request_id=request_id,
            idempotency_key="exec-held-1",
        ),
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
        ExecuteDispositionCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            request_id=request_id,
            idempotency_key="exec-held-1",
        ),
        disposition_repo=disposition_repo,
        policy_repo=policy_repo,
        case_repo=case_repo,
        hold_repo=hold_repo,
        audit=audit,
        clock=clock,
        idempotency_store=store,  # type: ignore[arg-type]
    )

    # then: same outcome, no second audit, case untouched
    assert first.outcome == second.outcome == "refused_hold"
    assert first.policy_version == second.policy_version == 1
    assert len(audit.events) == count_after_first
    assert (await case_repo.get(ACME_TENANT, case.id)) is not None


async def test_execute__idempotency_key_mismatch__conflict() -> None:
    """A key bound to one request cannot execute another."""
    # given
    clock = _MutableClock(HORIZON + timedelta(days=1))
    case_repo = InMemoryCaseRepository()
    case = _seed_case(case_repo)
    policy_repo = InMemoryRetentionPolicyRepository()
    disposition_repo = InMemoryDispositionRequestRepository()
    hold_repo = InMemoryLegalHoldRepository()
    audit = InMemoryAuditStore()
    store = InMemoryIdempotencyStore()
    await _publish(policy_repo, clock)
    request_id = await _route_and_approve(
        case_repo, policy_repo, disposition_repo, hold_repo, audit, clock, case
    )
    await execute_disposition(
        ExecuteDispositionCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            request_id=request_id,
            idempotency_key="exec-once",
        ),
        disposition_repo=disposition_repo,
        policy_repo=policy_repo,
        case_repo=case_repo,
        hold_repo=hold_repo,
        audit=audit,
        clock=clock,
        idempotency_store=store,  # type: ignore[arg-type]
    )

    # when / then: same key, different request id
    with pytest.raises(Conflict):
        await execute_disposition(
            ExecuteDispositionCommand(
                tenant_id=ACME_TENANT,
                actor_id=SECURITY_ADMIN,
                actor_roles=("process_admin",),
                request_id=uuid4(),
                idempotency_key="exec-once",
            ),
            disposition_repo=disposition_repo,
            policy_repo=policy_repo,
            case_repo=case_repo,
            hold_repo=hold_repo,
            audit=audit,
            clock=clock,
            idempotency_store=store,  # type: ignore[arg-type]
        )


__all__: list[str] = []
