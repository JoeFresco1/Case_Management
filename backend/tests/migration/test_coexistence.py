"""Tests for the coexistence policy (P14-US6 / FM-059).

Acceptance scenarios from ``specs/pillars/P14-integrations-migration/pillar.md``
US6 and the FM-059 acceptance narrative:

1. **Truthful declaration**: a process administrator drafts a
   coexistence policy that names source systems, declares ownership
   and migration state, and (by default) records
   ``real_time_sync_enabled=False``. The audit row records every
   declared field verbatim.
2. **No pretend real-time sync**: a request with
   ``real_time_sync_enabled=True`` and no authorization record is
   refused by the domain — even if the application layer were
   bypassed. This is the deterministic backstop of Constitution V.10 /
   XII.5 / P14 §1.
3. **Provenance is visible**: the policy exposes ``source_systems``,
   ``ownership``, ``migration_state``, and ``sync_evidence`` (the
   tuple of import-job ids). The operator console reads these
   directly.
4. **No LIVE state**: ``LIVE`` is intentionally not in the
   ``MigrationState`` enum — a case is "live in this platform" by
   virtue of not being ``divergent`` / ``unmigrated``, not by being
   labelled ``live``.
5. **Multi-tenant isolation**: a policy under tenant A is not visible
   under tenant B.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5. Tests are deterministic: fixed clock, fixed UUIDs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.application.migration import coexistence as manage_coexistence
from app.domain.coexistence import (
    CoexistencePolicy,
    MigrationState,
    OwnershipRule,
    is_terminal_migration_state,
)
from app.domain.errors import (
    Conflict,
    Forbidden,
    InvariantViolation,
    NotFound,
    ValidationFailed,
)

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
ADMIN_USER = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
INTAKE_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
POLICY_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
IMPORT_JOB_ID = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")

FIXED_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
SECOND_NOW = datetime(2026, 9, 1, 12, 30, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class InMemoryCoexistencePolicyRepository:
    """In-memory :class:`CoexistencePolicyRepository`."""

    def __init__(self) -> None:
        self._rows: dict[UUID, CoexistencePolicy] = {}

    async def get(self, tenant_id: UUID, policy_id: UUID) -> CoexistencePolicy | None:
        row = self._rows.get(policy_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def get_by_key(self, tenant_id: UUID, key: str) -> CoexistencePolicy | None:
        for row in self._rows.values():
            if row.tenant_id == tenant_id and row.key == key:
                return row
        return None

    async def get_latest_version(self, tenant_id: UUID, key: str) -> CoexistencePolicy | None:
        rows = [r for r in self._rows.values() if r.tenant_id == tenant_id and r.key == key]
        if not rows:
            return None
        return max(rows, key=lambda r: r.version)

    async def list(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CoexistencePolicy]:
        rows = [r for r in self._rows.values() if r.tenant_id == tenant_id]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[offset : offset + limit]

    async def add(self, policy: CoexistencePolicy) -> None:
        self._rows[policy.id] = policy

    async def update(self, policy: CoexistencePolicy) -> None:
        self._rows[policy.id] = policy


@dataclass
class StagedAudit:
    tenant_id: UUID
    event_type: str
    actor_id: UUID
    subject_type: str
    subject_id: UUID
    payload: dict[str, Any]
    correlation_id: str


class InMemoryAuditStore:
    def __init__(self) -> None:
        self.events: list[StagedAudit] = []

    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID,
        actor_type: str,  # noqa: ARG002
        subject_type: str,
        subject_id: UUID,
        case_id: UUID | None,  # noqa: ARG002
        correlation_id: str,
        causation_id: str | None,  # noqa: ARG002
        payload: dict[str, Any],
        occurred_at: datetime,  # noqa: ARG002
    ) -> None:
        self.events.append(
            StagedAudit(
                tenant_id=tenant_id,
                event_type=event_type,
                actor_id=actor_id,
                subject_type=subject_type,
                subject_id=subject_id,
                payload=payload,
                correlation_id=correlation_id,
            )
        )


@dataclass
class StagedOutbox:
    tenant_id: UUID
    event_type: str
    event_id: UUID
    payload: dict[str, Any]


class InMemoryOutbox:
    def __init__(self) -> None:
        self.events: list[StagedOutbox] = []

    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        event_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        self.events.append(
            StagedOutbox(
                tenant_id=tenant_id,
                event_type=event_type,
                event_id=event_id,
                payload=payload,
            )
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


@pytest.fixture
def policy_repo() -> InMemoryCoexistencePolicyRepository:
    return InMemoryCoexistencePolicyRepository()


@pytest.fixture
def audit() -> InMemoryAuditStore:
    return InMemoryAuditStore()


@pytest.fixture
def outbox() -> InMemoryOutbox:
    return InMemoryOutbox()


def _admin_cmd(
    *,
    key: str = "synthetic-onbase-inbound",
    source_systems: tuple[str, ...] = ("synthetic-legacy",),
    ownership: OwnershipRule = OwnershipRule.THIS_PLATFORM_SYSTEM_OF_RECORD,
    migration_state: MigrationState = MigrationState.UNMIGRATED,
    real_time_sync_enabled: bool = False,
    tenant_id: UUID = ACME_TENANT,
) -> manage_coexistence.DefineCoexistencePolicyCommand:
    return manage_coexistence.DefineCoexistencePolicyCommand(
        tenant_id=tenant_id,
        actor_id=ADMIN_USER,
        actor_roles=("process_admin",),
        key=key,
        source_systems=source_systems,
        ownership=ownership,
        migration_state=migration_state,
        provenance_note="ADR-016; legal review 2026-09-01",
        real_time_sync_enabled=real_time_sync_enabled,
        correlation_id="corr-coex-001",
    )


# ---------------------------------------------------------------------------
# 1. Truthful declaration
# ---------------------------------------------------------------------------


async def test_define_coexistence_policy__truthful_defaults__records_source_ownership_state(
    clock: FixedClock,
    policy_repo: InMemoryCoexistencePolicyRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    cmd = _admin_cmd()

    result = await manage_coexistence.define_coexistence_policy(
        cmd,
        policy_repo=policy_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    policy = result.policy
    # given: a process administrator defines a v1 policy
    # when: the use case runs
    # then: source_systems / ownership / migration_state are recorded
    assert policy.tenant_id == ACME_TENANT
    assert policy.key == "synthetic-onbase-inbound"
    assert policy.version == 1
    assert policy.source_systems == ("synthetic-legacy",)
    assert policy.ownership is OwnershipRule.THIS_PLATFORM_SYSTEM_OF_RECORD
    assert policy.migration_state is MigrationState.UNMIGRATED
    # then: the default "no real-time sync" is on the row
    assert policy.real_time_sync_enabled is False
    assert policy.sync_authorized_by is None
    assert policy.sync_authorized_at is None
    assert policy.sync_evidence == ()

    # then: provenance note is on the row (visible to operators)
    assert "ADR-016" in policy.provenance_note

    # then: audit + outbox both fire the draft event with the declared
    # fields visible to the operator console
    assert len(audit.events) == 1
    audit_event = audit.events[0]
    assert audit_event.event_type == manage_coexistence.EVENT_COEXISTENCE_POLICY_DRAFTED
    assert audit_event.subject_type == manage_coexistence.SUBJECT_TYPE_COEXISTENCE_POLICY
    assert audit_event.payload["source_systems"] == ["synthetic-legacy"]
    assert audit_event.payload["ownership"] == "this_platform_system_of_record"
    assert audit_event.payload["migration_state"] == "unmigrated"
    assert audit_event.payload["real_time_sync_enabled"] is False
    assert audit_event.payload["is_terminal"] is False

    assert len(outbox.events) == 1
    assert outbox.events[0].event_type == manage_coexistence.EVENT_COEXISTENCE_POLICY_DRAFTED
    assert outbox.events[0].payload["real_time_sync_enabled"] is False


# ---------------------------------------------------------------------------
# 2. No pretend real-time sync — domain backstop
# ---------------------------------------------------------------------------


def test_coexistence_policy__real_time_sync_enabled_without_auth__domain_refuses() -> None:
    # given: a caller attempts to construct a row with the sync flag
    # but no sync_authorized_by record
    # when: the factory runs
    # then: InvariantViolation — the deterministic backstop
    with pytest.raises(InvariantViolation) as exc:
        CoexistencePolicy.draft(
            tenant_id=ACME_TENANT,
            key="bogus",
            source_systems=("synthetic-legacy",),
            ownership=OwnershipRule.THIS_PLATFORM_SYSTEM_OF_RECORD,
            migration_state=MigrationState.UNMIGRATED,
            created_by=ADMIN_USER,
            now=FIXED_NOW,
            real_time_sync_enabled=True,
            sync_authorized_by=None,
            sync_authorized_at=None,
        )
    assert "real_time_sync_enabled=True" in str(exc.value)
    assert "sync_authorized_by" in str(exc.value)


def test_coexistence_policy__real_time_sync_disabled_with_stale_auth__domain_refuses() -> None:
    # given: the row has the auth record but real_time_sync_enabled=False
    # when: the factory runs
    # then: InvariantViolation — the auth record must match the flag
    with pytest.raises(InvariantViolation) as exc:
        CoexistencePolicy.draft(
            tenant_id=ACME_TENANT,
            key="bogus",
            source_systems=("synthetic-legacy",),
            ownership=OwnershipRule.THIS_PLATFORM_SYSTEM_OF_RECORD,
            migration_state=MigrationState.UNMIGRATED,
            created_by=ADMIN_USER,
            now=FIXED_NOW,
            real_time_sync_enabled=False,
            sync_authorized_by=ADMIN_USER,
            sync_authorized_at=FIXED_NOW,
        )
    assert "real_time_sync_enabled=False" in str(exc.value)
    assert "sync authorization record" in str(exc.value)


def test_coexistence_policy__shared_ownership_without_auth__domain_refuses() -> None:
    # given: ownership=SHARED but no ownership_authorized_by
    # when: the factory runs
    # then: InvariantViolation — SHARED implies a bidirectional
    # write channel that does not exist in the first slice
    with pytest.raises(InvariantViolation) as exc:
        CoexistencePolicy.draft(
            tenant_id=ACME_TENANT,
            key="bogus",
            source_systems=("synthetic-legacy",),
            ownership=OwnershipRule.SHARED,
            migration_state=MigrationState.UNMIGRATED,
            created_by=ADMIN_USER,
            now=FIXED_NOW,
        )
    assert "ownership='shared'" in str(exc.value)
    assert "ownership_authorized_by" in str(exc.value)


async def test_define_coexistence_policy__unrecognized_source_system__rejected(
    clock: FixedClock,
    policy_repo: InMemoryCoexistencePolicyRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    cmd = _admin_cmd(source_systems=("totally-made-up-system",))

    with pytest.raises(ValidationFailed) as exc:
        await manage_coexistence.define_coexistence_policy(
            cmd,
            policy_repo=policy_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
    assert "unrecognized source system" in str(exc.value)
    assert "totally-made-up-system" in str(exc.value)


async def test_define_coexistence_policy__duplicate_key__rejected_with_conflict(
    clock: FixedClock,
    policy_repo: InMemoryCoexistencePolicyRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    # given: a policy already exists at this key
    await manage_coexistence.define_coexistence_policy(
        _admin_cmd(),
        policy_repo=policy_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    # when: a second policy tries the same key
    with pytest.raises(Conflict):
        await manage_coexistence.define_coexistence_policy(
            _admin_cmd(),
            policy_repo=policy_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 3. Import evidence — append-only, idempotent, advances state
# ---------------------------------------------------------------------------


async def test_record_import_evidence__appends_id_and_advances_unmigrated_to_imported(
    clock: FixedClock,
    policy_repo: InMemoryCoexistencePolicyRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    # given: a freshly defined policy in UNMIGRATED
    drafted = await manage_coexistence.define_coexistence_policy(
        _admin_cmd(),
        policy_repo=policy_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert drafted.policy.migration_state is MigrationState.UNMIGRATED

    # when: an import job completes and the operator records evidence
    result = await manage_coexistence.record_import_evidence(
        manage_coexistence.RecordImportEvidenceCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            policy_id=drafted.policy.id,
            import_job_id=IMPORT_JOB_ID,
            correlation_id="corr-coex-002",
        ),
        policy_repo=policy_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # then: evidence is appended
    assert IMPORT_JOB_ID in result.policy.sync_evidence
    # then: state advanced from UNMIGRATED to IMPORTED
    assert result.policy.migration_state is MigrationState.IMPORTED
    # then: audit + outbox show the evidence trail
    evidence_events = [
        e
        for e in audit.events
        if e.event_type == manage_coexistence.EVENT_COEXISTENCE_IMPORT_EVIDENCE_RECORDED
    ]
    assert len(evidence_events) == 1
    assert evidence_events[0].payload["import_job_id"] == str(IMPORT_JOB_ID)
    assert evidence_events[0].payload["migration_state"] == "imported"
    assert evidence_events[0].payload["evidence_count"] == 1


async def test_record_import_evidence__duplicate_id__noop_and_no_extra_audit(
    clock: FixedClock,
    policy_repo: InMemoryCoexistencePolicyRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    drafted = await manage_coexistence.define_coexistence_policy(
        _admin_cmd(),
        policy_repo=policy_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    # given: evidence already recorded for this import job
    await manage_coexistence.record_import_evidence(
        manage_coexistence.RecordImportEvidenceCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            policy_id=drafted.policy.id,
            import_job_id=IMPORT_JOB_ID,
            correlation_id="corr-coex-002",
        ),
        policy_repo=policy_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    audit_count_after_first = sum(
        1
        for e in audit.events
        if e.event_type == manage_coexistence.EVENT_COEXISTENCE_IMPORT_EVIDENCE_RECORDED
    )

    # when: a retry tries to record the same id
    result = await manage_coexistence.record_import_evidence(
        manage_coexistence.RecordImportEvidenceCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            policy_id=drafted.policy.id,
            import_job_id=IMPORT_JOB_ID,
            correlation_id="corr-coex-003",
        ),
        policy_repo=policy_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # then: the policy is unchanged
    assert result.policy.sync_evidence.count(IMPORT_JOB_ID) == 1
    # then: no second audit row was emitted
    audit_count_after_second = sum(
        1
        for e in audit.events
        if e.event_type == manage_coexistence.EVENT_COEXISTENCE_IMPORT_EVIDENCE_RECORDED
    )
    assert audit_count_after_second == audit_count_after_first


# ---------------------------------------------------------------------------
# 4. Migration state transitions
# ---------------------------------------------------------------------------


async def test_set_migration_state__imported_to_reconciled__advances(
    clock: FixedClock,
    policy_repo: InMemoryCoexistencePolicyRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    drafted = await manage_coexistence.define_coexistence_policy(
        _admin_cmd(),
        policy_repo=policy_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    clock._now = SECOND_NOW  # noqa: SLF001 — deterministic clock advance

    result = await manage_coexistence.set_migration_state(
        manage_coexistence.SetMigrationStateCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            policy_id=drafted.policy.id,
            new_state=MigrationState.RECONCILED,
            correlation_id="corr-coex-004",
        ),
        policy_repo=policy_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    assert result.policy.migration_state is MigrationState.RECONCILED
    assert result.policy.is_terminal() is False


async def test_set_migration_state__terminal_retired_from_source__is_terminal(
    clock: FixedClock,
    policy_repo: InMemoryCoexistencePolicyRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    drafted = await manage_coexistence.define_coexistence_policy(
        _admin_cmd(),
        policy_repo=policy_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    result = await manage_coexistence.set_migration_state(
        manage_coexistence.SetMigrationStateCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            policy_id=drafted.policy.id,
            new_state=MigrationState.RETIRED_FROM_SOURCE,
            correlation_id="corr-coex-005",
        ),
        policy_repo=policy_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    assert result.policy.migration_state is MigrationState.RETIRED_FROM_SOURCE
    assert result.policy.is_terminal() is True
    assert is_terminal_migration_state(MigrationState.RETIRED_FROM_SOURCE) is True


async def test_set_migration_state__from_terminal__domain_refuses(
    clock: FixedClock,
    policy_repo: InMemoryCoexistencePolicyRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    drafted = await manage_coexistence.define_coexistence_policy(
        _admin_cmd(),
        policy_repo=policy_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    await manage_coexistence.set_migration_state(
        manage_coexistence.SetMigrationStateCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            policy_id=drafted.policy.id,
            new_state=MigrationState.RETIRED_FROM_SOURCE,
            correlation_id="corr-coex-005",
        ),
        policy_repo=policy_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # when: a transition out of the terminal state is attempted
    with pytest.raises(InvariantViolation) as exc:
        await manage_coexistence.set_migration_state(
            manage_coexistence.SetMigrationStateCommand(
                tenant_id=ACME_TENANT,
                actor_id=ADMIN_USER,
                actor_roles=("process_admin",),
                policy_id=drafted.policy.id,
                new_state=MigrationState.RECONCILED,
                correlation_id="corr-coex-006",
            ),
            policy_repo=policy_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
    assert "terminal" in str(exc.value)


# ---------------------------------------------------------------------------
# 5. Multi-tenant isolation
# ---------------------------------------------------------------------------


async def test_get_coexistence_policy__cross_tenant__not_found(
    clock: FixedClock,
    policy_repo: InMemoryCoexistencePolicyRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    drafted = await manage_coexistence.define_coexistence_policy(
        _admin_cmd(),
        policy_repo=policy_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # when: a query is made under a different tenant
    with pytest.raises(NotFound):
        await manage_coexistence.get_coexistence_policy(
            manage_coexistence.GetCoexistencePolicyQuery(
                tenant_id=OTHER_TENANT,
                actor_id=ADMIN_USER,
                actor_roles=("process_admin",),
                policy_id=drafted.policy.id,
            ),
            policy_repo=policy_repo,
        )


async def test_list_coexistence_policies__cross_tenant__empty(
    clock: FixedClock,
    policy_repo: InMemoryCoexistencePolicyRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    await manage_coexistence.define_coexistence_policy(
        _admin_cmd(tenant_id=ACME_TENANT),
        policy_repo=policy_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    rows = await manage_coexistence.list_coexistence_policies(
        manage_coexistence.ListCoexistencePoliciesQuery(
            tenant_id=OTHER_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
        ),
        policy_repo=policy_repo,
    )
    assert rows == []


# ---------------------------------------------------------------------------
# 6. Authorization
# ---------------------------------------------------------------------------


async def test_define_coexistence_policy__non_admin__forbidden(
    clock: FixedClock,
    policy_repo: InMemoryCoexistencePolicyRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    cmd = manage_coexistence.DefineCoexistencePolicyCommand(
        tenant_id=ACME_TENANT,
        actor_id=INTAKE_USER,
        actor_roles=("intake",),  # not a process admin
        key="synthetic-onbase-inbound",
        source_systems=("synthetic-legacy",),
        ownership=OwnershipRule.THIS_PLATFORM_SYSTEM_OF_RECORD,
        migration_state=MigrationState.UNMIGRATED,
        provenance_note="",
        real_time_sync_enabled=False,
        correlation_id="corr-coex-007",
    )

    with pytest.raises(Forbidden):
        await manage_coexistence.define_coexistence_policy(
            cmd,
            policy_repo=policy_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 7. Provenance visibility — operator-readable summary
# ---------------------------------------------------------------------------


async def test_coexistence_policy__references_and_state_queries__are_truthful(
    clock: FixedClock,
    policy_repo: InMemoryCoexistencePolicyRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    drafted = await manage_coexistence.define_coexistence_policy(
        _admin_cmd(source_systems=("synthetic-legacy", "onbase")),
        policy_repo=policy_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    policy = drafted.policy

    # given: the policy names two legacy systems
    assert policy.references("synthetic-legacy") is True
    assert policy.references("onbase") is True
    # then: an unrelated system is not silently accepted
    assert policy.references("panzura") is False
    # then: the sync channel is declared OFF — the truthful default
    assert policy.describes_sync() is False
    # then: "live" is not a state in the closed enum — it is a
    # property expressed by the migration_state being non-divergent
    assert "LIVE" not in {s.value for s in MigrationState}
    assert "live" not in policy.migration_state.value
