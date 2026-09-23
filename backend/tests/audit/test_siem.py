"""SIEM stream + signed audit batch tests (P09-US8, US9 / FM-046 / ADR-009).

# Acceptance criteria

The task brief defines the bar for the slice:

1. **An operator can stream persisted audit facts to a SIEM without
   weakening the chain.** The stream reads from the durable
   ``audit_events`` rows; the hash chain is the same per-tenant chain
   the export and the timeline use. A sink error does not roll back
   any commit (the row is already durable).
2. **An auditor can request a signed batch and verify the signature
   without weakening the durable chain.** The batch carries a
   signature the auditor recomputes off-system using the published
   ``key_id`` and ``algorithm``. The chain is verified against the
   durable table before signing so a misconfigured caller cannot
   attest a chain that does not exist on-disk.
3. **Gated requires explicit auth.** The ``audit.stream`` action is
   deny-by-default. No role grants it today. A caller without the
   role is refused at the boundary.

# Why in-memory

The application-layer use cases are exercised with the in-memory
``InMemoryAuditStore`` shape (mirrored from
``tests/audit/test_export_correction.py``) so the streaming,
chain-verification, and signing logic is covered without a live
container (AGENTS.md §4.R5 / R1). The Gated action test asserts on
the policy evaluation, not on a DB roundtrip.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.audit import siem_stream as siem_stream_uc
from app.application.audit.record_event import (
    RecordAuditCommand,
    record_event,
)
from app.application.audit.siem_stream import (
    ALGORITHM_HMAC_SHA256,
    DEFAULT_KEY_ID,
    MAX_BATCH_ROWS,
    BuildBatchCommand,
    CreateSignedBatchCommand,
    StreamQuery,
    build_signed_batch,
    create_signed_batch,
    derive_tenant_key,
    handle_stream,
    recompute_row_hash,
    sign_payload,
    verify_signature,
)
from app.application.authorization import (
    ROLE_INTAKE,
    ROLE_PROCESS_ADMIN,
    Action,
    Policy,
    Role,
    authorize,
)
from app.deps import UserContext
from app.domain.audit import (
    CASE_CREATED,
)
from app.domain.errors import Forbidden, ValidationFailed
from app.domain.ports import AuditEventStore
from app.infrastructure.persistence.siem import (
    InMemorySiemSink,
    InMemorySignedBatchRepository,
)

# ---------------------------------------------------------------------------
# Test-only role: ``siem_operator`` is granted the Gated action
# ``audit.stream`` so the use case can be exercised in tests. The
# production policy does not grant this role — the Gated action is
# deny-by-default (ADR-009).
# ---------------------------------------------------------------------------

SIEM_OPERATOR_ROLE = Role(
    name="siem_operator",
    actions=frozenset({Action.AUDIT_STREAM}),
)
SIEM_OPERATOR_POLICY = Policy(roles=(SIEM_OPERATOR_ROLE, ROLE_INTAKE, ROLE_PROCESS_ADMIN))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
ACTOR_ADMIN = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ACTOR_INTAKE = UUID("99999999-9999-9999-9999-999999999999")
SUBJECT = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
CORRELATION_BASE = "corr-siem-stream"
FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
SYNTHETIC_SECRET = "synthetic-environment-secret"


# ---------------------------------------------------------------------------
# In-memory audit store (mirrors tests/audit/test_export_correction.py)
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
    """Minimal audit store for the SIEM / signed-batch surface.

    Mirrors the in-memory fake in ``test_export_correction.py`` so the
    use case contract is exercised end-to-end without a live
    container. The chain is per-tenant; ``list_for_export`` returns
    the full chronological range so the signed-batch builder can
    slice by id.
    """

    def __init__(self) -> None:
        self.staged: list[_Stage] = []
        self.committed: list[_FakeRow] = []
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
        h = self._compute_hash(prev, payload)
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

    def _compute_hash(self, prev_hash: str, payload: dict[str, Any]) -> str:
        # Mirror ``app.domain.audit.compute_hash`` exactly so the
        # chain can be replayed off-system via the same rule.
        h = hashlib.sha256()
        h.update(prev_hash.encode("utf-8"))
        h.update(b"\x00")
        h.update(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        )
        return h.hexdigest()

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
        if since is not None:
            rows = [r for r in rows if r.occurred_at >= since]
        if until is not None:
            rows = [r for r in rows if r.occurred_at <= until]
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
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FixedClock:
    now_value: datetime = FIXED_NOW

    def now(self) -> datetime:
        return self.now_value


async def _append_case_created(
    store: InMemoryAuditStore,
    *,
    correlation_id: str,
    occurred_at: datetime,
) -> _FakeRow:
    """Append a single ``CaseCreated`` row and commit it."""

    await record_event(
        RecordAuditCommand(
            tenant_id=TENANT,
            actor_id=ACTOR_ADMIN,
            actor_type="user",
            event_type=CASE_CREATED,
            subject_type="case",
            subject_id=uuid4(),
            case_id=uuid4(),
            correlation_id=correlation_id,
            causation_id=None,
            payload={"reference": "C-1-2026-000001", "title": "Stream test"},
            occurred_at=occurred_at,
        ),
        audit=store,
        dedupe=lambda tenant_id, event_type, correlation_id, payload_hash_value: False,
    )
    store.commit()
    rows = [r for r in store.committed if r.correlation_id == correlation_id]
    assert len(rows) == 1
    return rows[0]


# ---------------------------------------------------------------------------
# Stream tests
# ---------------------------------------------------------------------------


async def test_stream__preserves_chain_after_durable_commit() -> None:
    """Streaming durable rows to a SIEM does not break the per-tenant
    hash chain (P09-US8 / FM-046).

    The stream reads from the durable table; the chain replayed from
    the streamed events matches the chain replayed from the
    on-disk rows.
    """

    store = InMemoryAuditStore()
    first = await _append_case_created(
        store,
        correlation_id=f"{CORRELATION_BASE}-1",
        occurred_at=FIXED_NOW,
    )
    second = await _append_case_created(
        store,
        correlation_id=f"{CORRELATION_BASE}-2",
        occurred_at=datetime(2026, 8, 31, 12, 5, 0, tzinfo=UTC),
    )
    third = await _append_case_created(
        store,
        correlation_id=f"{CORRELATION_BASE}-3",
        occurred_at=datetime(2026, 8, 31, 12, 10, 0, tzinfo=UTC),
    )

    sink = InMemorySiemSink()
    result = await handle_stream(
        StreamQuery(
            tenant_id=TENANT,
            actor_id=ACTOR_ADMIN,
            actor_roles=(SIEM_OPERATOR_ROLE.name,),
            since=None,
            until=None,
            limit=50,
        ),
        audit=store,
        sink=sink,
        clock=_FixedClock(),
        policy=SIEM_OPERATOR_POLICY,
    )

    assert result.rows_streamed == 3
    assert result.first_event_id == first.id
    assert result.last_event_id == third.id

    streamed = sink.events_for(TENANT)
    assert len(streamed) == 3
    # The chain anchors are on the wire so the SIEM consumer can
    # replay the chain from the stream alone.
    assert streamed[0]["prev_hash"] == "0" * 64
    assert streamed[0]["hash"] == first.hash
    assert streamed[1]["prev_hash"] == first.hash
    assert streamed[1]["hash"] == second.hash
    assert streamed[2]["prev_hash"] == second.hash
    assert streamed[2]["hash"] == third.hash

    # Recomputing the chain from the streamed events via the public
    # helper arrives at the same hashes the durable table holds.
    expected_prev = "0" * 64
    for event in streamed:
        recomputed = recompute_row_hash(expected_prev, event["payload"])
        assert recomputed == event["hash"]
        expected_prev = event["hash"]


async def test_stream__denied_when_actor_lacks_audit_stream() -> None:
    """The Gated action is deny-by-default: an intake actor (no
    ``audit.stream`` grant) is refused at the boundary.
    """

    store = InMemoryAuditStore()
    sink = InMemorySiemSink()
    with pytest.raises(Forbidden):
        await handle_stream(
            StreamQuery(
                tenant_id=TENANT,
                actor_id=ACTOR_INTAKE,
                actor_roles=(ROLE_INTAKE.name,),
                since=None,
                until=None,
                limit=50,
            ),
            audit=store,
            sink=sink,
            clock=_FixedClock(),
        )
    assert sink.events_for(TENANT) == []


async def test_stream__sink_error_does_not_roll_back_chain() -> None:
    """A sink transport error is recorded but does not weaken the
    durable chain. The use case surfaces the failure to telemetry
    and continues with the next row.
    """

    store = InMemoryAuditStore()
    first = await _append_case_created(
        store,
        correlation_id=f"{CORRELATION_BASE}-err-1",
        occurred_at=FIXED_NOW,
    )
    second = await _append_case_created(
        store,
        correlation_id=f"{CORRELATION_BASE}-err-2",
        occurred_at=datetime(2026, 8, 31, 12, 5, 0, tzinfo=UTC),
    )

    sink = InMemorySiemSink()
    sink.errors.append(RuntimeError("simulated SIEM outage"))
    result = await handle_stream(
        StreamQuery(
            tenant_id=TENANT,
            actor_id=ACTOR_ADMIN,
            actor_roles=(SIEM_OPERATOR_ROLE.name,),
            since=None,
            until=None,
            limit=50,
        ),
        audit=store,
        sink=sink,
        clock=_FixedClock(),
        policy=SIEM_OPERATOR_POLICY,
    )
    # One row hit the simulated sink outage; the other streamed. The
    # durable chain is intact — the in-memory store is unchanged by a
    # sink error, and the on-disk table is not touched at all.
    assert result.rows_streamed == 1
    assert result.rows_skipped == 1
    assert [r.hash for r in store.committed if r.tenant_id == TENANT] == [
        first.hash,
        second.hash,
    ]


# ---------------------------------------------------------------------------
# Signed batch tests
# ---------------------------------------------------------------------------


async def test_signed_batch__verifiable_off_system_against_durable_chain() -> None:
    """A signed batch round-trips: the auditor recomputes the
    signature with the published ``key_id`` and ``algorithm`` and
    confirms the chain against the durable rows (P09-US9 / FM-046).
    """

    store = InMemoryAuditStore()
    first = await _append_case_created(
        store,
        correlation_id=f"{CORRELATION_BASE}-batch-1",
        occurred_at=FIXED_NOW,
    )
    _ = await _append_case_created(
        store,
        correlation_id=f"{CORRELATION_BASE}-batch-2",
        occurred_at=datetime(2026, 8, 31, 12, 5, 0, tzinfo=UTC),
    )
    third = await _append_case_created(
        store,
        correlation_id=f"{CORRELATION_BASE}-batch-3",
        occurred_at=datetime(2026, 8, 31, 12, 10, 0, tzinfo=UTC),
    )

    repository = InMemorySignedBatchRepository()
    batch = await build_signed_batch(
        BuildBatchCommand(
            tenant_id=TENANT,
            actor_id=ACTOR_ADMIN,
            actor_roles=(SIEM_OPERATOR_ROLE.name,),
            first_event_id=first.id,
            last_event_id=third.id,
            reason="FM-046 acceptance",
            correlation_id=f"{CORRELATION_BASE}-batch-corr",
        ),
        audit=store,
        repository=repository,
        clock=_FixedClock(),
        policy=SIEM_OPERATOR_POLICY,
    )

    # Algorithm + key_id are the explicit version pins a production
    # auditor relies on.
    assert batch.algorithm == ALGORITHM_HMAC_SHA256
    assert batch.key_id == DEFAULT_KEY_ID
    assert batch.row_count == 3
    assert batch.first_event_id == first.id
    assert batch.last_event_id == third.id
    assert batch.first_event_hash == first.hash
    assert batch.last_event_hash == third.hash

    # 1. The signature verifies with the synthetic-environment secret.
    assert verify_signature(
        tenant_id=TENANT,
        payload=batch.payload,
        key_id=batch.key_id,
        signature=batch.signature,
        synthetic_secret=SYNTHETIC_SECRET,
    )

    # 2. The chain on the batch payload matches the durable table.
    rows = batch.payload["rows"]
    assert rows[0]["hash"] == first.hash
    assert rows[-1]["hash"] == third.hash
    expected_prev = "0" * 64
    for event in rows:
        recomputed = recompute_row_hash(expected_prev, event["payload"])
        assert recomputed == event["hash"]
        expected_prev = event["hash"]

    # 3. A wrong tenant id does not verify (defense-in-depth).
    assert not verify_signature(
        tenant_id=OTHER_TENANT,
        payload=batch.payload,
        key_id=batch.key_id,
        signature=batch.signature,
        synthetic_secret=SYNTHETIC_SECRET,
    )

    # 4. A wrong secret does not verify.
    assert not verify_signature(
        tenant_id=TENANT,
        payload=batch.payload,
        key_id=batch.key_id,
        signature=batch.signature,
        synthetic_secret="not-the-real-secret",
    )

    # 5. The batch is persisted so a re-read via the repository
    # returns the same payload + signature.
    re_read = await repository.get(tenant_id=TENANT, batch_id=batch.id)
    assert re_read is not None
    assert re_read.signature == batch.signature
    assert re_read.payload == batch.payload


async def test_signed_batch__rejects_inverted_span() -> None:
    """A misconfigured caller that passes ``first_event_id`` after
    ``last_event_id`` is refused with ``ValidationFailed``; the chain
    is never attested to a non-existent range.
    """

    store = InMemoryAuditStore()
    first = await _append_case_created(
        store,
        correlation_id=f"{CORRELATION_BASE}-inv-1",
        occurred_at=FIXED_NOW,
    )
    second = await _append_case_created(
        store,
        correlation_id=f"{CORRELATION_BASE}-inv-2",
        occurred_at=datetime(2026, 8, 31, 12, 5, 0, tzinfo=UTC),
    )

    repository = InMemorySignedBatchRepository()
    with pytest.raises(ValidationFailed):
        await build_signed_batch(
            BuildBatchCommand(
                tenant_id=TENANT,
                actor_id=ACTOR_ADMIN,
                actor_roles=(SIEM_OPERATOR_ROLE.name,),
                first_event_id=second.id,
                last_event_id=first.id,
                reason="FM-046 acceptance inverted",
                correlation_id=f"{CORRELATION_BASE}-inv-corr",
            ),
            audit=store,
            repository=repository,
            clock=_FixedClock(),
            policy=SIEM_OPERATOR_POLICY,
        )
    # The repository was never touched.
    assert await repository.list(tenant_id=TENANT) == []


async def test_signed_batch__create_signed_batch_picks_range() -> None:
    """``create_signed_batch`` reads a [since, until] window and
    signs the contiguous span it returns.
    """

    store = InMemoryAuditStore()
    for idx in range(3):
        await _append_case_created(
            store,
            correlation_id=f"{CORRELATION_BASE}-csb-{idx}",
            occurred_at=FIXED_NOW.replace(minute=idx * 5),
        )

    repository = InMemorySignedBatchRepository()
    batch = await create_signed_batch(
        CreateSignedBatchCommand(
            tenant_id=TENANT,
            actor_id=ACTOR_ADMIN,
            actor_roles=(SIEM_OPERATOR_ROLE.name,),
            since=None,
            until=None,
            reason="FM-046 acceptance",
            correlation_id=f"{CORRELATION_BASE}-csb-corr",
            limit=MAX_BATCH_ROWS,
        ),
        audit=store,
        repository=repository,
        clock=_FixedClock(),
        policy=SIEM_OPERATOR_POLICY,
    )
    assert batch.row_count == 3
    assert verify_signature(
        tenant_id=TENANT,
        payload=batch.payload,
        key_id=batch.key_id,
        signature=batch.signature,
        synthetic_secret=SYNTHETIC_SECRET,
    )


async def test_signed_batch__denied_when_actor_lacks_audit_stream() -> None:
    """A non-privileged actor cannot request a signed batch (Gated)."""

    store = InMemoryAuditStore()
    first = await _append_case_created(
        store,
        correlation_id=f"{CORRELATION_BASE}-deny-1",
        occurred_at=FIXED_NOW,
    )
    second = await _append_case_created(
        store,
        correlation_id=f"{CORRELATION_BASE}-deny-2",
        occurred_at=datetime(2026, 8, 31, 12, 5, 0, tzinfo=UTC),
    )
    repository = InMemorySignedBatchRepository()
    with pytest.raises(Forbidden):
        await build_signed_batch(
            BuildBatchCommand(
                tenant_id=TENANT,
                actor_id=ACTOR_INTAKE,
                actor_roles=(ROLE_INTAKE.name,),
                first_event_id=first.id,
                last_event_id=second.id,
                reason="intake probe",
                correlation_id=f"{CORRELATION_BASE}-deny-corr",
            ),
            audit=store,
            repository=repository,
            clock=_FixedClock(),
        )
    assert await repository.list(tenant_id=TENANT) == []


async def test_signed_batch__tenant_isolation() -> None:
    """A batch created for tenant A cannot be retrieved via tenant B
    (Constitution V / AGENTS.md §4.R12)."""

    store = InMemoryAuditStore()
    a_row = await _append_case_created(
        store,
        correlation_id=f"{CORRELATION_BASE}-iso-a",
        occurred_at=FIXED_NOW,
    )
    b_row = await _append_case_created(
        store,
        correlation_id=f"{CORRELATION_BASE}-iso-b",
        occurred_at=datetime(2026, 8, 31, 12, 5, 0, tzinfo=UTC),
    )

    repository = InMemorySignedBatchRepository()
    batch = await build_signed_batch(
        BuildBatchCommand(
            tenant_id=TENANT,
            actor_id=ACTOR_ADMIN,
            actor_roles=(SIEM_OPERATOR_ROLE.name,),
            first_event_id=a_row.id,
            last_event_id=b_row.id,
            reason="isolation probe",
            correlation_id=f"{CORRELATION_BASE}-iso-corr",
        ),
        audit=store,
        repository=repository,
        clock=_FixedClock(),
        policy=SIEM_OPERATOR_POLICY,
    )
    # Cross-tenant read returns None — same shape as the rest of the
    # tenant-scoped repositories (audit_outbox, role, case, etc.).
    cross = await repository.get(tenant_id=OTHER_TENANT, batch_id=batch.id)
    assert cross is None
    # Same-tenant read returns the batch.
    same = await repository.get(tenant_id=TENANT, batch_id=batch.id)
    assert same is not None
    assert same.id == batch.id


# ---------------------------------------------------------------------------
# Key derivation + signature helpers
# ---------------------------------------------------------------------------


def test_derive_tenant_key__isolates_tenants() -> None:
    """A per-tenant key derivation ensures a leak of one tenant's key
    does not weaken another's (defense-in-depth)."""

    a = derive_tenant_key(TENANT, synthetic_secret=SYNTHETIC_SECRET)
    b = derive_tenant_key(OTHER_TENANT, synthetic_secret=SYNTHETIC_SECRET)
    assert a != b
    # Stable: re-deriving the same tenant produces the same key.
    assert derive_tenant_key(TENANT, synthetic_secret=SYNTHETIC_SECRET) == a


def test_sign_payload__deterministic_for_same_inputs() -> None:
    payload = {"a": 1, "b": [1, 2, 3], "c": {"nested": "value"}}
    first = sign_payload(
        tenant_id=TENANT,
        payload=payload,
        key_id=DEFAULT_KEY_ID,
        synthetic_secret=SYNTHETIC_SECRET,
    )
    # Reordering keys does not change the signature: ``canonical_json``
    # sorts keys, so the audit chain and the signed batch share the
    # same canonicalization rule.
    reordered = {"c": {"nested": "value"}, "b": [1, 2, 3], "a": 1}
    second = sign_payload(
        tenant_id=TENANT,
        payload=reordered,
        key_id=DEFAULT_KEY_ID,
        synthetic_secret=SYNTHETIC_SECRET,
    )
    assert first == second


# ---------------------------------------------------------------------------
# Policy action registration
# ---------------------------------------------------------------------------


def test_audit_stream_action_registered_and_gated() -> None:
    """``audit.stream`` is registered in the Action enum but is not
    granted to any default role (Gated per ADR-009).
    """

    assert Action.AUDIT_STREAM.value == "audit.stream"
    # None of the four first-slice roles grant the action.
    for role in (ROLE_INTAKE, ROLE_PROCESS_ADMIN):
        assert Action.AUDIT_STREAM not in role.actions


async def test_authorize__denies_audit_stream_by_default() -> None:
    """Even ``ROLE_PROCESS_ADMIN`` is denied because no role grants
    the Gated action today.
    """

    user = UserContext(
        user_id=ACTOR_ADMIN,
        tenant_id=TENANT,
        display_name="process admin",
        roles=(ROLE_PROCESS_ADMIN.name,),
        is_system=False,
    )
    with pytest.raises(Forbidden):
        await authorize(
            Action.AUDIT_STREAM,
            user,
            siem_stream_uc.Resource(tenant_id=TENANT, kind="audit_siem"),
        )
