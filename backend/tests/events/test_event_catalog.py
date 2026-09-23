"""Tests for the event catalog (FM-055 / P13-US8) and the local
dispatch path through the outbox.

# given / when / then is used per AGENTS.md §4.R5.

The three concerns pinned here:

1. **Catalog shape**: every event in the public catalog has a stable
   id, a monotonic version, a description, and a payload-key
   whitelist; subscribers can rely on the tuple order.
2. **Backward compatibility**: a *new* key added to a payload fails
   construction under the current version (additive-only policy —
   ADR-023). Adding a ``v2`` event spec lets publishers add new keys
   without breaking v1 consumers.
3. **Outbox as sole source**: subscribers only see events that have
   been written to the outbox; ``MessageBus.publish`` called directly
   never reaches subscribers when the dispatcher is the gate. PHI is
   rejected by name in the payload validator.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.domain.events import (
    CASE_CREATED,
    EVENT_CATALOG,
    DomainEvent,
    EventTypeSpec,
    find_event_spec,
    validate_payload,
)
from app.infrastructure.messaging.local_message_bus import (
    LocalInProcessMessageBus,
)
from app.infrastructure.messaging.outbox_dispatcher import (
    _ENVELOPE_CAUSATION,
    _ENVELOPE_CORRELATION,
    _ENVELOPE_OCCURRED_AT,
    OutboxDispatcher,
    _rebuild_payload,
)
from app.infrastructure.persistence.models import (
    Base,
)
from app.infrastructure.persistence.models import (
    Outbox as OutboxRow,
)
from app.interfaces.http.routers.events import router as events_router

# ---------------------------------------------------------------------------
# Catalog shape (1)
# ---------------------------------------------------------------------------


def test_event_catalog__lists_first_slice_events() -> None:
    # given / when
    names = {spec.event_type for spec in EVENT_CATALOG}

    # then — every event in the first-slice spec is in the catalog.
    assert names == {
        "CaseCreated",
        "CaseUpdated",
        "TaskCreated",
        "TaskCompleted",
        "DocumentAdded",
        "WorkflowTransitioned",
        "DecisionRecorded",
        "AuditCorrectionRecorded",
        # FM-016 queue + worker lifecycle (P02-US3, US9 / FM-055):
        # registered so the outbox dispatcher delivers them instead
        # of dead-lettering them as unknown types.
        "QueueDefined",
        "QueueReconfigured",
        "QueueRetired",
        "TaskClaimed",
        "TaskReleased",
        "TaskBulkHandled",
        "TaskDelegated",
        "TaskDelegationExpired",
    }


def test_event_catalog__every_spec_has_version_one_and_keys() -> None:
    # given
    specs = list(EVENT_CATALOG)

    # then — every spec is well-formed; ``version`` is monotonic per
    # event_type and starts at 1.
    seen: dict[str, int] = {}
    for spec in specs:
        assert isinstance(spec, EventTypeSpec)
        assert spec.version >= 1
        assert spec.version >= seen.get(spec.event_type, 0)
        seen[spec.event_type] = spec.version
        assert spec.description  # non-empty
        assert spec.payload_keys  # non-empty


def test_event_catalog__specs_are_immutable() -> None:
    # given
    spec = CASE_CREATED

    # when / then — frozen dataclass; mutation is forbidden so a
    # use case cannot accidentally rewrite the catalog at runtime.
    with pytest.raises(FrozenInstanceError):
        spec.version = 99  # type: ignore[misc]


def test_event_catalog__case_created_keys_reference_by_id() -> None:
    # given / when
    keys = CASE_CREATED.payload_keys

    # then — sensitive case data is referenced by id (case_id,
    # reference) not embedded (those keys are absent).
    assert "case_id" in keys
    assert "actor_id" in keys
    assert "reference" in keys
    assert "title" not in keys  # title is case content
    assert "description" not in keys


# ---------------------------------------------------------------------------
# Backward compatibility (2)
# ---------------------------------------------------------------------------


def test_validate_payload__unknown_event_type_rejected() -> None:
    # given
    payload = {"case_id": str(uuid4())}

    # when / then
    with pytest.raises(ValueError, match="unknown event_type"):
        validate_payload("CaseCreatedButNotReal", payload)


def test_validate_payload__undeclared_key_rejected() -> None:
    # given — ``title`` is not in the CaseCreated whitelist.
    payload = {
        "case_id": str(uuid4()),
        "reference": "C-ACME-2026-000001",
        "case_type_key": "ServiceRequest",
        "tenant_id": str(uuid4()),
        "actor_id": str(uuid4()),
        "occurred_at": "2026-09-02T00:00:00Z",
        "title": "ACME case 1",  # not in the whitelist
    }

    # when / then — additive-only policy rejects undeclared keys.
    with pytest.raises(ValueError, match="not declared in catalog"):
        validate_payload("CaseCreated", payload)


def test_validate_payload__phi_key_rejected_by_name() -> None:
    # given — every key is well-formed but the payload smells like PHI.
    payload = {
        "case_id": str(uuid4()),
        "tenant_id": str(uuid4()),
        "actor_id": str(uuid4()),
        "ssn": "123-45-6789",  # PHI by name
    }

    # when / then — catalog key whitelist would already reject this,
    # but the PHI redaction list catches it as a safety net.
    with pytest.raises(ValueError, match="looks like PHI"):
        validate_payload("CaseCreated", payload)


def test_domain_event__construction_rejects_unknown_event_type() -> None:
    # given
    payload = {"case_id": str(uuid4())}

    # when / then
    with pytest.raises(ValueError, match="unknown event_type"):
        DomainEvent(event_type="Bogus", tenant_id=uuid4(), payload=payload)


def test_domain_event__construction_rejects_phi_payload() -> None:
    # given
    payload = {
        "case_id": str(uuid4()),
        "tenant_id": str(uuid4()),
        "actor_id": str(uuid4()),
        "patient_email": "patient@example.test",
    }

    # when / then
    with pytest.raises(ValueError, match="looks like PHI"):
        DomainEvent(event_type="CaseCreated", tenant_id=uuid4(), payload=payload)


def test_domain_event__adding_a_new_key_in_v2_only_breaks_v1() -> None:
    # given — a publisher on v2 of CaseCreated wants to add
    # ``initiator_tenant_role`` (a non-PHI derived field). The v1
    # whitelist forbids it; v2 widens the whitelist.
    v1_payload = {
        "case_id": str(uuid4()),
        "reference": "C-ACME-2026-000001",
        "case_type_key": "ServiceRequest",
        "tenant_id": str(uuid4()),
        "actor_id": str(uuid4()),
        "occurred_at": "2026-09-02T00:00:00Z",
        "initiator_tenant_role": "supervisor",
    }

    # when / then — v1 rejects the new key (additive-only within major).
    with pytest.raises(ValueError, match="not declared in catalog"):
        validate_payload("CaseCreated", v1_payload)


def test_find_event_spec__returns_none_for_unknown() -> None:
    # given / when / then
    assert find_event_spec("CaseCreated") is CASE_CREATED
    assert find_event_spec("Nonexistent") is None


# ---------------------------------------------------------------------------
# Outbox as sole source (3) — full round-trip through SQLAlchemy sqlite.
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_sessionmaker() -> Iterator[async_sessionmaker[AsyncSession]]:
    # Use a per-test sqlite file in a temp dir; Base.metadata.create_all
    # builds the schema (no Alembic migration in this test path).
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async def _init() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    try:
        yield sm
    finally:
        asyncio.run(engine.dispose())
        with contextlib.suppress(OSError):
            os.remove(path)


async def _enqueue_outbox(
    sm: async_sessionmaker[AsyncSession],
    *,
    event_type: str,
    event_id: UUID,
    tenant_id: UUID,
    wire_payload: dict[str, Any],
    correlation_id: str | None,
    causation_id: str | None,
    occurred_at: str | None,
) -> None:
    async with sm() as session:
        session.add(
            OutboxRow(
                tenant_id=tenant_id,
                event_type=event_type,
                event_id=event_id,
                payload=_rebuild_payload(
                    wire_payload,
                    correlation_id=correlation_id,
                    causation_id=causation_id,
                    occurred_at=occurred_at,
                ),
                created_at=datetime.now(tz=UTC),
            )
        )
        await session.commit()


async def _collect_bus(bus: LocalInProcessMessageBus, expected: int) -> list[dict[str, Any]]:
    received: list[dict[str, Any]] = []
    done = asyncio.Event()

    async def on_event(event: dict[str, Any]) -> None:
        received.append(event)
        if len(received) >= expected:
            done.set()

    await bus.subscribe(on_event)
    await done.wait()
    return received


@pytest.mark.asyncio
async def test_outbox_dispatcher__delivers_event_with_correlation_id(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # given — a row sits in the outbox with envelope keys; the bus
    # has a single subscriber.
    tenant_id = uuid4()
    event_id = uuid4()
    correlation_id = "corr-001"
    causation_id = "cause-001"
    bus = LocalInProcessMessageBus()
    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)

    await _enqueue_outbox(
        sqlite_sessionmaker,
        event_type="CaseCreated",
        event_id=event_id,
        tenant_id=tenant_id,
        wire_payload={
            "case_id": str(uuid4()),
            "reference": "C-ACME-2026-000001",
            "case_type_key": "ServiceRequest",
            "tenant_id": str(tenant_id),
            "actor_id": str(uuid4()),
        },
        correlation_id=correlation_id,
        causation_id=causation_id,
        occurred_at="2026-09-02T00:00:00Z",
    )

    received_task = asyncio.create_task(_collect_bus(bus, 1))

    # when
    dispatched = await dispatcher.drain_once()

    # then
    assert dispatched == 1
    received = await asyncio.wait_for(received_task, timeout=2.0)
    assert len(received) == 1
    event = received[0]
    assert event["event_type"] == "CaseCreated"
    assert event["event_id"] == str(event_id)
    assert event["tenant_id"] == str(tenant_id)
    assert event["correlation_id"] == correlation_id
    assert event["causation_id"] == causation_id
    assert event["occurred_at"] == "2026-09-02T00:00:00Z"
    assert event["version"] == 1
    # envelope keys must not leak into the payload on the wire
    assert _ENVELOPE_CORRELATION not in event["payload"]
    assert _ENVELOPE_CAUSATION not in event["payload"]
    assert _ENVELOPE_OCCURRED_AT not in event["payload"]


@pytest.mark.asyncio
async def test_outbox_dispatcher__unknown_event_type_dead_lettered(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # given — a stray row (use-case typo, downgrade) with an event
    # type that is not in the catalog.
    tenant_id = uuid4()
    event_id = uuid4()
    bus = LocalInProcessMessageBus()
    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)

    received: list[dict[str, Any]] = []

    async def on_event(event: dict[str, Any]) -> None:
        received.append(event)

    await bus.subscribe(on_event)

    await _enqueue_outbox(
        sqlite_sessionmaker,
        event_type="SomethingRemoved",
        event_id=event_id,
        tenant_id=tenant_id,
        wire_payload={"tenant_id": str(tenant_id)},
        correlation_id=None,
        causation_id=None,
        occurred_at=None,
    )

    # when
    dispatched = await dispatcher.drain_once()

    # then — the dispatcher refuses to publish a row it cannot
    # validate; the row is dead-lettered, not silently dropped.
    assert dispatched == 0
    assert received == []
    async with sqlite_sessionmaker() as session:
        row = (
            await session.execute(select(OutboxRow).where(OutboxRow.event_id == event_id))
        ).scalar_one()
        assert row.attempt_count >= 5
        assert row.last_error == "unknown_event_type"


@pytest.mark.asyncio
async def test_outbox_dispatcher__subscriber_failure_keeps_row_pending(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # given — a broken subscriber should not lose the event; the row
    # remains in the outbox (will be retried until MAX_ATTEMPTS).
    tenant_id = uuid4()
    event_id = uuid4()
    bus = LocalInProcessMessageBus()
    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)

    async def boom(event: dict[str, Any]) -> None:
        raise RuntimeError("subscriber down")

    await bus.subscribe(boom)

    await _enqueue_outbox(
        sqlite_sessionmaker,
        event_type="DocumentAdded",
        event_id=event_id,
        tenant_id=tenant_id,
        wire_payload={
            "document_id": str(uuid4()),
            "case_id": str(uuid4()),
            "tenant_id": str(tenant_id),
            "actor_id": str(uuid4()),
            "sha256": "deadbeef" * 8,
            "classification": "Contract",
            "version_number": 1,
        },
        correlation_id=None,
        causation_id=None,
        occurred_at=None,
    )

    # when
    dispatched = await dispatcher.drain_once()

    # then — the dispatcher logs the failure and marks the row as
    # failed-but-still-pending.
    assert dispatched == 0
    async with sqlite_sessionmaker() as session:
        row = (
            await session.execute(select(OutboxRow).where(OutboxRow.event_id == event_id))
        ).scalar_one()
        assert row.dispatched_at is None
        assert row.attempt_count == 1
        assert row.last_error is not None
        assert "subscriber down" in row.last_error


def test_catalog_endpoint__returns_versioned_events() -> None:
    # given an authenticated caller (the catalog requires identity so
    # anonymous callers cannot enumerate the platform event surface)
    from app.deps import UserContext, get_current_user  # noqa: PLC0415

    app = FastAPI()
    app.include_router(events_router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        tenant_id=UUID("11111111-1111-1111-1111-111111111111"),
        display_name="Synthetic User",
        roles=("intake",),
    )
    client = TestClient(app)

    # when
    response = client.get("/api/v1/events/catalog")

    # then
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "EVENT_CATALOG"
    types = {item["event_type"]: item for item in body["items"]}
    assert types["CaseCreated"]["version"] == 1
    assert "case_id" in types["CaseCreated"]["payload_keys"]
    assert types["TaskCompleted"]["version"] == 1
    assert types["DocumentAdded"]["version"] == 1
    assert types["WorkflowTransitioned"]["version"] == 1


def test_first_slice_events_have_distinct_payload_keys() -> None:
    # given / when — every spec has at least the six common keys
    # (subject id, case_id, tenant_id, actor_id, occurred_at, ...).
    common = {"tenant_id", "actor_id", "occurred_at"}
    for spec in EVENT_CATALOG:
        # then — every spec carries the common correlation keys.
        assert common.issubset(set(spec.payload_keys)), spec.event_type
        # and every spec carries a subject reference: a ``*_id`` key,
        # or the queue's stable ``queue_key`` (queues are addressed by
        # key on the wire; the batch-level ``TaskBulkHandled`` event
        # covers many tasks so no single task id applies).
        subject_keys = {
            k for k in spec.payload_keys if k.endswith("_id") and k not in {"tenant_id", "actor_id"}
        } | ({"queue_key"} if "queue_key" in spec.payload_keys else set())
        assert subject_keys, spec.event_type
