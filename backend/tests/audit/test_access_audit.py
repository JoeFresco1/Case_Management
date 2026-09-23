"""Access-decision audit tests (P09-US6 / FM-045; P11-US9).

Acceptance criteria from the task:

1. A denied sensitive-field access writes an ``AccessDecisionRecorded``
   row whose payload carries ``result=denied`` and the policy
   ``reason`` (``forbidden``). The cross-tenant probe must be visible
   in the report (P11-US2 / K1).
2. An allowed access writes an ``AccessDecisionRecorded`` row whose
   payload carries ``result=allowed`` and the granting role name in
   the ``reason``.
3. The report endpoint (``view_report`` / GET
   ``/api/v1/audit/access-decisions``) returns both outcomes and
   filters by actor, action, and result.

# Why in-memory fakes

The application-layer use cases are exercised with the in-memory
``AuditEventStore`` fake (``tests/audit/test_export_correction.py``)
so the access-decision recorder and the report projection are
covered without a live container (AGENTS.md §4.R5 / R1). The fake
implements ``list_access_decisions`` so the SQL filter shape is
exercised through the same use case the router calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.application.audit.access_audit import (
    ACCESS_KEY_ACTION,
    ACCESS_KEY_ACTOR_ROLES,
    ACCESS_KEY_REASON,
    ACCESS_KEY_RESULT,
    RESULT_ALLOWED,
    RESULT_DENIED,
    AccessDecisionInput,
    AccessDecisionQuery,
    AccessDecisionRecorder,
    build_recorder,
    record_decision,
    view_report,
)
from app.application.audit.record_event import (
    RecordAuditCommand,
)
from app.application.authorization import (
    ROLE_INTAKE,
    ROLE_SUPERVISOR,
    Action,
    Decision,
    Resource,
    authorize,
)
from app.deps import UserContext
from app.domain.audit import (
    ACCESS_DECISION_RECORDED as DOMAIN_ACCESS_DECISION_RECORDED,
)
from app.domain.audit import (
    compute_hash,
)
from app.domain.errors import Forbidden, ValidationFailed
from app.domain.ports import AuditEventStore

TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
INTake_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
REVIEWER_USER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SUPERVISOR_USER = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
SUBJECT = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
CORRELATION_BASE = "corr-access-audit"
FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# In-memory audit store + dedupe (mirrors test_export_correction.py)
# ---------------------------------------------------------------------------


@dataclass
class _FakeRow:
    id: UUID
    tenant_id: UUID
    event_type: str
    actor_id: UUID
    actor_type: str
    subject_type: str
    subject_id: UUID
    case_id: UUID | None
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any]
    occurred_at: datetime
    prev_hash: str
    hash: str


@dataclass
class _Stage:
    cmd: RecordAuditCommand
    prev_hash: str
    computed_hash: str


class InMemoryAuditStore(AuditEventStore):
    """Minimal audit store covering the access-decision read/write contract."""

    def __init__(self) -> None:
        self.staged: list[_Stage] = []
        self.committed: list[_FakeRow] = []
        self._seen: dict[tuple[UUID, str, str], bool] = {}
        self._id_seq = 0

    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID,
        actor_type: str,
        subject_type: str,
        subject_id: UUID,
        case_id: UUID | None,
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        prev = self._last_hash(tenant_id)
        h = compute_hash(prev, payload)
        self.staged.append(
            _Stage(
                cmd=RecordAuditCommand(
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    actor_type=actor_type,
                    event_type=event_type,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    case_id=case_id,
                    correlation_id=correlation_id,
                    causation_id=causation_id,
                    payload=payload,
                    occurred_at=occurred_at,
                ),
                prev_hash=prev,
                computed_hash=h,
            )
        )

    def commit(self) -> list[_FakeRow]:
        for stage in self.staged:
            self._id_seq += 1
            row = _FakeRow(
                id=UUID(int=self._id_seq),
                tenant_id=stage.cmd.tenant_id,
                event_type=stage.cmd.event_type,
                actor_id=stage.cmd.actor_id,
                actor_type=stage.cmd.actor_type,
                subject_type=stage.cmd.subject_type,
                subject_id=stage.cmd.subject_id,
                case_id=stage.cmd.case_id,
                correlation_id=stage.cmd.correlation_id,
                causation_id=stage.cmd.causation_id,
                payload=dict(stage.cmd.payload),
                occurred_at=stage.cmd.occurred_at,
                prev_hash=stage.prev_hash,
                hash=stage.computed_hash,
            )
            self.committed.append(row)
            self._seen[(row.tenant_id, row.event_type, row.correlation_id)] = True
        self.staged.clear()
        return list(self.committed)

    def _last_hash(self, tenant_id: UUID) -> str:
        for stage in reversed(self.staged):
            if stage.cmd.tenant_id == tenant_id:
                return stage.computed_hash
        for row in reversed(self.committed):
            if row.tenant_id == tenant_id:
                return row.hash
        return "0" * 64

    async def list_for_case(
        self,
        *,
        tenant_id: UUID,
        case_id: UUID,
        event_types: Any | None = None,
        actor_id: UUID | None = None,
        since: Any | None = None,
        until: Any | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[_FakeRow]:
        return []

    async def list_for_export(
        self,
        *,
        tenant_id: UUID,
        case_id: UUID | None,
        since: Any | None = None,
        until: Any | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[_FakeRow]:
        return []

    async def get_event(self, *, tenant_id: UUID, event_id: UUID) -> _FakeRow | None:
        for r in self.committed:
            if r.tenant_id == tenant_id and r.id == event_id:
                return r
        return None

    async def list_access_decisions(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID | None = None,
        subject_type: str | None = None,
        action: str | None = None,
        result: str | None = None,
        since: Any | None = None,
        until: Any | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[_FakeRow]:
        rows = [
            r
            for r in self.committed
            if r.tenant_id == tenant_id and r.event_type == DOMAIN_ACCESS_DECISION_RECORDED
        ]
        if actor_id is not None:
            rows = [r for r in rows if r.actor_id == actor_id]
        if subject_type:
            rows = [r for r in rows if r.subject_type == subject_type]
        if action:
            rows = [r for r in rows if r.payload.get(ACCESS_KEY_ACTION) == action]
        if result:
            rows = [r for r in rows if r.payload.get(ACCESS_KEY_RESULT) == result]
        if since is not None:
            rows = [r for r in rows if r.occurred_at >= since]
        if until is not None:
            rows = [r for r in rows if r.occurred_at <= until]
        rows.sort(key=lambda r: (r.occurred_at, r.id))
        return rows[:limit]

    def dedupe(self) -> Any:
        def _d(
            tenant_id: UUID,
            event_type: str,
            correlation_id: str,
            payload_hash_value: str,  # noqa: ARG001
        ) -> bool:
            return (
                tenant_id,
                event_type,
                correlation_id,
            ) in self._seen

        return _d


# ---------------------------------------------------------------------------
# Recording a single decision
# ---------------------------------------------------------------------------


@dataclass
class _FixedClock:
    now_value: datetime = FIXED_NOW

    def now(self) -> datetime:
        return self.now_value


async def test_record_decision__denied_writes_audit_row_with_reason() -> None:
    """A denied access appends an ``AccessDecisionRecorded`` row whose
    payload carries ``result=denied`` and the policy ``reason``
    (P11-US9 / P09-US6).
    """
    store = InMemoryAuditStore()
    decision = Decision.deny("no role grants this action")
    cmd = AccessDecisionInput(
        tenant_id=TENANT,
        actor_id=INTake_USER,
        actor_type="user",
        actor_roles=(ROLE_INTAKE.name,),
        action=Action.CASE_READ_SENSITIVE,
        resource=Resource(tenant_id=TENANT, kind="case", id=SUBJECT),
        decision=decision,
        correlation_id=f"{CORRELATION_BASE}-denied",
        causation_id=None,
        occurred_at=FIXED_NOW,
    )

    await record_decision(cmd, audit=store)
    store.commit()

    assert len(store.committed) == 1
    row = store.committed[0]
    assert row.event_type == DOMAIN_ACCESS_DECISION_RECORDED
    assert row.actor_id == INTake_USER
    assert row.subject_type == "case"
    assert row.subject_id == SUBJECT
    assert row.payload[ACCESS_KEY_RESULT] == RESULT_DENIED
    assert row.payload[ACCESS_KEY_REASON] == "no role grants this action"
    assert row.payload[ACCESS_KEY_ACTION] == "case.read_sensitive"


async def test_record_decision__allowed_writes_audit_row() -> None:
    """An allowed access appends an ``AccessDecisionRecorded`` row
    whose payload carries ``result=allowed`` and the granting role in
    the ``reason`` (P11-US9).
    """
    store = InMemoryAuditStore()
    decision = Decision.allow("role:supervisor")
    cmd = AccessDecisionInput(
        tenant_id=TENANT,
        actor_id=SUPERVISOR_USER,
        actor_type="user",
        actor_roles=(ROLE_SUPERVISOR.name,),
        action=Action.CASE_READ_SENSITIVE,
        resource=Resource(tenant_id=TENANT, kind="case", id=SUBJECT),
        decision=decision,
        correlation_id=f"{CORRELATION_BASE}-allowed",
        causation_id=None,
        occurred_at=FIXED_NOW,
    )

    await record_decision(cmd, audit=store)
    store.commit()

    assert len(store.committed) == 1
    row = store.committed[0]
    assert row.payload[ACCESS_KEY_RESULT] == RESULT_ALLOWED
    assert row.payload[ACCESS_KEY_REASON] == "role:supervisor"
    assert row.payload[ACCESS_KEY_ACTOR_ROLES] == [ROLE_SUPERVISOR.name]


async def test_record_decision__validates_actor_type() -> None:
    """An unknown ``actor_type`` is rejected before the store is touched."""
    store = InMemoryAuditStore()
    cmd = AccessDecisionInput(
        tenant_id=TENANT,
        actor_id=INTake_USER,
        actor_type="bot",
        actor_roles=("intake",),
        action=Action.CASE_READ,
        resource=Resource(tenant_id=TENANT, kind="case", id=SUBJECT),
        decision=Decision.allow("role:intake"),
        correlation_id=f"{CORRELATION_BASE}-bad-actor",
        causation_id=None,
        occurred_at=FIXED_NOW,
    )
    with pytest.raises(ValidationFailed):
        await record_decision(cmd, audit=store)
    assert store.committed == []


# ---------------------------------------------------------------------------
# Wiring: authorize() records every decision via the access_recorder
# ---------------------------------------------------------------------------


async def test_authorize__denied_decision_writes_access_decision_audit() -> None:
    """``authorize`` writes an ``AccessDecisionRecorded`` row when the
    policy denies, so the cross-tenant probe is visible in the report
    (P11-US2 / K1, P11-US9).
    """
    store = InMemoryAuditStore()
    recorder = _RecorderSpy()
    user = UserContext(
        user_id=INTake_USER,
        tenant_id=TENANT,
        display_name="intake",
        roles=(ROLE_INTAKE.name,),
        is_system=False,
    )
    # Cross-tenant: actor in TENANT targeting a resource in OTHER_TENANT
    resource = Resource(tenant_id=OTHER_TENANT, kind="case", id=SUBJECT)

    with pytest.raises(Forbidden):
        await authorize(
            Action.CASE_READ_SENSITIVE,
            user,
            resource,
            access_recorder=recorder,
        )
    store.commit()

    # The recorder saw the denied decision with the right action and
    # the cross-tenant reason ("cross-tenant access").
    assert len(recorder.calls) == 1
    action, _u, res, dec, _cid = recorder.calls[0]
    assert action == Action.CASE_READ_SENSITIVE
    assert res.kind == "case"
    assert dec.allowed is False
    assert dec.reason == "cross-tenant access"

    # And the persisted audit row carries the same decision.
    rows = await store.list_access_decisions(tenant_id=TENANT)
    assert len(rows) == 0  # nothing committed through the store path
    # Re-record through the real recorder so the store is exercised
    # via the application boundary.
    real_recorder = _build_real_recorder(store)
    with pytest.raises(Forbidden):
        await authorize(
            Action.CASE_READ_SENSITIVE,
            user,
            resource,
            access_recorder=real_recorder,
        )
    store.commit()
    rows = await store.list_access_decisions(tenant_id=TENANT)
    assert len(rows) == 1
    assert rows[0].payload[ACCESS_KEY_RESULT] == RESULT_DENIED
    assert rows[0].payload[ACCESS_KEY_REASON] == "cross-tenant access"
    assert rows[0].payload[ACCESS_KEY_ACTION] == "case.read_sensitive"


async def test_authorize__allowed_decision_writes_access_decision_audit() -> None:
    """``authorize`` writes an ``AccessDecisionRecorded`` row when the
    policy allows, so the security administrator can audit successful
    access as well (P09-US6 / P11-US9).
    """
    store = InMemoryAuditStore()
    real_recorder = _build_real_recorder(store)
    user = UserContext(
        user_id=SUPERVISOR_USER,
        tenant_id=TENANT,
        display_name="supervisor",
        roles=(ROLE_SUPERVISOR.name,),
        is_system=False,
    )
    resource = Resource(tenant_id=TENANT, kind="case", id=SUBJECT)

    await authorize(
        Action.CASE_READ_SENSITIVE,
        user,
        resource,
        access_recorder=real_recorder,
    )
    store.commit()

    rows = await store.list_access_decisions(tenant_id=TENANT)
    assert len(rows) == 1
    assert rows[0].payload[ACCESS_KEY_RESULT] == RESULT_ALLOWED
    assert rows[0].payload[ACCESS_KEY_REASON] == "role:supervisor"
    assert rows[0].actor_id == SUPERVISOR_USER


async def test_authorize__without_recorder_still_authorizes() -> None:
    """``authorize`` with no recorder refuses as usual and writes no
    audit row. The recorder is opt-in so unit tests and the CLI path
    can use the same function without standing up the audit store.
    """
    store = InMemoryAuditStore()
    user = UserContext(
        user_id=INTake_USER,
        tenant_id=TENANT,
        display_name="intake",
        roles=(ROLE_INTAKE.name,),
        is_system=False,
    )
    resource = Resource(tenant_id=OTHER_TENANT, kind="case", id=SUBJECT)
    with pytest.raises(Forbidden):
        await authorize(Action.CASE_READ_SENSITIVE, user, resource)
    store.commit()
    assert store.committed == []


# ---------------------------------------------------------------------------
# Report endpoint
# ---------------------------------------------------------------------------


async def test_view_report__returns_allowed_and_denied_decisions() -> None:
    """The report returns both outcomes in chronological order and
    carries the action, resource, actor, and reason on every row
    (P09-US6 / FM-045).
    """
    store = InMemoryAuditStore()
    real_recorder = _build_real_recorder(store)
    intake = UserContext(
        user_id=INTake_USER,
        tenant_id=TENANT,
        display_name="intake",
        roles=(ROLE_INTAKE.name,),
        is_system=False,
    )
    supervisor = UserContext(
        user_id=SUPERVISOR_USER,
        tenant_id=TENANT,
        display_name="supervisor",
        roles=(ROLE_SUPERVISOR.name,),
        is_system=False,
    )
    target = Resource(tenant_id=TENANT, kind="case", id=SUBJECT)

    # intake (denied) — lacks CASE_READ_SENSITIVE
    with pytest.raises(Forbidden):
        await authorize(
            Action.CASE_READ_SENSITIVE,
            intake,
            target,
            access_recorder=real_recorder,
        )
    # supervisor (allowed)
    await authorize(
        Action.CASE_READ_SENSITIVE,
        supervisor,
        target,
        access_recorder=real_recorder,
    )
    store.commit()

    page = await view_report(
        AccessDecisionQuery(
            tenant_id=TENANT,
            actor_id=SUPERVISOR_USER,
            actor_roles=(ROLE_SUPERVISOR.name,),
            limit=50,
            cursor=None,
        ),
        audit=store,
    )
    assert len(page.items) == 2

    results = {item.result for item in page.items}
    assert results == {RESULT_ALLOWED, RESULT_DENIED}

    for item in page.items:
        assert item.action == "case.read_sensitive"
        assert item.resource_kind == "case"
        assert item.resource_id == str(SUBJECT)
        assert item.reason


async def test_view_report__filters_by_actor() -> None:
    """The report narrows to a single actor when ``actor_filter`` is set.

    Two actors make decisions; only the supervisor's rows are returned.
    """
    store = InMemoryAuditStore()
    real_recorder = _build_real_recorder(store)
    intake = UserContext(
        user_id=INTake_USER,
        tenant_id=TENANT,
        display_name="intake",
        roles=(ROLE_INTAKE.name,),
        is_system=False,
    )
    supervisor = UserContext(
        user_id=SUPERVISOR_USER,
        tenant_id=TENANT,
        display_name="supervisor",
        roles=(ROLE_SUPERVISOR.name,),
        is_system=False,
    )
    target = Resource(tenant_id=TENANT, kind="case", id=SUBJECT)

    with pytest.raises(Forbidden):
        await authorize(
            Action.CASE_READ_SENSITIVE,
            intake,
            target,
            access_recorder=real_recorder,
        )
    await authorize(
        Action.CASE_READ_SENSITIVE,
        supervisor,
        target,
        access_recorder=real_recorder,
    )
    store.commit()

    page = await view_report(
        AccessDecisionQuery(
            tenant_id=TENANT,
            actor_id=SUPERVISOR_USER,
            actor_roles=(ROLE_SUPERVISOR.name,),
            actor_filter=SUPERVISOR_USER,
            limit=50,
            cursor=None,
        ),
        audit=store,
    )
    assert len(page.items) == 1
    assert page.items[0].actor_id == SUPERVISOR_USER
    assert page.items[0].result == RESULT_ALLOWED


async def test_view_report__filters_by_result() -> None:
    """The report narrows to ``denied`` when ``result=denied`` is set."""
    store = InMemoryAuditStore()
    real_recorder = _build_real_recorder(store)
    intake = UserContext(
        user_id=INTake_USER,
        tenant_id=TENANT,
        display_name="intake",
        roles=(ROLE_INTAKE.name,),
        is_system=False,
    )
    supervisor = UserContext(
        user_id=SUPERVISOR_USER,
        tenant_id=TENANT,
        display_name="supervisor",
        roles=(ROLE_SUPERVISOR.name,),
        is_system=False,
    )
    target = Resource(tenant_id=TENANT, kind="case", id=SUBJECT)

    with pytest.raises(Forbidden):
        await authorize(
            Action.CASE_READ_SENSITIVE,
            intake,
            target,
            access_recorder=real_recorder,
        )
    await authorize(
        Action.CASE_READ_SENSITIVE,
        supervisor,
        target,
        access_recorder=real_recorder,
    )
    store.commit()

    denied_page = await view_report(
        AccessDecisionQuery(
            tenant_id=TENANT,
            actor_id=SUPERVISOR_USER,
            actor_roles=(ROLE_SUPERVISOR.name,),
            result=RESULT_DENIED,
            limit=50,
            cursor=None,
        ),
        audit=store,
    )
    assert {item.result for item in denied_page.items} == {RESULT_DENIED}
    assert denied_page.items[0].actor_id == INTake_USER

    allowed_page = await view_report(
        AccessDecisionQuery(
            tenant_id=TENANT,
            actor_id=SUPERVISOR_USER,
            actor_roles=(ROLE_SUPERVISOR.name,),
            result=RESULT_ALLOWED,
            limit=50,
            cursor=None,
        ),
        audit=store,
    )
    assert {item.result for item in allowed_page.items} == {RESULT_ALLOWED}
    assert allowed_page.items[0].actor_id == SUPERVISOR_USER


async def test_view_report__rejects_unknown_result_filter() -> None:
    """An unknown ``result`` filter is rejected as ``ValidationFailed``."""
    store = InMemoryAuditStore()
    with pytest.raises(ValidationFailed):
        await view_report(
            AccessDecisionQuery(
                tenant_id=TENANT,
                actor_id=SUPERVISOR_USER,
                actor_roles=(ROLE_SUPERVISOR.name,),
                result="unknown",
                limit=50,
                cursor=None,
            ),
            audit=store,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _RecorderSpy:
    """In-process recorder that captures every call without persisting."""

    def __init__(self) -> None:
        self.calls: list[tuple[Action, UserContext, Resource, Decision, str]] = []

    async def __call__(
        self,
        action: Action,
        user: UserContext,
        resource: Resource,
        decision: Decision,
        correlation_id: str,
    ) -> None:
        self.calls.append((action, user, resource, decision, correlation_id))


def _build_real_recorder(store: InMemoryAuditStore) -> AccessDecisionRecorder:
    """Wire the application-layer recorder onto the in-memory store."""
    return build_recorder(audit=store, clock=_FixedClock())


__all__ = [
    "InMemoryAuditStore",
    "test_record_decision__denied_writes_audit_row_with_reason",
    "test_record_decision__allowed_writes_audit_row",
    "test_record_decision__validates_actor_type",
    "test_authorize__denied_decision_writes_access_decision_audit",
    "test_authorize__allowed_decision_writes_access_decision_audit",
    "test_authorize__without_recorder_still_authorizes",
    "test_view_report__returns_allowed_and_denied_decisions",
    "test_view_report__filters_by_actor",
    "test_view_report__filters_by_result",
    "test_view_report__rejects_unknown_result_filter",
]
