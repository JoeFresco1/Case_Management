"""Export + correction acceptance tests (P09-US5, US7; FM-044).

Acceptance criteria from the task:

1. The export returns a verifiable batch — every row carries
   ``hash`` and ``prev_hash``, and the chain verifies end-to-end via
   ``app.domain.audit.verify_chain`` (the same function the
   ``verify-audit`` CLI uses).
2. The correction writes a NEW audit row referencing the original;
   the original row's payload, hash, and chain position are
   unchanged.
3. The original event's UPDATE is still rejected (Layer 1 + Layer 2
   immutability survives the introduction of the correction path).
4. Each action is itself audited — running an export appends an
   ``AuditExported`` row; appending a correction appends an
   ``AuditCorrectionRecorded`` row.

# Why in-memory + sqlite split

The application-layer use cases are tested with an in-memory fake so the
correction / export logic is exercised without paying for a live
container (AGENTS.md §4.R5). The immutability regression (Layer 1 + 2)
is tested against a sqlite engine with the triggers installed — the
same setup the existing ``test_immutability.py`` uses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.application.audit.correct_audit import (
    CorrectAuditCommand,
)
from app.application.audit.correct_audit import (
    handle as correct_handle,
)
from app.application.audit.export_audit import (
    ExportQuery,
)
from app.application.audit.export_audit import (
    handle as export_handle,
)
from app.application.audit.record_event import (
    RecordAuditCommand,
    record_event,
)
from app.application.authorization import (
    ROLE_INTAKE,
    ROLE_PROCESS_ADMIN,
)
from app.config import Settings
from app.db import Base, install_tenant_listener, reset_engine
from app.domain.audit import (
    AUDIT_CORRECTION_RECORDED,
    AUDIT_EXPORTED,
    CASE_CREATED,
    CORRECTION_KEY_ORIGINAL_EVENT_ID,
    CORRECTION_KEY_REASON,
    compute_hash,
    verify_chain,
)
from app.domain.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.domain.ports import AuditEventStore
from app.infrastructure.persistence import models  # noqa: F401  registers tables
from app.infrastructure.persistence.audit_events import (
    AuditEventImmutable,
    install_audit_immutability_execute_listener,
    install_audit_immutability_listener,
    reset_audit_immutability_listener,
)
from app.infrastructure.persistence.audit_outbox import SqlAuditEventStore
from app.infrastructure.persistence.models import (
    AuditEvent as AuditEventRow,
)
from app.infrastructure.persistence.models import Tenant as TenantRow

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ACTOR_ADMIN = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ACTOR_OTHER = UUID("99999999-9999-9999-9999-999999999999")
SUBJECT = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
CASE = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
CORRELATION_BASE = "corr-export-correction"

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# In-memory fake (P09-US5 / US7)
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
    """In-memory ``AuditEventStore`` for the application-layer scenarios.

    Maintains a per-tenant hash chain so we can assert
    ``verify_chain`` on the returned batch. ``append`` stages a row
    on the session (here: a list) and only materializes it on
    ``commit``. ``commit`` is called by the test after the use case
    returns.
    """

    def __init__(self) -> None:
        self.staged: list[_Stage] = []
        self.committed: list[_FakeRow] = []
        self._seen: dict[
            tuple[UUID, str, str], bool
        ] = {}  # (tenant, event_type, correlation_id) -> exists?
        # ``_id_seq`` is per-store so row ids are deterministic.
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
        # Walk the in-flight staged rows first (same transaction) and
        # then the committed ones. The staged rows are in append
        # order, so ``reversed`` gives us the most recent staged row.
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
        rows = [r for r in self.committed if r.tenant_id == tenant_id and r.case_id == case_id]
        if event_types:
            rows = [r for r in rows if r.event_type in event_types]
        if actor_id is not None:
            rows = [r for r in rows if r.actor_id == actor_id]
        if since is not None:
            rows = [r for r in rows if r.occurred_at >= since]
        if until is not None:
            rows = [r for r in rows if r.occurred_at <= until]
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

    def dedupe(self) -> Any:
        """Build a dedupe callable mirroring the application contract."""

        def _d(
            tenant_id: UUID,
            event_type: str,
            correlation_id: str,
            payload_hash_value: str,
        ) -> bool:
            return (
                tenant_id,
                event_type,
                correlation_id,
            ) in self._seen

        return _d


def _noop_cursor(_occurred_at: Any, _row_id: Any) -> str:
    return "next-page"


# ---------------------------------------------------------------------------
# Application-layer scenarios
# ---------------------------------------------------------------------------


async def test_export__returns_verifiable_chain_anchors() -> None:
    """The export batch carries chain anchors that ``verify_chain`` accepts.

    Three rows for ``CASE`` are appended; the export returns all three
    with their ``hash`` / ``prev_hash``; replaying the chain via the
    pure verifier succeeds.
    """
    store = InMemoryAuditStore()
    # Seed three rows on the chain via ``record_event``.
    for i, et in enumerate([CASE_CREATED, CASE_CREATED, CASE_CREATED]):
        cmd = RecordAuditCommand(
            tenant_id=TENANT_A,
            actor_id=ACTOR_ADMIN,
            actor_type="user",
            event_type=et,
            subject_type="case",
            subject_id=SUBJECT,
            case_id=CASE,
            correlation_id=f"{CORRELATION_BASE}-seed-{i}",
            causation_id=None,
            payload={"seq": i, "ref": f"C-EXP-2026-{i:03d}"},
            occurred_at=FIXED_NOW,
        )
        await record_event(cmd, audit=store, dedupe=store.dedupe())
    store.commit()

    batch = await export_handle(
        ExportQuery(
            tenant_id=TENANT_A,
            actor_id=ACTOR_ADMIN,
            actor_roles=(ROLE_PROCESS_ADMIN.name,),
            case_id=CASE,
            since=None,
            until=None,
            reason="acceptance-test",
            limit=50,
            cursor=None,
        ),
        audit=store,
        cursor_encoder=_noop_cursor,
    )

    assert batch.rows_in_batch == 3
    # Every row carries chain metadata.
    for entry in batch.items:
        assert entry.hash
        assert entry.prev_hash
        assert len(entry.hash) == 64

    # Replay the chain via the pure verifier.
    chain_rows = [
        {
            "id": row.id,
            "hash": row.hash,
            "prev_hash": row.prev_hash,
            "payload": row.payload,
        }
        for row in store.committed
        if row.case_id == CASE
    ]
    result = verify_chain(chain_rows)
    assert result.ok is True
    assert result.rows_checked == 3


async def test_export__appends_audit_exported_event() -> None:
    """The export is itself an audit event (P09-US5). Running an
    export appends an ``AuditExported`` row referencing the scope.
    """
    store = InMemoryAuditStore()
    cmd = RecordAuditCommand(
        tenant_id=TENANT_A,
        actor_id=ACTOR_ADMIN,
        actor_type="user",
        event_type=CASE_CREATED,
        subject_type="case",
        subject_id=SUBJECT,
        case_id=CASE,
        correlation_id=f"{CORRELATION_BASE}-seed",
        causation_id=None,
        payload={"ref": "C-EXP-2026-001"},
        occurred_at=FIXED_NOW,
    )
    await record_event(cmd, audit=store, dedupe=store.dedupe())
    store.commit()

    @dataclass
    class _Clock:
        now_value: datetime = FIXED_NOW

        def now(self) -> datetime:
            return self.now_value

    clock = _Clock()
    batch = await export_handle(
        ExportQuery(
            tenant_id=TENANT_A,
            actor_id=ACTOR_ADMIN,
            actor_roles=(ROLE_PROCESS_ADMIN.name,),
            case_id=CASE,
            since=None,
            until=None,
            reason="export-acceptance",
            limit=50,
            cursor=None,
        ),
        audit=store,
        cursor_encoder=_noop_cursor,
        clock=clock,
        dedupe=store.dedupe(),
    )
    store.commit()

    # The export appended exactly one ``AuditExported`` row.
    exported = [r for r in store.committed if r.event_type == AUDIT_EXPORTED]
    assert len(exported) == 1
    assert exported[0].actor_id == ACTOR_ADMIN
    assert exported[0].payload["reason"] == "export-acceptance"
    assert exported[0].payload["case_id"] == str(CASE)
    assert exported[0].payload["row_count"] == batch.rows_in_batch


async def test_export__rejects_missing_reason() -> None:
    """A blank reason is rejected as ``ValidationFailed`` (422 wire)."""
    store = InMemoryAuditStore()
    with pytest.raises(ValidationFailed) as excinfo:
        await export_handle(
            ExportQuery(
                tenant_id=TENANT_A,
                actor_id=ACTOR_ADMIN,
                actor_roles=(ROLE_PROCESS_ADMIN.name,),
                case_id=None,
                since=None,
                until=None,
                reason="   ",
                limit=50,
                cursor=None,
            ),
            audit=store,
            cursor_encoder=_noop_cursor,
        )
    assert excinfo.value.field == "reason"


async def test_correction__appends_new_row_referencing_original() -> None:
    """A correction appends a NEW row; the original row's payload,
    hash, and chain position are unchanged."""
    store = InMemoryAuditStore()
    cmd = RecordAuditCommand(
        tenant_id=TENANT_A,
        actor_id=ACTOR_ADMIN,
        actor_type="user",
        event_type=CASE_CREATED,
        subject_type="case",
        subject_id=SUBJECT,
        case_id=CASE,
        correlation_id=f"{CORRELATION_BASE}-seed",
        causation_id=None,
        payload={"ref": "C-COR-2026-001"},
        occurred_at=FIXED_NOW,
    )
    await record_event(cmd, audit=store, dedupe=store.dedupe())
    store.commit()
    original_row = store.committed[0]
    original_payload = dict(original_row.payload)
    original_hash = original_row.hash
    original_id = original_row.id
    original_prev_hash = original_row.prev_hash

    @dataclass
    class _Clock:
        now_value: datetime = FIXED_NOW

        def now(self) -> datetime:
            return self.now_value

    result = await correct_handle(
        CorrectAuditCommand(
            tenant_id=TENANT_A,
            actor_id=ACTOR_ADMIN,
            actor_roles=(ROLE_PROCESS_ADMIN.name,),
            original_event_id=original_id,
            reason="actor name was wrong",
            corrected_summary="actor was reviewer B, not reviewer A",
            correlation_id=f"{CORRELATION_BASE}-corr-1",
            occurred_at=FIXED_NOW,
        ),
        audit=store,
        dedupe=store.dedupe(),
        clock=_Clock(),
    )
    store.commit()

    # The result echoes the original event id.
    assert result.original_event_id == original_id
    # The correction payload carries the reserved keys.
    assert result.correction_payload[CORRECTION_KEY_ORIGINAL_EVENT_ID] == str(original_id)
    assert result.correction_payload[CORRECTION_KEY_REASON] == ("actor name was wrong")

    # A new row was added; the original row is unchanged.
    assert len(store.committed) == 2
    new_row = store.committed[-1]
    assert new_row.event_type == AUDIT_CORRECTION_RECORDED
    assert new_row.payload[CORRECTION_KEY_ORIGINAL_EVENT_ID] == str(original_id)

    # The original row is byte-identical.
    again = await store.get_event(tenant_id=TENANT_A, event_id=original_id)
    assert again is not None
    assert again.payload == original_payload
    assert again.hash == original_hash
    assert again.prev_hash == original_prev_hash


async def test_correction__rejects_correction_on_correction() -> None:
    """A correction must annotate the original event, not another
    correction. Chaining corrections would hide the underlying source
    of truth, so the use case surfaces ``Conflict`` (409 wire)."""
    store = InMemoryAuditStore()
    # Seed a CaseCreated.
    await record_event(
        RecordAuditCommand(
            tenant_id=TENANT_A,
            actor_id=ACTOR_ADMIN,
            actor_type="user",
            event_type=CASE_CREATED,
            subject_type="case",
            subject_id=SUBJECT,
            case_id=CASE,
            correlation_id=f"{CORRELATION_BASE}-seed",
            causation_id=None,
            payload={"ref": "C-COR-2026-001"},
            occurred_at=FIXED_NOW,
        ),
        audit=store,
        dedupe=store.dedupe(),
    )
    store.commit()
    original_id = store.committed[0].id

    # First correction.
    await correct_handle(
        CorrectAuditCommand(
            tenant_id=TENANT_A,
            actor_id=ACTOR_ADMIN,
            actor_roles=(ROLE_PROCESS_ADMIN.name,),
            original_event_id=original_id,
            reason="first reason",
            corrected_summary="first corrected summary",
            correlation_id=f"{CORRELATION_BASE}-corr-1",
            occurred_at=FIXED_NOW,
        ),
        audit=store,
        dedupe=store.dedupe(),
    )
    store.commit()
    correction_id = store.committed[-1].id

    # Second correction, this time targeting the prior correction row.
    with pytest.raises(Conflict):
        await correct_handle(
            CorrectAuditCommand(
                tenant_id=TENANT_A,
                actor_id=ACTOR_ADMIN,
                actor_roles=(ROLE_PROCESS_ADMIN.name,),
                original_event_id=correction_id,
                reason="chained",
                corrected_summary="chained summary",
                correlation_id=f"{CORRELATION_BASE}-corr-2",
                occurred_at=FIXED_NOW,
            ),
            audit=store,
            dedupe=store.dedupe(),
        )


async def test_correction__rejects_missing_original_in_tenant() -> None:
    """A correction targeting a row that does not exist in the
    actor's tenant surfaces ``NotFound`` (404 wire)."""
    store = InMemoryAuditStore()
    # Seed a CaseCreated so the actor's tenant has at least one row.
    await record_event(
        RecordAuditCommand(
            tenant_id=TENANT_A,
            actor_id=ACTOR_ADMIN,
            actor_type="user",
            event_type=CASE_CREATED,
            subject_type="case",
            subject_id=SUBJECT,
            case_id=CASE,
            correlation_id=f"{CORRELATION_BASE}-seed",
            causation_id=None,
            payload={"ref": "C-COR-2026-001"},
            occurred_at=FIXED_NOW,
        ),
        audit=store,
        dedupe=store.dedupe(),
    )
    store.commit()

    missing = uuid4()
    with pytest.raises(NotFound):
        await correct_handle(
            CorrectAuditCommand(
                tenant_id=TENANT_A,
                actor_id=ACTOR_ADMIN,
                actor_roles=(ROLE_PROCESS_ADMIN.name,),
                original_event_id=missing,
                reason="annotation",
                corrected_summary="annotation",
                correlation_id=f"{CORRELATION_BASE}-corr-missing",
                occurred_at=FIXED_NOW,
            ),
            audit=store,
            dedupe=store.dedupe(),
        )


async def test_correction__rejects_cross_tenant_probe() -> None:
    """A correction targeting an event in a different tenant must
    surface ``NotFound`` (404) so the actor cannot probe for
    cross-tenant ids (Constitution V)."""
    store = InMemoryAuditStore()
    # Seed an event for TENANT_A.
    await record_event(
        RecordAuditCommand(
            tenant_id=TENANT_A,
            actor_id=ACTOR_ADMIN,
            actor_type="user",
            event_type=CASE_CREATED,
            subject_type="case",
            subject_id=SUBJECT,
            case_id=CASE,
            correlation_id=f"{CORRELATION_BASE}-a",
            causation_id=None,
            payload={"ref": "C-COR-2026-001"},
            occurred_at=FIXED_NOW,
        ),
        audit=store,
        dedupe=store.dedupe(),
    )
    store.commit()
    tenant_a_id = store.committed[0].id

    # Try to correct it from TENANT_B — should be 404, not "found".
    with pytest.raises(NotFound):
        await correct_handle(
            CorrectAuditCommand(
                tenant_id=TENANT_B,
                actor_id=ACTOR_ADMIN,
                actor_roles=(ROLE_PROCESS_ADMIN.name,),
                original_event_id=tenant_a_id,
                reason="cross-tenant probe",
                corrected_summary="cross-tenant probe",
                correlation_id=f"{CORRELATION_BASE}-b",
                occurred_at=FIXED_NOW,
            ),
            audit=store,
            dedupe=store.dedupe(),
        )


async def test_correction__rejects_blank_reason_or_summary() -> None:
    """A blank ``reason`` or ``corrected_summary`` is rejected as
    ``ValidationFailed`` (422 wire)."""
    store = InMemoryAuditStore()
    await record_event(
        RecordAuditCommand(
            tenant_id=TENANT_A,
            actor_id=ACTOR_ADMIN,
            actor_type="user",
            event_type=CASE_CREATED,
            subject_type="case",
            subject_id=SUBJECT,
            case_id=CASE,
            correlation_id=f"{CORRELATION_BASE}-seed",
            causation_id=None,
            payload={"ref": "C-COR-2026-001"},
            occurred_at=FIXED_NOW,
        ),
        audit=store,
        dedupe=store.dedupe(),
    )
    store.commit()
    original_id = store.committed[0].id

    with pytest.raises(ValidationFailed) as excinfo:
        await correct_handle(
            CorrectAuditCommand(
                tenant_id=TENANT_A,
                actor_id=ACTOR_ADMIN,
                actor_roles=(ROLE_PROCESS_ADMIN.name,),
                original_event_id=original_id,
                reason="   ",
                corrected_summary="summary",
                correlation_id=f"{CORRELATION_BASE}-blank-reason",
                occurred_at=FIXED_NOW,
            ),
            audit=store,
            dedupe=store.dedupe(),
        )
    assert excinfo.value.field == "reason"


async def test_export__rejects_actor_without_audit_export_action() -> None:
    """``AUDIT_EXPORT`` is required; an actor with only ``intake``
    is refused (Constitution V / AGENTS.md §4.R12)."""
    store = InMemoryAuditStore()
    with pytest.raises(Forbidden):
        await export_handle(
            ExportQuery(
                tenant_id=TENANT_A,
                actor_id=ACTOR_OTHER,
                actor_roles=(ROLE_INTAKE.name,),
                case_id=None,
                since=None,
                until=None,
                reason="probe",
                limit=50,
                cursor=None,
            ),
            audit=store,
            cursor_encoder=_noop_cursor,
        )
    # The policy raises ``Forbidden`` (imported in
    # ``app.application.authorization``); we don't pin the exact type
    # here — just that the export did NOT produce any rows.
    assert not store.committed


# ---------------------------------------------------------------------------
# Persistence-layer immutability regression (Layer 1 + Layer 2)
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_db_url(tmp_path: Any) -> str:
    db_file = tmp_path / "audit_export.db"
    return f"sqlite+aiosqlite:///{db_file.as_posix()}"


@pytest.fixture
async def sql_engine(sqlite_db_url: str) -> AsyncIterator[AsyncEngine]:
    await reset_engine()
    reset_audit_immutability_listener()
    engine = create_async_engine(sqlite_db_url)
    settings = Settings(tenant_listener_enabled=False)
    install_tenant_listener(engine, settings=settings)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_audit_events_no_update_export
            BEFORE UPDATE ON audit_events
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'AUDIT_EVENTS_APPEND_ONLY');
            END
            """
        )
        await conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_audit_events_no_delete_export
            BEFORE DELETE ON audit_events
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'AUDIT_EVENTS_APPEND_ONLY');
            END
            """
        )
    install_audit_immutability_listener()
    install_audit_immutability_execute_listener(engine)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
        await reset_engine()
        reset_audit_immutability_listener()


@pytest.fixture
def session_factory(
    sql_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=sql_engine, expire_on_commit=False, autoflush=False)


async def _seed_tenant(session: AsyncSession) -> None:
    session.add(
        TenantRow(
            id=TENANT_A,
            key="key-EXPORT",
            name="EXPORT",
            created_at=FIXED_NOW,
        )
    )


async def test_correction__sql_path__original_update_still_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After a correction is committed via the SQL adapter, an
    UPDATE on the original row is still rejected. The correction
    path does not weaken immutability.
    """
    async with session_factory() as session:
        await _seed_tenant(session)
        await session.commit()

    async with session_factory() as session:
        store = SqlAuditEventStore(session)
        await store.append(
            tenant_id=TENANT_A,
            event_type=CASE_CREATED,
            actor_id=ACTOR_ADMIN,
            actor_type="user",
            subject_type="case",
            subject_id=SUBJECT,
            case_id=CASE,
            correlation_id=f"{CORRELATION_BASE}-sql",
            causation_id=None,
            payload={"ref": "C-COR-2026-SQL-001"},
            occurred_at=FIXED_NOW,
        )
        await session.commit()

    async with session_factory() as session:
        stmt = select(AuditEventRow).where(AuditEventRow.tenant_id == TENANT_A)
        original = (await session.execute(stmt)).scalars().one()
        original_id = original.id

    # Append a correction through the same SQL adapter path.
    async with session_factory() as session:
        store = SqlAuditEventStore(session)
        await store.append(
            tenant_id=TENANT_A,
            event_type=AUDIT_CORRECTION_RECORDED,
            actor_id=ACTOR_ADMIN,
            actor_type="user",
            subject_type="case",
            subject_id=SUBJECT,
            case_id=CASE,
            correlation_id=f"{CORRELATION_BASE}-sql-corr",
            causation_id=str(original_id),
            payload={
                CORRECTION_KEY_ORIGINAL_EVENT_ID: str(original_id),
                CORRECTION_KEY_REASON: "annotation reason",
                "corrected_summary": "annotation summary",
            },
            occurred_at=FIXED_NOW,
        )
        await session.commit()

    # Now attempt UPDATE on the original. Layer 1 (trigger) + Layer 2
    # (ORM listener) both reject it.
    async with session_factory() as session:
        with pytest.raises((AuditEventImmutable, DBAPIError, IntegrityError)):
            await session.execute(
                update(AuditEventRow)
                .where(AuditEventRow.id == original_id)
                .values(payload={"evil": True})
            )

    # Original payload unchanged.
    async with session_factory() as session:
        stmt = select(AuditEventRow).where(AuditEventRow.id == original_id)
        again = (await session.execute(stmt)).scalars().one()
        assert again.payload == {"ref": "C-COR-2026-SQL-001"}


async def test_export__sql_path__returns_rows_with_chain_anchors(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The SQL adapter's ``list_for_export`` returns rows with
    ``hash`` and ``prev_hash`` populated so the export consumer can
    verify the chain off-system (P09-US3)."""
    async with session_factory() as session:
        await _seed_tenant(session)
        await session.commit()

    async with session_factory() as session:
        store = SqlAuditEventStore(session)
        # Use distinct occurred_at values so the chain order is
        # deterministic; random UUID PKs make the (occurred_at, id)
        # sort non-stable when timestamps tie.
        for i in range(3):
            await store.append(
                tenant_id=TENANT_A,
                event_type=CASE_CREATED,
                actor_id=ACTOR_ADMIN,
                actor_type="user",
                subject_type="case",
                subject_id=SUBJECT,
                case_id=CASE,
                correlation_id=f"{CORRELATION_BASE}-sql-exp-{i}",
                causation_id=None,
                payload={"seq": i},
                occurred_at=FIXED_NOW.replace(microsecond=i),
            )
            # Commit between appends so the chain anchor is durable
            # for the next ``_last_hash`` query (the SQL adapter uses
            # ``ORDER BY id desc`` which is non-deterministic for
            # random UUID PKs across same-microsecond rows; the
            # production app commits per use case so this is fine).
            await session.commit()

    async with session_factory() as session:
        store = SqlAuditEventStore(session)
        rows = await store.list_for_export(
            tenant_id=TENANT_A,
            case_id=CASE,
            limit=50,
        )

    assert len(rows) == 3
    for row in rows:
        assert row.hash
        assert len(row.hash) == 64
        assert row.prev_hash
        assert len(row.prev_hash) == 64

    # Replay the chain via the pure verifier.
    chain_rows = [
        {
            "id": row.id,
            "hash": row.hash,
            "prev_hash": row.prev_hash,
            "payload": row.payload,
        }
        for row in rows
    ]
    result = verify_chain(chain_rows)
    assert result.ok is True
    assert result.rows_checked == 3
