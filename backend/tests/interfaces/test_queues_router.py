"""SQLite endpoint coverage for the queue HTTP boundary (FM-016).

Exercises the production provider composition
(``SqlQueueHttpDependencyProvider``) end-to-end: admin CRUD, tenant
isolation, worker claim/release, and authorization denial.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.deps import UserContext, get_current_user, get_db
from app.domain.errors import DomainError
from app.domain.task import Task
from app.infrastructure.persistence.models import AuditEvent as AuditEventRow
from app.infrastructure.persistence.queue_http_dependencies import (
    SqlQueueHttpDependencyProvider,
)
from app.infrastructure.persistence.repositories import SqlTaskRepository
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.queues import router
from tests._live_roles import seed_live_roles

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ADMIN_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ADMIN_B_ID = UUID("abababab-abab-abab-abab-abababababab")
WORKER_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
DELEGATE_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
CASE_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _run(coro: Any) -> Any:
    """Run an async seed operation for the synchronous TestClient test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    roles: tuple[str, ...] = ("process_admin",),
    user_id: UUID = ADMIN_ID,
    tenant_id: UUID = TENANT_A,
) -> FastAPI:
    """Build a minimal app using the production provider composition."""
    _run(
        seed_live_roles(
            session_factory,
            tenant_id=tenant_id,
            user_id=user_id,
            role_names=roles,
        )
    )

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.state.queue_http_dependencies = SqlQueueHttpDependencyProvider()
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        display_name="Synthetic User",
        roles=roles,
    )
    return app


async def _seed_task_on_queue(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    queue_key: str,
    tenant_id: UUID = TENANT_A,
) -> UUID:
    """Stage one open task assigned to ``queue_key``; return its id."""
    task_id = uuid4()
    async with session_factory() as session:
        await SqlTaskRepository(session).add(
            Task(
                id=task_id,
                tenant_id=tenant_id,
                case_id=CASE_ID,
                type="review",
                title="Synthetic review task",
                assignee_user_id=None,
                assignee_queue_id=queue_key,
                created_at=NOW,
            )
        )
        await session.commit()
    return task_id


def test_queues_router__define_and_list__round_trips_through_sql(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Admin defines a queue; list returns it (FM-016 admin CRUD)."""
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/admin/queues",
            json={"key": "review_queue", "name": "Review", "assignment_policy": "manual"},
        )
        assert created.status_code == 201, created.text
        assert created.json()["key"] == "review_queue"

        listed = client.get("/api/v1/admin/queues")
        assert listed.status_code == 200, listed.text
        assert [q["key"] for q in listed.json()] == ["review_queue"]


def test_queues_router__list__stays_tenant_scoped(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Tenant B's queue is invisible from tenant A (Constitution V)."""
    app_b = _build_app(sql_session_factory, tenant_id=TENANT_B, user_id=ADMIN_B_ID)
    with TestClient(app_b) as client:
        created = client.post(
            "/api/v1/admin/queues",
            json={"key": "tenant_b_queue", "name": "Tenant B"},
        )
        assert created.status_code == 201, created.text

    app_a = _build_app(sql_session_factory, tenant_id=TENANT_A)
    with TestClient(app_a) as client:
        listed = client.get("/api/v1/admin/queues")
        assert listed.status_code == 200, listed.text
        assert all(q["key"] != "tenant_b_queue" for q in listed.json())


def test_queues_router__define__duplicate_key__conflicts(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second define with the same key is a 409, not a silent overwrite."""
    app = _build_app(sql_session_factory)
    with TestClient(app) as client:
        first = client.post("/api/v1/admin/queues", json={"key": "dup_queue", "name": "First"})
        assert first.status_code == 201, first.text
        second = client.post("/api/v1/admin/queues", json={"key": "dup_queue", "name": "Second"})
        assert second.status_code == 409, second.text


def test_queues_router__define__non_admin__forbidden(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Queue administration requires process_admin (fail-closed)."""
    app = _build_app(sql_session_factory, roles=("reviewer",), user_id=WORKER_ID)
    with TestClient(app) as client:
        response = client.post("/api/v1/admin/queues", json={"key": "nope", "name": "Nope"})
        assert response.status_code in (403, 422), response.text


def test_queues_router__claim_and_release__moves_task_assignment(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Worker claims the open task then releases it (FM-016 worker flow)."""
    admin_app = _build_app(sql_session_factory)
    with TestClient(admin_app) as client:
        created = client.post("/api/v1/admin/queues", json={"key": "work_queue", "name": "Work"})
        assert created.status_code == 201, created.text

    task_id = _run(_seed_task_on_queue(sql_session_factory, queue_key="work_queue"))

    worker_app = _build_app(sql_session_factory, roles=("reviewer",), user_id=WORKER_ID)
    with TestClient(worker_app) as client:
        claimed = client.post("/api/v1/queues/work_queue/claim")
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["task_id"] == str(task_id)

        released = client.post(f"/api/v1/tasks/{task_id}/release", json={"queue_key": "work_queue"})
        assert released.status_code == 200, released.text
        assert released.json()["status"] == "released"


def test_queues_router__claim__empty_queue__not_found(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Claiming from a queue with no open work is a 404 (nothing to do)."""
    admin_app = _build_app(sql_session_factory)
    with TestClient(admin_app) as client:
        created = client.post("/api/v1/admin/queues", json={"key": "empty_queue", "name": "Empty"})
        assert created.status_code == 201, created.text

    worker_app = _build_app(sql_session_factory, roles=("reviewer",), user_id=WORKER_ID)
    with TestClient(worker_app) as client:
        response = client.post("/api/v1/queues/empty_queue/claim")
        assert response.status_code == 404, response.text


async def _seed_claimed_task(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID = TENANT_A,
    owner_id: UUID = WORKER_ID,
) -> UUID:
    """Stage one open task already claimed by ``owner_id``; return its id."""
    task_id = uuid4()
    async with session_factory() as session:
        await SqlTaskRepository(session).add(
            Task(
                id=task_id,
                tenant_id=tenant_id,
                case_id=CASE_ID,
                type="review",
                title="Synthetic claimed task",
                assignee_user_id=owner_id,
                assignee_queue_id=None,
                created_at=NOW,
            )
        )
        await session.commit()
    return task_id


async def _seed_aged_task(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    queue_key: str,
    title: str,
    due_at: datetime | None,
    tenant_id: UUID = TENANT_A,
) -> UUID:
    """Stage one open queue task with an explicit due date; return its id."""
    task_id = uuid4()
    async with session_factory() as session:
        await SqlTaskRepository(session).add(
            Task(
                id=task_id,
                tenant_id=tenant_id,
                case_id=CASE_ID,
                type="review",
                title=title,
                assignee_user_id=None,
                assignee_queue_id=queue_key,
                due_at=due_at,
                created_at=datetime.now(tz=UTC) - timedelta(days=2),
            )
        )
        await session.commit()
    return task_id


async def _audit_events(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event_type: str,
    tenant_id: UUID = TENANT_A,
) -> list[AuditEventRow]:
    """Return audit rows of ``event_type`` for ``tenant_id`` (oldest first)."""
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(AuditEventRow)
                    .where(
                        AuditEventRow.tenant_id == tenant_id,
                        AuditEventRow.event_type == event_type,
                    )
                    .order_by(AuditEventRow.occurred_at.asc())
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


def test_queues_router__bulk_handle__claims_batch__skips_missing(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Bulk claim handles staged tasks and skips unknown ids (FM-016)."""
    # given: a queue with one open task.
    admin_app = _build_app(sql_session_factory)
    with TestClient(admin_app) as client:
        created = client.post("/api/v1/admin/queues", json={"key": "bulk_queue", "name": "Bulk"})
        assert created.status_code == 201, created.text
    task_id = _run(_seed_task_on_queue(sql_session_factory, queue_key="bulk_queue"))
    missing_id = uuid4()

    # when: the worker bulk-claims the staged task plus an unknown id.
    worker_app = _build_app(sql_session_factory, roles=("reviewer",), user_id=WORKER_ID)
    with TestClient(worker_app) as client:
        response = client.post(
            "/api/v1/queues/bulk_queue/bulk-handle",
            json={"action": "claim", "task_ids": [str(task_id), str(missing_id)]},
        )

    # then: the known task is handled; the unknown id is skipped, not fatal.
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["handled"] == [str(task_id)]
    assert body["skipped"] == [str(missing_id)]


def test_queues_router__claim_release_delegate__round_trip_with_aging(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Claim -> release -> claim -> delegate round-trips with aging visible."""
    # given: a queue holding one overdue task (aging context is observable).
    admin_app = _build_app(sql_session_factory)
    with TestClient(admin_app) as client:
        created = client.post("/api/v1/admin/queues", json={"key": "round_queue", "name": "Round"})
        assert created.status_code == 201, created.text
    overdue_id = _run(
        _seed_aged_task(
            sql_session_factory,
            queue_key="round_queue",
            title="Overdue intake",
            due_at=datetime.now(tz=UTC) - timedelta(hours=3),
        )
    )
    until = (datetime.now(tz=UTC) + timedelta(days=7)).isoformat()

    worker_app = _build_app(sql_session_factory, roles=("reviewer",), user_id=WORKER_ID)
    with TestClient(worker_app) as client:
        # when: the worker reads the queue tasks and sees the overdue flag...
        listed = client.get("/api/v1/queues/round_queue/tasks")
        assert listed.status_code == 200, listed.text
        rows = {row["task_id"]: row for row in listed.json()}
        assert rows[str(overdue_id)]["aging"]["is_overdue"] is True
        assert rows[str(overdue_id)]["aging"]["overdue_by_seconds"] is not None

        # ...claims the task, releases it back, claims it again...
        claimed = client.post("/api/v1/queues/round_queue/claim")
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["task_id"] == str(overdue_id)
        released = client.post(
            f"/api/v1/tasks/{overdue_id}/release", json={"queue_key": "round_queue"}
        )
        assert released.status_code == 200, released.text
        assert released.json() == {"task_id": str(overdue_id), "status": "released"}
        reclaimed = client.post("/api/v1/queues/round_queue/claim")
        assert reclaimed.status_code == 200, reclaimed.text

        # ...then delegates it for a bounded window.
        delegated = client.post(
            "/api/v1/tasks/delegate",
            json={
                "delegate_user_id": str(DELEGATE_ID),
                "task_ids": [str(overdue_id)],
                "until": until,
            },
        )
        assert delegated.status_code == 200, delegated.text
        assert delegated.json()["delegated"] == [str(overdue_id)]
        assert delegated.json()["delegate_user_id"] == str(DELEGATE_ID)
        # why parsed comparison: Pydantic serializes aware datetimes
        # with a ``Z`` suffix while ``isoformat()`` emits ``+00:00`` —
        # the instants match, only the rendering differs.
        assert datetime.fromisoformat(
            delegated.json()["until"].replace("Z", "+00:00")
        ) == datetime.fromisoformat(until)

    # then: the delegation is audited with its time bound.
    events = _run(_audit_events(sql_session_factory, event_type="TaskDelegated"))
    assert len(events) == 1
    assert events[0].payload["delegate_user_id"] == str(DELEGATE_ID)
    assert datetime.fromisoformat(
        str(events[0].payload["until"]).replace("Z", "+00:00")
    ) == datetime.fromisoformat(until)
    assert events[0].payload["window_seconds"] > 0


def test_queues_router__delegate__past_window__conflicts(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A delegation window in the past is a 409 ErrorResponse, not a silent handoff."""
    # given: a task claimed by the worker.
    admin_app = _build_app(sql_session_factory)
    with TestClient(admin_app) as client:
        created = client.post("/api/v1/admin/queues", json={"key": "past_queue", "name": "Past"})
        assert created.status_code == 201, created.text
    task_id = _run(_seed_claimed_task(sql_session_factory))

    # when: the worker delegates with a past ``until``.
    worker_app = _build_app(sql_session_factory, roles=("reviewer",), user_id=WORKER_ID)
    with TestClient(worker_app) as client:
        response = client.post(
            "/api/v1/tasks/delegate",
            json={
                "delegate_user_id": str(DELEGATE_ID),
                "task_ids": [str(task_id)],
                "until": (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat(),
            },
        )

    # then: 409 with the structured error envelope (never a stack trace).
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "conflict"
    assert "message" in body


def test_queues_router__conflicts__return_error_response_shape(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Conflict paths return ``ErrorResponse{code,message,...}`` (AGENTS.md R7)."""
    app = _build_app(sql_session_factory)
    with TestClient(app) as client:
        first = client.post("/api/v1/admin/queues", json={"key": "shape_queue", "name": "First"})
        assert first.status_code == 201, first.text
        second = client.post("/api/v1/admin/queues", json={"key": "shape_queue", "name": "Second"})
        assert second.status_code == 409, second.text
        body = second.json()
        assert body["code"] == "conflict"
        assert "message" in body

    # given: a task owned by someone else.
    stranger_id = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
    other_task = _run(_seed_claimed_task(sql_session_factory, owner_id=stranger_id))
    worker_app = _build_app(sql_session_factory, roles=("reviewer",), user_id=WORKER_ID)
    with TestClient(worker_app) as client:
        # when: the worker releases a task they do not own...
        response = client.post(f"/api/v1/tasks/{other_task}/release", json={})

    # then: 409 with the structured envelope.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "conflict"

    with TestClient(worker_app) as client:
        # when: the worker claims from an empty queue...
        empty = client.post("/api/v1/queues/shape_queue/claim")

    # then: 404 with the structured envelope (code, not a bare string).
    # Why ``detail``: FastAPI wraps ``HTTPException.detail`` one level
    # deep; the inner dict is the ``ErrorResponse`` shape.
    assert empty.status_code == 404, empty.text
    assert empty.json()["detail"]["code"] == "no_work_available"


def test_queues_router__list_visible__returns_worker_queues_tenant_scoped(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``GET /queues`` lists the tenant catalog to workers (FM-016 list).

    Why only the default scope here: the SQL adapter has no
    ``visibility_scope`` column yet (see ``mapping.py`` — every row
    reads back as ``TENANT``), so member-filtering is pinned at the
    domain/application level in
    ``test_queue_scope_aging_delegation.py``. This test pins the HTTP
    contract that remains: workers see the tenant catalog, and tenant
    B rows stay invisible.
    """
    admin_app = _build_app(sql_session_factory)
    with TestClient(admin_app) as client:
        for key in ("alpha_queue", "beta_queue"):
            created = client.post("/api/v1/admin/queues", json={"key": key, "name": key.title()})
            assert created.status_code == 201, created.text

    worker_app = _build_app(sql_session_factory, roles=("reviewer",), user_id=WORKER_ID)
    with TestClient(worker_app) as client:
        listed = client.get("/api/v1/queues")
        assert listed.status_code == 200, listed.text
        assert {q["key"] for q in listed.json()} >= {"alpha_queue", "beta_queue"}

    other_admin = _build_app(sql_session_factory, tenant_id=TENANT_B)
    with TestClient(other_admin) as client:
        created = client.post("/api/v1/admin/queues", json={"key": "tenant_b_only", "name": "B"})
        assert created.status_code == 201, created.text

    with TestClient(worker_app) as client:
        listed = client.get("/api/v1/queues")
        assert listed.status_code == 200, listed.text
        assert all(q["key"] != "tenant_b_only" for q in listed.json())


def test_queues_router__openapi__queue_paths_present(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The OpenAPI contract exposes every queue worker endpoint (AGENTS.md R7)."""
    app = _build_app(sql_session_factory)
    with TestClient(app) as client:
        schema = client.get("/openapi.json")
    assert schema.status_code == 200, schema.text
    paths = schema.json()["paths"]
    assert "/api/v1/queues" in paths
    assert "/api/v1/queues/{queue_key}/tasks" in paths
    assert "/api/v1/queues/{queue_key}/claim" in paths
    assert "/api/v1/queues/{queue_key}/bulk-handle" in paths
    assert "/api/v1/tasks/{task_id}/release" in paths
    assert "/api/v1/tasks/delegate" in paths
