"""Interface tests for the cases router (P01-US2, FR-002, FM-006).

End-to-end checks that the HTTP layer:

- accepts an authorized request and returns the canonical
  ``CaseSummary`` shape on ``GET /api/v1/cases/{id}``;
- accepts an authorized request and returns the paginated list on
  ``GET /api/v1/cases`` (default 50, max 200, FR-002 acceptance
  scenario);
- denies unauthorized actors with 403 (AGENTS.md §4.R12);
- redacts the structured payload for sensitive cases (P01-US4, FR-002
  acceptance scenario A4) without leaking the underlying content;
- rejects a cross-tenant case id with 404 (Constitution V).

Naming follows AGENTS.md §4.R5: ``test_<unit>_<scenario>__<expectation>``.

The fixture layer is sqlite-backed so the suite does not require a
PostgreSQL container (AGENTS.md §4.R1 fallback). A fresh DB is built
per test to keep tests isolated.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.db import Base
from app.deps import UserContext, get_current_user, get_db, register_local_user
from app.domain.case_type import (
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersionStatus,
)
from app.domain.errors import DomainError
from app.infrastructure.persistence import models  # noqa: F401 — register tables
from app.infrastructure.persistence.case_http_dependencies import (
    SqlCaseHttpDependencyProvider,
)
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.idempotency import IdempotencyMiddleware
from app.interfaces.http.routers.cases import router as cases_router

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
INTAKE = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
GHOST = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
MISSING_CASE_ID = UUID("77777777-7777-7777-7777-777777777777")


def _utc(year: int, month: int, day: int, hour: int, minute: int, second: int):
    from datetime import UTC, datetime  # noqa: PLC0415

    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


FIXED_NOW = _utc(2026, 8, 31, 12, 0, 0)


# ---------------------------------------------------------------------------
# Event-loop helper (pytest-asyncio's auto mode handles async tests; the
# sync helpers below build a fresh loop only for the per-test seeding
# routine)
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an awaitable to completion in a fresh event loop.

    The interface tests build the FastAPI app and TestClient in a sync
    context, then seed the sqlite DB with a tiny async routine. Doing
    this in a fresh loop avoids the ``There is no current event loop``
    DeprecationWarning under pytest's ``filterwarnings=error`` policy.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Engine fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_db_url(tmp_path: Any) -> str:
    """Per-test sqlite URL. No shared state between tests."""
    db_file = tmp_path / "cases_router.db"
    return f"sqlite+aiosqlite:///{db_file.as_posix()}"


@pytest.fixture
def engine_and_sm(sqlite_db_url: str):
    """Build a fresh sqlite engine + sessionmaker, run ``create_all``."""
    # Each TestClient request runs on its own portal loop. A pooled aiosqlite
    # connection can therefore outlive that loop and only be finalized when
    # the fixture disposes the engine on a different loop. NullPool makes the
    # session context own and close each connection on the loop that used it.
    engine = create_async_engine(sqlite_db_url, poolclass=NullPool)
    sm = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def _create_all() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    _run(_create_all())
    try:
        yield engine, sm
    finally:
        _run(engine.dispose())


@pytest.fixture(autouse=True)
def _reset_local_users():
    """The ``get_current_user`` dependency reads from a process-global
    registry. Clear it around the test so registered users from a prior
    test cannot leak in.
    """
    from app import deps as _deps  # noqa: PLC0415

    saved = dict(_deps._LOCAL_USERS)
    _deps._LOCAL_USERS.clear()
    try:
        yield
    finally:
        _deps._LOCAL_USERS.clear()
        _deps._LOCAL_USERS.update(saved)


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_tenant_and_type(
    session: AsyncSession, *, tenant_id: UUID, key: str = "ServiceRequest"
) -> tuple[UUID, UUID]:
    """Insert a tenant, case type, and a published version."""
    from app.infrastructure.persistence.models import (  # noqa: PLC0415
        CaseType as CaseTypeRow,
    )
    from app.infrastructure.persistence.models import (  # noqa: PLC0415
        CaseTypeVersion as CaseTypeVersionRow,
    )
    from app.infrastructure.persistence.models import Tenant as TenantRow  # noqa: PLC0415

    session.add(
        TenantRow(
            id=tenant_id,
            key=f"T{tenant_id.hex[:6]}",
            name=f"Tenant {tenant_id}",
            created_at=FIXED_NOW,
        )
    )

    case_type_id = uuid4()
    version_id = uuid4()
    now = FIXED_NOW

    session.add(
        CaseTypeRow(
            id=case_type_id,
            tenant_id=tenant_id,
            key=key,
            name=key,
            description="",
            sensitivity_default="internal",
            current_version_id=version_id,
            created_at=now,
            updated_at=now,
        )
    )
    schema = CaseTypeSchema(
        fields=(
            CaseTypeField(
                key="title",
                label="Title",
                type="text",
                required=True,
                sensitivity="internal",
            ),
            CaseTypeField(
                key="priority",
                label="Priority",
                type="enum",
                required=True,
                enum_values=("low", "normal", "high", "urgent"),
                sensitivity="internal",
            ),
        )
    )
    session.add(
        CaseTypeVersionRow(
            id=version_id,
            tenant_id=tenant_id,
            case_type_id=case_type_id,
            version_number=1,
            schema_json={
                "fields": [
                    {
                        "key": f.key,
                        "label": f.label,
                        "type": f.type,
                        "required": f.required,
                        "enum_values": list(f.enum_values),
                        "sensitivity": f.sensitivity,
                        "searchable": f.searchable,
                    }
                    for f in schema.fields
                ]
            },
            document_classifications=[],
            rules=[],
            status=CaseTypeVersionStatus.PUBLISHED.value,
            published_at=now,
            published_by=INTAKE,
            created_at=now,
        )
    )
    await session.flush()
    return case_type_id, version_id


async def _seed_case(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    case_type_version_id: UUID,
    title: str = "Help with onboarding",
    reference: str = "C-TEST-2026-000001",
) -> UUID:
    from app.infrastructure.persistence.models import Case as CaseRow  # noqa: PLC0415

    case_id = uuid4()
    now = FIXED_NOW
    session.add(
        CaseRow(
            id=case_id,
            tenant_id=tenant_id,
            reference=reference,
            case_type_version_id=case_type_version_id,
            status="open",
            priority="normal",
            title=title,
            description="First-time customer request.",
            data={
                "title": title,
                "priority": "normal",
                "redact_if_sensitive": "this-must-not-leak",
            },
            current_workflow_state="intake",
            version_token=1,
            created_by=INTAKE,
            created_at=now,
            updated_at=now,
            closed_at=None,
        )
    )
    await session.flush()
    return case_id


async def _seed_tenant_a_case_async(*, sm: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    async with sm() as session:
        _, ver_id = await _seed_tenant_and_type(session, tenant_id=TENANT_A)
        case_id = await _seed_case(session, tenant_id=TENANT_A, case_type_version_id=ver_id)
        await session.commit()
        return case_id, ver_id


def _seed_tenant_a_case(engine_and_sm) -> tuple[UUID, UUID]:
    _, sm = engine_and_sm
    return _run(_seed_tenant_a_case_async(sm=sm))


async def _seed_two_tenants_async(*, sm: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    """Seed TENANT_A and TENANT_B with their own case types."""
    async with sm() as session:
        _, ver_a = await _seed_tenant_and_type(session, tenant_id=TENANT_A)
        await _seed_tenant_and_type(session, tenant_id=TENANT_B, key="OtherType")
        case_id = await _seed_case(session, tenant_id=TENANT_A, case_type_version_id=ver_a)
        await session.commit()
        return case_id, ver_a


def _seed_two_tenants(engine_and_sm) -> tuple[UUID, UUID]:
    _, sm = engine_and_sm
    return _run(_seed_two_tenants_async(sm=sm))


async def _seed_sensitive_case_async(*, sm: async_sessionmaker[AsyncSession]) -> UUID:
    """Seed a case type marked sensitive + a case against it."""
    from sqlalchemy import update  # noqa: PLC0415

    from app.infrastructure.persistence.models import CaseType as CaseTypeRow  # noqa: PLC0415

    async with sm() as session:
        ct_id, ver_id = await _seed_tenant_and_type(session, tenant_id=TENANT_A)
        await session.execute(
            update(CaseTypeRow)
            .where(CaseTypeRow.id == ct_id)
            .values(sensitivity_default="restricted")
        )
        case_id = await _seed_case(
            session,
            tenant_id=TENANT_A,
            case_type_version_id=ver_id,
            title="Sensitive matter",
        )
        await session.commit()
        return case_id


def _seed_sensitive_case(engine_and_sm) -> UUID:
    _, sm = engine_and_sm
    return _run(_seed_sensitive_case_async(sm=sm))


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _build_app(sm: async_sessionmaker[AsyncSession]) -> FastAPI:
    """Build the FastAPI app with overrides pointed at the test session."""

    async def _override_get_db() -> Any:
        async with sm() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.state.case_http_dependencies = SqlCaseHttpDependencyProvider()
    app.add_middleware(IdempotencyMiddleware, scope_paths=())
    app.include_router(cases_router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[__import__("app.config", fromlist=["get_settings"]).get_settings] = (
        lambda: Settings(app_env="local")
    )  # type: ignore[arg-type]
    return app


def _override_user(*, tenant_id: UUID, user_id: UUID, roles: tuple[str, ...]):
    async def _dep() -> UserContext:
        return UserContext(
            user_id=user_id,
            tenant_id=tenant_id,
            display_name="",
            roles=roles,
        )

    return _dep


# ---------------------------------------------------------------------------
# GET /api/v1/cases/{id}
# ---------------------------------------------------------------------------


def test_view_case__authorized_actor__returns_full_summary(engine_and_sm) -> None:
    """A 200 with the canonical ``CaseSummary`` shape (FR-002 / P01-US2)."""
    _, sm = engine_and_sm
    case_id, _ = _seed_tenant_a_case(engine_and_sm)

    register_local_user(
        UserContext(
            user_id=INTAKE,
            tenant_id=TENANT_A,
            display_name="Alex Intake",
            roles=("intake",),
        )
    )
    app = _build_app(sm)
    app.dependency_overrides[get_current_user] = _override_user(
        tenant_id=TENANT_A, user_id=INTAKE, roles=("intake",)
    )

    with TestClient(app) as client:
        response = client.get(f"/api/v1/cases/{case_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reference"] == "C-TEST-2026-000001"
    assert body["case_type_key"] == "ServiceRequest"
    assert body["status"] == "open"
    assert body["title"] == "Help with onboarding"
    assert body["data"]["redact_if_sensitive"] == "this-must-not-leak"
    assert body["assignee"] == {"redacted": True, "reason": "not_implemented"}
    assert body["timeline"] == []


def test_view_case__actor_without_roles__returns_403(engine_and_sm) -> None:
    """An actor without roles is denied (AGENTS.md §4.R12)."""
    _, sm = engine_and_sm
    case_id, _ = _seed_tenant_a_case(engine_and_sm)

    register_local_user(
        UserContext(
            user_id=GHOST,
            tenant_id=TENANT_A,
            display_name="",
            roles=(),
        )
    )
    app = _build_app(sm)
    app.dependency_overrides[get_current_user] = _override_user(
        tenant_id=TENANT_A, user_id=GHOST, roles=()
    )

    with TestClient(app) as client:
        response = client.get(f"/api/v1/cases/{case_id}")
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_view_case__cross_tenant_case_id__returns_404(engine_and_sm) -> None:
    """A case in TENANT_A queried from TENANT_B surfaces as 404."""
    _, sm = engine_and_sm
    case_id, _ = _seed_two_tenants(engine_and_sm)

    register_local_user(
        UserContext(
            user_id=INTAKE,
            tenant_id=TENANT_B,
            display_name="",
            roles=("intake",),
        )
    )
    app = _build_app(sm)
    app.dependency_overrides[get_current_user] = _override_user(
        tenant_id=TENANT_B, user_id=INTAKE, roles=("intake",)
    )

    with TestClient(app) as client:
        response = client.get(f"/api/v1/cases/{case_id}")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_view_case__missing_case__returns_404(engine_and_sm) -> None:
    """A non-existent case id in the actor's tenant surfaces as 404."""
    _, sm = engine_and_sm
    _seed_tenant_a_case(engine_and_sm)

    register_local_user(
        UserContext(
            user_id=INTAKE,
            tenant_id=TENANT_A,
            display_name="",
            roles=("intake",),
        )
    )
    app = _build_app(sm)
    app.dependency_overrides[get_current_user] = _override_user(
        tenant_id=TENANT_A, user_id=INTAKE, roles=("intake",)
    )

    with TestClient(app) as client:
        response = client.get(f"/api/v1/cases/{MISSING_CASE_ID}")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/cases (list)
# ---------------------------------------------------------------------------


def test_list_cases__default_page_size_is_50(engine_and_sm) -> None:
    """No ``limit`` query param -> server uses the 50 default (FR-002)."""
    _, sm = engine_and_sm
    _seed_tenant_a_case(engine_and_sm)

    register_local_user(
        UserContext(
            user_id=INTAKE,
            tenant_id=TENANT_A,
            display_name="",
            roles=("intake",),
        )
    )
    app = _build_app(sm)
    app.dependency_overrides[get_current_user] = _override_user(
        tenant_id=TENANT_A, user_id=INTAKE, roles=("intake",)
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/cases")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["limit"] == 50
    assert isinstance(body["items"], list)
    assert body["next_cursor"] is None  # one item < 50


def test_list_cases__limit_above_200__rejected_by_query(engine_and_sm) -> None:
    """``limit > 200`` is rejected by FastAPI's ``Query(le=200)`` guard."""
    _, sm = engine_and_sm
    _seed_tenant_only(engine_and_sm)

    register_local_user(
        UserContext(
            user_id=INTAKE,
            tenant_id=TENANT_A,
            display_name="",
            roles=("intake",),
        )
    )
    app = _build_app(sm)
    app.dependency_overrides[get_current_user] = _override_user(
        tenant_id=TENANT_A, user_id=INTAKE, roles=("intake",)
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/cases?limit=500")
    assert response.status_code == 422


def test_list_cases__page_size_capped_at_200_in_response(engine_and_sm) -> None:
    """A request with ``limit=200`` returns ``"limit": 200`` in the page envelope."""
    _, sm = engine_and_sm
    _seed_tenant_only(engine_and_sm)

    register_local_user(
        UserContext(
            user_id=INTAKE,
            tenant_id=TENANT_A,
            display_name="",
            roles=("intake",),
        )
    )
    app = _build_app(sm)
    app.dependency_overrides[get_current_user] = _override_user(
        tenant_id=TENANT_A, user_id=INTAKE, roles=("intake",)
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/cases?limit=200")
    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 200


def test_list_cases__actor_without_roles__returns_403(engine_and_sm) -> None:
    """An actor without roles is denied even on the list endpoint."""
    _, sm = engine_and_sm
    register_local_user(
        UserContext(
            user_id=GHOST,
            tenant_id=TENANT_A,
            display_name="",
            roles=(),
        )
    )
    app = _build_app(sm)
    app.dependency_overrides[get_current_user] = _override_user(
        tenant_id=TENANT_A, user_id=GHOST, roles=()
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/cases")
    assert response.status_code == 403


def test_list_cases__items_have_no_workspace_slots(engine_and_sm) -> None:
    """The list view strips workspace-only slots to keep the payload compact."""
    _, sm = engine_and_sm
    _seed_tenant_a_case(engine_and_sm)

    register_local_user(
        UserContext(
            user_id=INTAKE,
            tenant_id=TENANT_A,
            display_name="",
            roles=("intake",),
        )
    )
    app = _build_app(sm)
    app.dependency_overrides[get_current_user] = _override_user(
        tenant_id=TENANT_A, user_id=INTAKE, roles=("intake",)
    )

    with TestClient(app) as client:
        body = client.get("/api/v1/cases").json()
    assert body["items"], body
    row = body["items"][0]
    for slot in (
        "assignee",
        "queue",
        "sla_state",
        "participants",
        "open_tasks",
        "recent_documents",
        "recent_decisions",
        "timeline",
        "data",
        "description",
    ):
        assert slot not in row, (slot, row)


# ---------------------------------------------------------------------------
# Sensitive redaction (P01-US4 / FR-002 acceptance A4)
# ---------------------------------------------------------------------------


def test_view_case__sensitive_case__redacts_structured_data(engine_and_sm) -> None:
    """A sensitive case's structured ``data`` is replaced with a redacted
    marker so the actor cannot see the underlying content. Reference +
    type + status remain visible so the operator still knows the case
    exists.
    """
    _, sm = engine_and_sm
    case_id = _seed_sensitive_case(engine_and_sm)

    register_local_user(
        UserContext(
            user_id=INTAKE,
            tenant_id=TENANT_A,
            display_name="",
            roles=("intake",),
        )
    )
    app = _build_app(sm)
    app.dependency_overrides[get_current_user] = _override_user(
        tenant_id=TENANT_A, user_id=INTAKE, roles=("intake",)
    )

    with TestClient(app) as client:
        body = client.get(f"/api/v1/cases/{case_id}").json()
    # Structured data is redacted; nothing leaks.
    assert body["data"] == {"redacted": True, "reason": "sensitive"}
    assert "redact_if_sensitive" not in str(body["data"])
    # Reference + type remain visible (FR-002 acceptance A4).
    assert body["reference"] == "C-TEST-2026-000001"
    assert body["case_type_key"] == "ServiceRequest"
    assert body["title"] == "Sensitive matter"


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


async def _seed_tenant_only_async(*, sm: async_sessionmaker[AsyncSession]) -> None:
    async with sm() as session:
        await _seed_tenant_and_type(session, tenant_id=TENANT_A)
        await session.commit()


def _seed_tenant_only(engine_and_sm) -> None:
    _, sm = engine_and_sm
    return _run(_seed_tenant_only_async(sm=sm))
