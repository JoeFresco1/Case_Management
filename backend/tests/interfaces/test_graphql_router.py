"""HTTP boundary tests for the governed GraphQL router (P13-US11 / FM-057).

Exercises ``POST /api/v1/graphql`` end-to-end against sqlite through
the production provider composition
(``SqlCaseHttpDependencyProvider`` on ``app.state``):

- an authorized case query returns the governed fields;
- a cross-tenant or missing case id yields ``null`` + ``errors`` (no leak);
- an unknown field is rejected with a ``validation_failed`` error;
- malformed queries, ``mutation`` operations, and ``variables`` are 400;
- an unauthenticated caller is 401 via the standard auth handlers.

Naming follows AGENTS.md section 4 rule R5:
``test_<unit>_<scenario>__<expectation>`` with given/when/then steps.
Cases are seeded through :class:`SqlCaseRepository` so the suite covers
the real SQL read path, not the in-memory fakes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.deps import UserContext, get_current_user, get_db
from app.domain.case import Case, CaseReference, CaseStatus
from app.domain.errors import DomainError
from app.infrastructure.persistence.case_http_dependencies import (
    SqlCaseHttpDependencyProvider,
)
from app.infrastructure.persistence.repositories import SqlCaseRepository
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.graphql import router as graphql_router

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
INTAKE_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
MISSING_CASE_ID = UUID("77777777-7777-7777-7777-777777777777")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def _run(coro: Any) -> Any:
    """Run an async seeding routine to completion in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    roles: tuple[str, ...] = ("intake",),
    user_id: UUID = INTAKE_USER,
    tenant_id: UUID = TENANT_A,
) -> FastAPI:
    """Build a minimal app wired to the production case provider."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.state.case_http_dependencies = SqlCaseHttpDependencyProvider()
    app.include_router(graphql_router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        display_name="Synthetic User",
        roles=roles,
    )
    return app


async def _seed_open_case(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID = TENANT_A,
    reference: str = "C-TEST-2026-000001",
    title: str = "Help with onboarding",
) -> UUID:
    """Seed tenant + case type + published version, then the case via SQL repo."""
    from app.infrastructure.persistence.models import (  # noqa: PLC0415
        CaseType as CaseTypeRow,
    )
    from app.infrastructure.persistence.models import (  # noqa: PLC0415
        CaseTypeVersion as CaseTypeVersionRow,
    )
    from app.infrastructure.persistence.models import Tenant as TenantRow  # noqa: PLC0415

    case_id = uuid4()
    case_type_id = uuid4()
    version_id = uuid4()
    async with session_factory() as session:
        session.add(
            TenantRow(
                id=tenant_id,
                key=f"T{tenant_id.hex[:6]}",
                name=f"Tenant {tenant_id}",
                created_at=FIXED_NOW,
            )
        )
        session.add(
            CaseTypeRow(
                id=case_type_id,
                tenant_id=tenant_id,
                key="ServiceRequest",
                name="ServiceRequest",
                description="",
                sensitivity_default="internal",
                current_version_id=version_id,
                created_at=FIXED_NOW,
                updated_at=FIXED_NOW,
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
                            "key": "title",
                            "label": "Title",
                            "type": "text",
                            "required": True,
                            "enum_values": [],
                            "sensitivity": "internal",
                            "searchable": True,
                        }
                    ]
                },
                document_classifications=[],
                rules=[],
                status="published",
                published_at=FIXED_NOW,
                published_by=INTAKE_USER,
                created_at=FIXED_NOW,
            )
        )
        await session.flush()
        # why: the case row itself goes through the production SQL
        # repository so the suite covers the real read path (tenant
        # filter, mapping) rather than a raw row insert.
        await SqlCaseRepository(session).add(
            Case(
                id=case_id,
                tenant_id=tenant_id,
                reference=CaseReference(value=reference),
                case_type_version_id=version_id,
                status=CaseStatus.OPEN,
                priority="normal",
                title=title,
                description="First-time customer request.",
                data={"title": title},
                current_workflow_state=None,
                version_token=1,
                created_at=FIXED_NOW,
                updated_at=FIXED_NOW,
                created_by=INTAKE_USER,
                closed_at=None,
                sensitivity="internal",
            )
        )
        await session.commit()
    return case_id


def test_graphql_router_authorized_case_query__returns_fields(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An authorized actor reads the governed case fields over HTTP."""
    # // given an open case in the actor's tenant
    case_id = _run(_seed_open_case(sql_session_factory))
    app = _build_app(sql_session_factory)
    # // when the actor posts a case query
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/graphql",
            json={"query": f'query {{ case(id: "{case_id}") {{ id reference title status }} }}'},
        )
    # // then the governed fields round-trip with no errors
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["errors"] == []
    assert body["data"]["case"]["id"] == str(case_id)
    assert body["data"]["case"]["reference"] == "C-TEST-2026-000001"
    assert body["data"]["case"]["title"] == "Help with onboarding"
    assert body["data"]["case"]["status"] == "open"


def test_graphql_router_cross_tenant_case__returns_null_with_errors(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A tenant-A case queried from tenant B is null + errors, not a leak."""
    # // given a case that lives in tenant A
    case_id = _run(_seed_open_case(sql_session_factory, tenant_id=TENANT_A))
    app = _build_app(sql_session_factory, tenant_id=TENANT_B)
    # // when a tenant-B actor queries it by id
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/graphql",
            json={"query": f'query {{ case(id: "{case_id}") {{ id reference }} }}'},
        )
    # // then the branch is null with a single denial/miss error (no data leak)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["case"] is None
    assert len(body["errors"]) == 1
    assert body["errors"][0]["code"] in {"forbidden", "not_found"}
    assert "C-TEST-2026-000001" not in response.text


def test_graphql_router_missing_case__returns_null_with_errors(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An unknown case id in the actor's own tenant is null + errors."""
    # // given a seeded tenant with no matching case
    _run(_seed_open_case(sql_session_factory))
    app = _build_app(sql_session_factory)
    # // when the actor queries a random id
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/graphql",
            json={"query": f'query {{ case(id: "{MISSING_CASE_ID}") {{ id }} }}'},
        )
    # // then the response is null with a not_found error (same shape as cross-tenant)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["case"] is None
    assert len(body["errors"]) == 1
    assert body["errors"][0]["code"] == "not_found"


def test_graphql_router_unknown_field__rejected_with_validation_error(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An undeclared sub-selection is rejected without executing the read."""
    # // given an open case in the actor's tenant
    case_id = _run(_seed_open_case(sql_session_factory))
    app = _build_app(sql_session_factory)
    # // when the actor requests a field the schema does not declare
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/graphql",
            json={"query": f'query {{ case(id: "{case_id}") {{ secret }} }}'},
        )
    # // then the executor reports validation_failed and emits no case data
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["errors"]) == 1
    assert body["errors"][0]["code"] == "validation_failed"


def test_graphql_router_malformed_query__rejected_with_400(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A syntactically invalid query is a 400 parse failure."""
    # // given an app with no seeded data needed (parse fails first)
    app = _build_app(sql_session_factory)
    # // when the caller posts text that is not a query
    with TestClient(app) as client:
        response = client.post("/api/v1/graphql", json={"query": "this is not graphql {{{"})
    # // then the transport rejects it before any repository is touched
    assert response.status_code == 400, response.text


def test_graphql_router_mutation__rejected_with_400(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The governed surface stays read-only: mutations are rejected."""
    # // given an open case in the actor's tenant
    case_id = _run(_seed_open_case(sql_session_factory))
    app = _build_app(sql_session_factory)
    # // when the caller posts a mutation operation
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/graphql",
            json={"query": f'mutation {{ case(id: "{case_id}") {{ id }} }}'},
        )
    # // then the parser rejects it — writes stay on the REST API
    assert response.status_code == 400, response.text


def test_graphql_router_variables__rejected_with_400(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The executor inlines all arguments, so variables fail closed."""
    # // given an authorized app
    app = _build_app(sql_session_factory)
    # // when the caller supplies a variables payload
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/graphql",
            json={
                "query": "query { cases(limit: 10, offset: 0) { id } }",
                "variables": {"limit": 10},
            },
        )
    # // then the router rejects it instead of silently ignoring it
    assert response.status_code == 400, response.text


def test_graphql_router_unauthenticated__returns_401(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A caller without identity never reaches the executor."""
    # // given an app whose identity dependency denies the caller
    app = _build_app(sql_session_factory)

    def _deny() -> UserContext:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-Dev-User header",
        )

    app.dependency_overrides[get_current_user] = _deny
    # // when the anonymous caller posts a well-formed query
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/graphql",
            json={"query": "query { cases(limit: 10, offset: 0) { id } }"},
        )
    # // then the standard auth handler answers 401
    assert response.status_code == 401, response.text
