"""Chain-of-custody acceptance tests (F-5; P09 / FM-044 + FM-045).

Acceptance criteria from the task:

1. Original events never mutate — a correction overlays as an
   annotation; the original payload/hash/position are byte-identical.
2. Exports record actor/policy/time/audit — the envelope carries
   ``actor_id``, ``policy_action="audit.export"``, ``generated_at``,
   and the chain verdict; the run appends one ``AuditExported`` row.
3. Unauthorized data is omitted without metadata leakage — payloads
   outside the timeline allowlist render as ``"***"``; access
   decisions for other cases are omitted entirely (not counted);
   cross-tenant ids surface as ``NotFound``; actors without
   ``audit.export`` are refused before any read.
4. Output is deterministic — two runs over the same chain produce
   byte-identical canonical JSON for the same clock.

Why in-memory fakes: the use case is exercised without a live
container (AGENTS.md R1). The fake keeps a per-tenant hash chain so
per-row self-hash verification runs the real ``compute_hash`` path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.audit.chain_of_custody import (
    POLICY_ACTION,
    ChainOfCustodyQuery,
)
from app.application.audit.chain_of_custody import (
    handle as custody_handle,
)
from app.application.authorization import ROLE_INTAKE, ROLE_PROCESS_ADMIN
from app.domain.audit import (
    ACCESS_DECISION_RECORDED,
    AUDIT_CORRECTION_RECORDED,
    AUDIT_EXPORTED,
    CASE_CREATED,
    compute_hash,
)
from app.domain.errors import Forbidden, NotFound, ValidationFailed
from app.domain.ports import AuditEventStore

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ACTOR_ADMIN = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ACTOR_OTHER = UUID("99999999-9999-9999-9999-999999999999")
CASE = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
OTHER_CASE = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
SUBJECT = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
FIXED_NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes
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
class _FakeCase:
    id: UUID
    tenant_id: UUID


class InMemoryAuditStore(AuditEventStore):
    """Immediate-commit audit store with a real per-tenant hash chain."""

    def __init__(self) -> None:
        self.committed: list[_FakeRow] = []
        self._seen: set[tuple[UUID, str, str, str]] = set()
        self._id_seq = 0

    def _last_hash(self, tenant_id: UUID) -> str:
        for row in reversed(self.committed):
            if row.tenant_id == tenant_id:
                return row.hash
        return "0" * 64

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
        self._id_seq += 1
        self.committed.append(
            _FakeRow(
                id=UUID(int=self._id_seq),
                tenant_id=tenant_id,
                event_type=event_type,
                actor_id=actor_id,
                actor_type=actor_type,
                subject_type=subject_type,
                subject_id=subject_id,
                case_id=case_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                payload=dict(payload),
                occurred_at=occurred_at,
                prev_hash=prev,
                hash=compute_hash(prev, payload),
            )
        )

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
        rows = [r for r in self.committed if r.tenant_id == tenant_id and r.case_id == case_id]
        if event_types:
            rows = [r for r in rows if r.event_type in event_types]
        rows.sort(key=lambda r: (r.occurred_at, r.id))
        return rows[:limit]

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
        rows = [r for r in self.committed if r.tenant_id == tenant_id]
        if case_id is not None:
            rows = [r for r in rows if r.case_id == case_id]
        rows.sort(key=lambda r: (r.occurred_at, r.id))
        return rows[:limit]

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
            if r.tenant_id == tenant_id and r.event_type == ACCESS_DECISION_RECORDED
        ]
        if actor_id is not None:
            rows = [r for r in rows if r.actor_id == actor_id]
        rows.sort(key=lambda r: (r.occurred_at, r.id))
        return rows[:limit]

    def dedupe(self) -> Any:
        def _d(
            tenant_id: UUID,
            event_type: str,
            correlation_id: str,
            payload_digest: str,
        ) -> bool:
            key = (tenant_id, event_type, correlation_id, payload_digest)
            if key in self._seen:
                return True
            self._seen.add(key)
            return False

        return _d


class InMemoryCaseRepository:
    """Minimal ``CaseRepository`` exposing ``get`` with tenant scoping."""

    def __init__(self, cases: list[_FakeCase] | None = None) -> None:
        self._cases: list[_FakeCase] = list(cases or [])

    async def get(self, tenant_id: UUID, case_id: UUID) -> _FakeCase | None:
        for case in self._cases:
            if case.tenant_id == tenant_id and case.id == case_id:
                return case
        return None


@dataclass
class _Clock:
    now_value: datetime = FIXED_NOW

    def now(self) -> datetime:
        return self.now_value


def _query(**overrides: Any) -> ChainOfCustodyQuery:
    base: dict[str, Any] = {
        "tenant_id": TENANT_A,
        "actor_id": ACTOR_ADMIN,
        "actor_roles": (ROLE_PROCESS_ADMIN.name,),
        "case_id": CASE,
        "reason": "custody-review-2026-09",
    }
    base.update(overrides)
    return ChainOfCustodyQuery(**base)


async def _seed_chain(store: InMemoryAuditStore) -> _FakeRow:
    """Seed two base rows; return the first (correction target)."""
    # given: a case with two audited writes, one carrying a secret key
    await store.append(
        tenant_id=TENANT_A,
        event_type=CASE_CREATED,
        actor_id=ACTOR_ADMIN,
        actor_type="user",
        subject_type="case",
        subject_id=SUBJECT,
        case_id=CASE,
        correlation_id="corr-seed-1",
        causation_id=None,
        payload={"case_reference": "C-ACME-2026-000001", "ssn": "123-45-6789"},
        occurred_at=FIXED_NOW,
    )
    await store.append(
        tenant_id=TENANT_A,
        event_type=CASE_CREATED,
        actor_id=ACTOR_ADMIN,
        actor_type="user",
        subject_type="case",
        subject_id=SUBJECT,
        case_id=CASE,
        correlation_id="corr-seed-2",
        causation_id=None,
        payload={"case_reference": "C-ACME-2026-000001"},
        occurred_at=FIXED_NOW,
    )
    return store.committed[0]


async def _seed_correction(store: InMemoryAuditStore, original_id: UUID) -> None:
    await store.append(
        tenant_id=TENANT_A,
        event_type=AUDIT_CORRECTION_RECORDED,
        actor_id=ACTOR_ADMIN,
        actor_type="user",
        subject_type="case",
        subject_id=SUBJECT,
        case_id=CASE,
        correlation_id="corr-correction-1",
        causation_id=str(original_id),
        payload={
            "original_event_id": str(original_id),
            "reason": "reference typo",
            "corrected_summary": "reference is C-ACME-2026-000001",
        },
        occurred_at=FIXED_NOW,
    )


async def _seed_access_decisions(store: InMemoryAuditStore) -> None:
    # given: one decision for this case + one for an unrelated case
    await store.append(
        tenant_id=TENANT_A,
        event_type=ACCESS_DECISION_RECORDED,
        actor_id=ACTOR_OTHER,
        actor_type="user",
        subject_type="case",
        subject_id=CASE,
        case_id=None,
        correlation_id="corr-access-1",
        causation_id=None,
        payload={
            "action": "case.read",
            "resource_kind": "case",
            "resource_id": str(CASE),
            "result": "denied",
            "reason": "forbidden",
            "actor_roles": ["intake"],
        },
        occurred_at=FIXED_NOW,
    )
    await store.append(
        tenant_id=TENANT_A,
        event_type=ACCESS_DECISION_RECORDED,
        actor_id=ACTOR_OTHER,
        actor_type="user",
        subject_type="case",
        subject_id=OTHER_CASE,
        case_id=None,
        correlation_id="corr-access-2",
        causation_id=None,
        payload={
            "action": "case.read",
            "resource_kind": "case",
            "resource_id": str(OTHER_CASE),
            "result": "allowed",
            "reason": "supervisor",
            "actor_roles": ["supervisor"],
        },
        occurred_at=FIXED_NOW,
    )


def _case_repo() -> InMemoryCaseRepository:
    return InMemoryCaseRepository([_FakeCase(id=CASE, tenant_id=TENANT_A)])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_chain_of_custody__authorized_export__returns_full_packet() -> None:
    """An authorized export returns chain + overlay + scoped evidence."""
    # given
    store = InMemoryAuditStore()
    original = await _seed_chain(store)
    await _seed_correction(store, original.id)
    await _seed_access_decisions(store)
    # when
    report = await custody_handle(
        _query(),
        audit=store,  # type: ignore[arg-type]
        case_repo=_case_repo(),  # type: ignore[arg-type]
        clock=_Clock(),
        dedupe=store.dedupe(),
        correlation_id="corr-custody-1",
    )
    # then: envelope records actor/policy/time/audit
    assert report.tenant_id == str(TENANT_A)
    assert report.case_id == str(CASE)
    assert report.actor_id == str(ACTOR_ADMIN)
    assert report.policy_action == POLICY_ACTION == "audit.export"
    assert report.generated_at == FIXED_NOW.isoformat()
    assert report.chain.ok is True
    assert report.chain.rows_checked == 3
    # then: base rows carry anchors; secret key is redacted without leakage
    assert len(report.items) == 2
    first = report.items[0]
    assert first.payload["case_reference"] == "C-ACME-2026-000001"
    assert first.payload["ssn"] == "***"
    assert first.redacted is True
    assert len(first.hash) == 64
    assert len(first.prev_hash) == 64
    assert report.redacted_rows == 1
    # then: the correction overlays as an annotation on the original
    assert len(first.annotations) == 1
    assert first.annotations[0].original_event_id == str(original.id)
    assert first.annotations[0].reason == "reference typo"
    # then: access evidence is scoped to this case only — no leakage
    assert len(report.access_decisions) == 1
    assert report.access_decisions[0].result == "denied"
    assert report.access_decisions[0].action == "case.read"
    # then: the export itself is an audit event
    exported = [r for r in store.committed if r.event_type == AUDIT_EXPORTED]
    assert len(exported) == 1
    assert exported[0].payload["case_id"] == str(CASE)
    assert exported[0].payload["row_count"] == 2
    assert exported[0].payload["correction_count"] == 1
    assert exported[0].payload["access_decision_count"] == 1
    assert exported[0].payload["chain_ok"] is True


async def test_chain_of_custody__original_event__never_mutates_on_overlay() -> None:
    """Overlaying a correction leaves the original row byte-identical."""
    # given
    store = InMemoryAuditStore()
    original = await _seed_chain(store)
    before_payload = dict(original.payload)
    before_hash = original.hash
    before_prev = original.prev_hash
    await _seed_correction(store, original.id)
    # when
    await custody_handle(
        _query(),
        audit=store,  # type: ignore[arg-type]
        case_repo=_case_repo(),  # type: ignore[arg-type]
        clock=_Clock(),
        dedupe=store.dedupe(),
    )
    # then
    again = await store.get_event(tenant_id=TENANT_A, event_id=original.id)
    assert again is not None
    assert again.payload == before_payload
    assert again.hash == before_hash
    assert again.prev_hash == before_prev


async def test_chain_of_custody__repeated_runs__produce_identical_canonical_json() -> None:
    """Two runs over the same chain render byte-identical canonical JSON."""
    # given
    store = InMemoryAuditStore()
    original = await _seed_chain(store)
    await _seed_correction(store, original.id)
    await _seed_access_decisions(store)
    kwargs: dict[str, Any] = {
        "audit": store,
        "case_repo": _case_repo(),
        "clock": _Clock(),
    }
    # when
    first = await custody_handle(_query(), **kwargs)  # type: ignore[arg-type]
    second = await custody_handle(_query(), **kwargs)  # type: ignore[arg-type]
    # then
    assert first.to_canonical_json() == second.to_canonical_json()


async def test_chain_of_custody__unauthorized_actor__raises_forbidden() -> None:
    """An actor without ``audit.export`` is refused before any read."""
    # given
    store = InMemoryAuditStore()
    await _seed_chain(store)
    before = len(store.committed)
    # when / then
    with pytest.raises(Forbidden):
        await custody_handle(
            _query(actor_roles=(ROLE_INTAKE.name,)),
            audit=store,  # type: ignore[arg-type]
            case_repo=_case_repo(),  # type: ignore[arg-type]
            clock=_Clock(),
            dedupe=store.dedupe(),
        )
    assert len(store.committed) == before


async def test_chain_of_custody__cross_tenant_case__raises_not_found() -> None:
    """A case in another tenant surfaces as ``NotFound`` (no existence leak)."""
    # given
    store = InMemoryAuditStore()
    await _seed_chain(store)
    # when / then
    with pytest.raises(NotFound):
        await custody_handle(
            _query(tenant_id=TENANT_B),
            audit=store,  # type: ignore[arg-type]
            case_repo=_case_repo(),  # type: ignore[arg-type]
            clock=_Clock(),
            dedupe=store.dedupe(),
        )


async def test_chain_of_custody__missing_case__raises_not_found() -> None:
    """An unknown case id surfaces as ``NotFound``."""
    # given
    store = InMemoryAuditStore()
    # when / then
    with pytest.raises(NotFound):
        await custody_handle(
            _query(case_id=uuid4()),
            audit=store,  # type: ignore[arg-type]
            case_repo=_case_repo(),  # type: ignore[arg-type]
            clock=_Clock(),
            dedupe=store.dedupe(),
        )


async def test_chain_of_custody__blank_reason__raises_validation_failed() -> None:
    """A blank reason is rejected so the audit marker stays honest."""
    # given
    store = InMemoryAuditStore()
    # when / then
    with pytest.raises(ValidationFailed) as excinfo:
        await custody_handle(
            _query(reason="   "),
            audit=store,  # type: ignore[arg-type]
            case_repo=_case_repo(),  # type: ignore[arg-type]
            clock=_Clock(),
            dedupe=store.dedupe(),
        )
    assert excinfo.value.field == "reason"
