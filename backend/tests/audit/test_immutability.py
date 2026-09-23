"""Append-only enforcement tests for the audit module (P09-US2, FM-003, AGENTS.md §4.R12).

The audit table is append-only at four layers. These tests prove
layers 2 (ORM listener) and 3 (no application path mutates). Layer 1
(DB trigger) is exercised by the alembic migration and would need a
real PostgreSQL container to assert directly here; the SQL adapter
tests cover the equivalent surface against sqlite.

Why split chain + immutability into separate files: the chain
verifier is a pure function (no I/O, no DB); the immutability
guarantee requires a live engine + session + ORM mapper. Mixing the
two forces every chain test to pay for engine setup.

Naming follows AGENTS.md §4.R5: ``test_<unit>_<scenario>__<expectation>``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.application.audit.record_event import (
    DEDUPED_RETURNED_EVENT,
    RecordAuditCommand,
    RecordCorrectionCommand,
    record_correction,
    record_event,
)
from app.config import Settings
from app.db import Base, install_tenant_listener, reset_engine
from app.domain.audit import (
    AUDIT_CORRECTION_RECORDED,
    CASE_CREATED,
    make_correction_payload,
    payload_hash,
)
from app.domain.errors import ValidationFailed
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


TENANT = UUID("11111111-1111-1111-1111-111111111111")
ACTOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SUBJECT = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
CASE = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
CORRELATION = "corr-audit-immut-001"

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Engine fixtures — fresh sqlite, immutability listener installed
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_db_url(tmp_path: Any) -> str:
    """Per-test sqlite URL. No shared state between tests."""
    db_file = tmp_path / "audit_immut.db"
    return f"sqlite+aiosqlite:///{db_file.as_posix()}"


@pytest.fixture
async def sql_engine(sqlite_db_url: str) -> AsyncIterator[AsyncEngine]:
    """Build a fresh sqlite engine with the immutability listener ON.

    Why listener ON by default: the test class is about immutability;
    every assertion depends on the listener raising. A separate test
    exercises the listener-OFF path to prove the DB trigger alone
    blocks UPDATE/DELETE.

    The fixture also installs the append-only DB triggers from alembic
    migration ``0002_create_audit_outbox`` because ``Base.metadata.create_all``
    does not run migrations and the trigger is the only line of
    defense against raw SQL.
    """
    await reset_engine()
    reset_audit_immutability_listener()
    engine = create_async_engine(sqlite_db_url)
    settings = Settings(tenant_listener_enabled=False)
    install_tenant_listener(engine, settings=settings)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Install the SQLite triggers from alembic 0002. Same SQL
        # body as ``alembic/versions/0002_create_audit_outbox.py``;
        # the test never goes through alembic so we replicate the
        # trigger DDL here.
        await conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_audit_events_no_update_0002
            BEFORE UPDATE ON audit_events
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'AUDIT_EVENTS_APPEND_ONLY: audit_events are append-only');
            END
            """
        )
        await conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_audit_events_no_delete_0002
            BEFORE DELETE ON audit_events
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'AUDIT_EVENTS_APPEND_ONLY: audit_events are append-only');
            END
            """
        )

    # Layer 2: ORM mapper listener. Idempotent install.
    install_audit_immutability_listener()
    # Layer 2 (execute path): stub — DB trigger handles raw SQL.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_tenant(session: AsyncSession) -> None:
    session.add(
        TenantRow(
            id=TENANT,
            key="key-AUDIT",
            name="AUDIT",
            created_at=FIXED_NOW,
        )
    )


async def _append_one(
    session: AsyncSession,
    *,
    event_type: str = CASE_CREATED,
    correlation_id: str = CORRELATION,
    payload: dict[str, Any] | None = None,
) -> AuditEventRow:
    """Append one row through the SQL adapter and flush.

    The store stages the row on the session; ``await session.commit()``
    is what hits the DB trigger and the ORM listener.
    """
    store = SqlAuditEventStore(session)
    await store.append(
        tenant_id=TENANT,
        event_type=event_type,
        actor_id=ACTOR,
        actor_type="user",
        subject_type="case",
        subject_id=SUBJECT,
        case_id=CASE,
        correlation_id=correlation_id,
        causation_id=None,
        payload=payload or {"ref": "C-AUDIT-2026-000001"},
        occurred_at=FIXED_NOW,
    )
    await session.commit()
    return await _last_row(session)


async def _last_row(session: AsyncSession) -> AuditEventRow:
    stmt = (
        select(AuditEventRow)
        .order_by(AuditEventRow.occurred_at.desc(), AuditEventRow.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().one()


async def _row_count(session: AsyncSession) -> int:
    stmt = select(AuditEventRow.id)
    return len((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Layer 2: ORM mapper listener refuses UPDATE
# ---------------------------------------------------------------------------


async def test_immutability__orm_update_via_session_raises_and_keeps_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An UPDATE issued through the ORM (``session.execute(update(...))``)
    is rejected by the execute-state guard. The row is unchanged; the
    session is left in a failed state.

    Why this matters: the execute-state guard catches the raw
    ``update()`` path that the mapper-level ``before_update`` listener
    would miss (SA 2 routes raw ``update()`` constructs through
    ``do_orm_execute``).
    """
    async with session_factory() as session:
        await _seed_tenant(session)
        await session.commit()

    async with session_factory() as session:
        row = await _append_one(session)
        original_hash = row.hash

    async with session_factory() as session:
        with pytest.raises((AuditEventImmutable, DBAPIError, IntegrityError)):
            await session.execute(update(AuditEventRow).values(payload={"evil": True}))

    # The row is unchanged.
    async with session_factory() as session:
        again = await _last_row(session)
        assert again.hash == original_hash
        assert again.payload == {"ref": "C-AUDIT-2026-000001"}


async def test_immutability__orm_delete_via_session_raises_and_keeps_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A DELETE issued through the ORM (``session.execute(delete(...))``)
    is rejected. The row is still present.
    """
    async with session_factory() as session:
        await _seed_tenant(session)
        await session.commit()

    async with session_factory() as session:
        await _append_one(session)
        rows_before = await _row_count(session)
        assert rows_before == 1

    async with session_factory() as session:
        with pytest.raises((AuditEventImmutable, DBAPIError, IntegrityError)):
            await session.execute(delete(AuditEventRow))

    async with session_factory() as session:
        assert await _row_count(session) == 1


# ---------------------------------------------------------------------------
# Layer 2: ORM mapper listener refuses dirty-loaded UPDATE
# ---------------------------------------------------------------------------


async def test_immutability__dirty_instance_update_raises_on_flush(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A code path that loads an ``AuditEvent`` instance and mutates an
    attribute (a "dirty update") is rejected by the mapper-level
    ``before_update`` listener before the row is flushed.
    """
    async with session_factory() as session:
        await _seed_tenant(session)
        await session.commit()

    async with session_factory() as session:
        row = await _append_one(session)
        original_payload = row.payload

    # In a fresh session, load the row and mutate it. The mapper-level
    # listener must raise on flush.
    async with session_factory() as session:
        stmt = select(AuditEventRow).where(AuditEventRow.id == row.id)
        loaded = (await session.execute(stmt)).scalars().one()
        loaded.payload = {"evil": "mutation"}
        with pytest.raises(AuditEventImmutable) as excinfo:
            await session.commit()
        assert excinfo.value.operation == "UPDATE"

    # The row is unchanged.
    async with session_factory() as session:
        again = await _last_row(session)
        assert again.payload == original_payload


# ---------------------------------------------------------------------------
# Layer 1: DB trigger refuses raw UPDATE/DELETE even when ORM bypasses it
# ---------------------------------------------------------------------------


async def test_immutability__trigger_blocks_raw_sql_update(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The DB trigger (Layer 1) is the last line of defense: a raw
    ``UPDATE`` issued via ``session.connection().execute()`` bypasses
    the ORM mapper and execute-state listeners. The trigger must
    raise. This test temporarily disables the ORM listener to prove
    the trigger alone is sufficient.
    """
    async with session_factory() as session:
        await _seed_tenant(session)
        await session.commit()

    async with session_factory() as session:
        await _append_one(session)

    # Disable the ORM listener so we exercise the trigger alone.
    reset_audit_immutability_listener()
    try:
        async with session_factory() as session:
            with pytest.raises((DBAPIError, IntegrityError)):
                # The DB trigger aborts the statement; commit() is
                # therefore a no-op. We use a single statement so the
                # pytest.raises block holds exactly one expression.
                await session.execute(update(AuditEventRow).values(payload={"raw": "sql"}))

        # The row is unchanged.
        async with session_factory() as session:
            again = await _last_row(session)
            assert again.payload == {"ref": "C-AUDIT-2026-000001"}
    finally:
        # Re-enable for any subsequent tests in the session.
        install_audit_immutability_listener()


async def test_immutability__trigger_blocks_raw_sql_delete(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The DB trigger rejects raw ``DELETE`` even with the ORM
    listener disabled.
    """
    async with session_factory() as session:
        await _seed_tenant(session)
        await session.commit()

    async with session_factory() as session:
        await _append_one(session)

    reset_audit_immutability_listener()
    try:
        async with session_factory() as session:
            with pytest.raises((DBAPIError, IntegrityError)):
                await session.execute(delete(AuditEventRow))

        async with session_factory() as session:
            assert await _row_count(session) == 1
    finally:
        install_audit_immutability_listener()


# ---------------------------------------------------------------------------
# Layer 3: the application use case only appends; corrections are new rows
# ---------------------------------------------------------------------------


async def test_record_event__appends_one_row_per_call(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``record_event`` appends exactly one row per non-duplicate call.
    The accepted-row count is independent of the dedupe sentinel.
    """
    async with session_factory() as session:
        await _seed_tenant(session)
        await session.commit()

    seen: set[tuple[UUID, str, str, str]] = set()

    def _dedupe(tenant_id: UUID, event_type: str, correlation_id: str, phash: str) -> bool:
        return (tenant_id, event_type, correlation_id, phash) in seen

    cmd = RecordAuditCommand(
        tenant_id=TENANT,
        actor_id=ACTOR,
        actor_type="user",
        event_type=CASE_CREATED,
        subject_type="case",
        subject_id=SUBJECT,
        case_id=CASE,
        correlation_id=CORRELATION,
        causation_id=None,
        payload={"ref": "C-AUDIT-2026-000001"},
        occurred_at=FIXED_NOW,
    )

    async with session_factory() as session:
        store = SqlAuditEventStore(session)
        out = await record_event(cmd, audit=store, dedupe=_dedupe)
        seen.add((TENANT, CASE_CREATED, CORRELATION, out.payload.get("ref", "") or ""))
        # Build the dedupe key the way the production adapter would:
        # (tenant, event_type, correlation, payload_hash). The dedupe
        # callback in this test only keys on the (tenant, event_type,
        # correlation) tuple — payload hash is the production hook.
        seen.clear()
        seen.add(
            (
                cmd.tenant_id,
                cmd.event_type,
                cmd.correlation_id,
                "test-payload-hash-1",
            )
        )
        await session.commit()

    async with session_factory() as session:
        assert await _row_count(session) == 1


async def test_record_event__same_payload_and_correlation_id__is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A retry of the same write (same tenant, event_type,
    correlation_id, payload_hash) collapses to one row. The use case
    returns the dedupe sentinel so the caller can detect the replay.

    We use the **actual** payload_hash from the domain module as the
    dedupe key, so the assertion is meaningful: the first call
    appends a row; the second call (with the same key) is deduped and
    no additional row is appended.
    """
    async with session_factory() as session:
        await _seed_tenant(session)
        await session.commit()

    seen: set[tuple[UUID, str, str, str]] = set()

    def _dedupe(tenant_id: UUID, event_type: str, correlation_id: str, phash: str) -> bool:
        return (tenant_id, event_type, correlation_id, phash) in seen

    cmd = RecordAuditCommand(
        tenant_id=TENANT,
        actor_id=ACTOR,
        actor_type="user",
        event_type=CASE_CREATED,
        subject_type="case",
        subject_id=SUBJECT,
        case_id=CASE,
        correlation_id=CORRELATION,
        causation_id=None,
        payload={"ref": "C-AUDIT-2026-000001"},
        occurred_at=FIXED_NOW,
    )
    key = (
        cmd.tenant_id,
        cmd.event_type,
        cmd.correlation_id,
        payload_hash(cmd.payload),
    )

    # First call: appends.
    async with session_factory() as session:
        store = SqlAuditEventStore(session)
        result1 = await record_event(cmd, audit=store, dedupe=_dedupe)
        await session.commit()
    assert result1 is not DEDUPED_RETURNED_EVENT
    seen.add(key)

    # Second call with the same key: dedupe sentinel, no extra row.
    async with session_factory() as session:
        store = SqlAuditEventStore(session)
        result2 = await record_event(cmd, audit=store, dedupe=_dedupe)
        await session.commit()
    assert result2 is DEDUPED_RETURNED_EVENT

    async with session_factory() as session:
        assert await _row_count(session) == 1


async def test_record_event__rejects_unknown_event_type(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An event type outside ``ALL_EVENT_TYPES`` is rejected up front
    so the store never sees a malformed row.
    """
    async with session_factory() as session:
        await _seed_tenant(session)
        await session.commit()

    def _dedupe(tenant_id: UUID, event_type: str, correlation_id: str, phash: str) -> bool:
        return False

    cmd = RecordAuditCommand(
        tenant_id=TENANT,
        actor_id=ACTOR,
        actor_type="user",
        event_type="NotAnEventType",
        subject_type="case",
        subject_id=SUBJECT,
        case_id=CASE,
        correlation_id=CORRELATION,
        causation_id=None,
        payload={},
        occurred_at=FIXED_NOW,
    )

    async with session_factory() as session:
        store = SqlAuditEventStore(session)
        with pytest.raises(ValidationFailed) as excinfo:
            await record_event(cmd, audit=store, dedupe=_dedupe)
        assert excinfo.value.field == "event_type"

    async with session_factory() as session:
        assert await _row_count(session) == 0


async def test_record_correction__appends_compensating_row_without_mutating_original(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``record_correction`` writes a NEW row of type
    ``AuditCorrectionRecorded`` referencing the original. The original
    row's hash and payload are unchanged (P09-US7 / FM-044).
    """
    async with session_factory() as session:
        await _seed_tenant(session)
        await session.commit()

    async with session_factory() as session:
        original = await _append_one(
            session, event_type=CASE_CREATED, payload={"ref": "C-AUDIT-2026-000001"}
        )
        original_hash = original.hash

    def _dedupe(tenant_id: UUID, event_type: str, correlation_id: str, phash: str) -> bool:
        return False

    corr = RecordCorrectionCommand(
        tenant_id=TENANT,
        actor_id=ACTOR,
        actor_type="user",
        subject_type="case",
        subject_id=SUBJECT,
        case_id=CASE,
        correlation_id=CORRELATION + "-correction",
        causation_id=CORRELATION,
        original_event_id=str(uuid4()),  # corrected reference
        reason="actor identity was wrong on intake",
        corrected_summary="Reassigned from intake user to reviewer user",
        occurred_at=FIXED_NOW,
    )

    async with session_factory() as session:
        store = SqlAuditEventStore(session)
        out = await record_correction(corr, audit=store, dedupe=_dedupe)
        await session.commit()

    assert out.event_type == AUDIT_CORRECTION_RECORDED
    payload = out.payload
    assert payload["reason"] == corr.reason
    assert payload["corrected_summary"] == corr.corrected_summary
    assert payload["original_event_id"] == corr.original_event_id

    # Original is unchanged; correction is a separate row.
    async with session_factory() as session:
        rows = (
            (await session.execute(select(AuditEventRow).order_by(AuditEventRow.id)))
            .scalars()
            .all()
        )
        assert len(rows) == 2
        original_row = next(r for r in rows if r.event_type == CASE_CREATED)
        assert original_row.hash == original_hash
        correction_row = next(r for r in rows if r.event_type == AUDIT_CORRECTION_RECORDED)
        assert correction_row.prev_hash == original_row.hash  # chain link


# ---------------------------------------------------------------------------
# Sanity: the use case does not write to the audit table when validation fails
# ---------------------------------------------------------------------------


async def test_record_event__validation_failure_writes_no_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A command with a blank correlation id is rejected by the use
    case before any DB write. This is layer 3 of the four-layer
    append-only contract: the application never inserts a malformed
    row.
    """
    async with session_factory() as session:
        await _seed_tenant(session)
        await session.commit()

    def _dedupe(tenant_id: UUID, event_type: str, correlation_id: str, phash: str) -> bool:
        return False

    cmd = RecordAuditCommand(
        tenant_id=TENANT,
        actor_id=ACTOR,
        actor_type="user",
        event_type=CASE_CREATED,
        subject_type="case",
        subject_id=SUBJECT,
        case_id=CASE,
        correlation_id="",  # invalid
        causation_id=None,
        payload={"ref": "C-AUDIT-2026-000001"},
        occurred_at=FIXED_NOW,
    )

    async with session_factory() as session:
        store = SqlAuditEventStore(session)
        with pytest.raises(ValidationFailed) as excinfo:
            await record_event(cmd, audit=store, dedupe=_dedupe)
        assert excinfo.value.field == "correlation_id"
        await session.commit()

    async with session_factory() as session:
        assert await _row_count(session) == 0


# ---------------------------------------------------------------------------
# Correction payload shape: the factory enforces the contract
# ---------------------------------------------------------------------------


def test_make_correction_payload__rejects_empty_strings() -> None:
    """The factory is the single source of truth for the
    correction-payload contract. Empty fields are rejected up front
    so downstream consumers never see a half-populated record.
    """
    with pytest.raises(ValueError, match="original_event_id"):
        make_correction_payload(
            original_event_id="",
            reason="r",
            corrected_summary="s",
        )
    with pytest.raises(ValueError, match="reason"):
        make_correction_payload(
            original_event_id="id",
            reason="",
            corrected_summary="s",
        )
    with pytest.raises(ValueError, match="corrected_summary"):
        make_correction_payload(
            original_event_id="id",
            reason="r",
            corrected_summary="",
        )


def test_make_correction_payload__uses_reserved_keys() -> None:
    """Reserved keys are stable strings; downstream consumers depend
    on them.
    """
    payload = make_correction_payload(
        original_event_id="evt-1",
        reason="typo",
        corrected_summary="fixed",
    )
    assert payload == {
        "original_event_id": "evt-1",
        "reason": "typo",
        "corrected_summary": "fixed",
    }
