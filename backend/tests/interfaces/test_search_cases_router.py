"""SQLite endpoint coverage for ad-hoc case search + audit (FM-031 / FM-032).

Exercises every ad-hoc query path end-to-end through the production
provider composition (``SqlSearchHttpDependencyProvider``):

- ``GET /api/v1/cases/search`` — two-tenant isolation + audit row.
- ``GET /api/v1/search/cases`` — plan-contract alias, same guarantees.
- ``POST /api/v1/search`` — two-tenant isolation + audit row.
- ``POST /api/v1/search/semantic`` — two-tenant isolation + audit row.
- ``POST /api/v1/search/saved/{id}/run`` — saved reuse stays tenant-scoped.
- Non-searchable gating — a field declared ``searchable=False`` never
  appears in snippets and is rejected in filters with 400.
- Audit shape — every ad-hoc execution appends one ``search_audit``
  row with ``saved_search_id=None``, the canonical query shape, and
  the result count; sensitive filter values are ``ref`` markers, never
  content (no PHI in the audit row).

Naming follows AGENTS.md §4.R5: ``test_<unit>_<scenario>__<expectation>``
with ``// given`` / ``// when`` / ``// then`` structure. Seeding goes
through the SQL repositories so the P05 schema contract and the SQL
LIKE adapter are both on the path, exactly as in production.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.deps import UserContext, get_current_user, get_db
from app.domain.case import Case, CaseReference
from app.domain.case_type import CaseType, CaseTypeField, CaseTypeSchema, CaseTypeVersion
from app.domain.errors import DomainError
from app.infrastructure.persistence.models import SearchAudit as SearchAuditRow
from app.infrastructure.persistence.models import Tenant as TenantRow
from app.infrastructure.persistence.repositories import (
    SqlCaseRepository,
    SqlCaseTypeRepository,
    SqlCaseTypeVersionRepository,
)
from app.infrastructure.persistence.search_http_dependencies import (
    SqlSearchHttpDependencyProvider,
)
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.search import router
from app.interfaces.http.routers.search.common import _adhoc_audit_shape
from app.interfaces.http.schemas.search import SearchFilterRequest

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
INTAKE_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
INTAKE_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

SSN_A = "111-22-3333"
SSN_B = "999-88-7777"


def _run(coro: Any) -> Any:
    """Run an async seed/assert operation for the synchronous TestClient test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    roles: tuple[str, ...] = ("intake",),
    user_id: UUID = INTAKE_A,
    tenant_id: UUID = TENANT_A,
) -> FastAPI:
    """Build a minimal app using the production search provider composition."""

    async def _override_get_db() -> Any:
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.state.search_http_dependencies = SqlSearchHttpDependencyProvider()
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        display_name="Synthetic Intake",
        roles=roles,
    )
    return app


def _schema() -> CaseTypeSchema:
    """Published schema: searchable summary + searchable phi ssn + non-searchable code."""
    return CaseTypeSchema(
        fields=(
            CaseTypeField(
                key="summary",
                label="Summary",
                type="text",
                required=False,
                sensitivity="internal",
                searchable=True,
            ),
            CaseTypeField(
                key="ssn",
                label="SSN",
                type="text",
                required=False,
                sensitivity="phi",
                searchable=True,
            ),
            CaseTypeField(
                key="internal_code",
                label="Internal Code",
                type="text",
                required=False,
                sensitivity="internal",
                searchable=False,
            ),
        )
    )


async def _seed_tenant(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    short: str,
    actor_id: UUID,
    reference: str,
    title: str,
    summary: str,
    ssn: str,
) -> UUID:
    """Seed one tenant with a published type and one open case; return the case id."""
    session.add(TenantRow(id=tenant_id, key=short, name=f"Tenant {short}", created_at=FIXED_NOW))
    case_type = CaseType.create(
        tenant_id=tenant_id,
        key="ServiceRequest",
        name="ServiceRequest",
        description="",
        now=FIXED_NOW,
    )
    version = CaseTypeVersion.draft(
        tenant_id=tenant_id,
        case_type_id=case_type.id,
        version_number=1,
        schema=_schema(),
        now=FIXED_NOW,
    ).publish(actor_id=actor_id, at=FIXED_NOW)
    case_type = case_type.with_published_version(version)
    await SqlCaseTypeRepository(session).add(case_type)
    await SqlCaseTypeVersionRepository(session).add(version)
    case = Case.create(
        tenant_id=tenant_id,
        reference=CaseReference(reference),
        case_type_version=version,
        title=title,
        description="Synthetic two-tenant search fixture.",
        data={"summary": summary, "ssn": ssn, "internal_code": f"code-{short}"},
        created_by=actor_id,
        now=FIXED_NOW,
    )
    await SqlCaseRepository(session).add(case)
    return case.id


async def _seed_async(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, UUID]:
    """Seed tenants A + B, each with one open onboarding case."""
    async with session_factory() as session:
        case_a = await _seed_tenant(
            session,
            tenant_id=TENANT_A,
            short="TA",
            actor_id=INTAKE_A,
            reference="C-TEST-2026-000001",
            title="Alpha onboarding request",
            summary="alpha intake summary",
            ssn=SSN_A,
        )
        case_b = await _seed_tenant(
            session,
            tenant_id=TENANT_B,
            short="TB",
            actor_id=INTAKE_B,
            reference="C-TEST-2026-000002",
            title="Beta onboarding request",
            summary="beta intake summary",
            ssn=SSN_B,
        )
        await session.commit()
        return {"case_a": case_a, "case_b": case_b}


async def _audit_rows(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> list[SearchAuditRow]:
    """Return the tenant's ``search_audit`` rows, oldest first."""
    async with session_factory() as session:
        stmt = (
            select(SearchAuditRow)
            .where(SearchAuditRow.tenant_id == tenant_id)
            .order_by(SearchAuditRow.occurred_at, SearchAuditRow.id)
        )
        return list((await session.execute(stmt)).scalars().all())


def test_get_cases_search__other_tenant_case__never_appears(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GET /cases/search returns only the actor's tenant cases and audits the run."""
    # // given two tenants with identically shaped onboarding cases.
    ids = _run(_seed_async(sql_session_factory))
    app = _build_app(sql_session_factory)
    # // when the tenant-A actor searches free text matching both cases.
    with TestClient(app) as client:
        response = client.get("/api/v1/cases/search", params={"query": "onboarding"})
    # // then only tenant A's case appears — the B row is invisible.
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == [str(ids["case_a"])]
    assert body["total_considered"] == 1
    # // and one audit row carries the query shape plus the count, no PHI.
    rows = _run(_audit_rows(sql_session_factory, TENANT_A))
    assert len(rows) == 1
    assert rows[0].saved_search_id is None
    assert rows[0].query_shape["text_query"] == {"redacted": True, "reason": "free_text"}
    assert rows[0].result_count == 1
    assert SSN_A not in json.dumps(rows[0].query_shape)
    assert _run(_audit_rows(sql_session_factory, TENANT_B)) == []


def test_get_search_cases_alias__other_tenant_case__never_appears(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GET /search/cases (plan path) behaves exactly like GET /cases/search."""
    # // given the same two-tenant fixture.
    ids = _run(_seed_async(sql_session_factory))
    app = _build_app(sql_session_factory)
    # // when the tenant-A actor uses the plan-contract alias path.
    with TestClient(app) as client:
        response = client.get("/api/v1/search/cases", params={"query": "onboarding"})
    # // then the result set and the audit row match the canonical path.
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == [str(ids["case_a"])]
    assert body["total_considered"] == 1
    rows = _run(_audit_rows(sql_session_factory, TENANT_A))
    assert len(rows) == 1
    assert rows[0].saved_search_id is None
    assert rows[0].result_count == 1


def test_post_search__other_tenant_case__never_appears(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """POST /search with a status filter returns only the actor's tenant cases."""
    # // given the two-tenant fixture (both cases are open).
    ids = _run(_seed_async(sql_session_factory))
    app = _build_app(sql_session_factory)
    # // when the tenant-A actor posts a fielded filter matching both tenants.
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/search",
            json={"query": None, "filters": [{"field": "status", "op": "eq", "value": "open"}]},
        )
    # // then only tenant A's case is returned and audited.
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == [str(ids["case_a"])]
    assert body["total_considered"] == 1
    rows = _run(_audit_rows(sql_session_factory, TENANT_A))
    assert len(rows) == 1
    assert rows[0].query_shape["filters"] == [
        {
            "field": "status",
            "op": "eq",
            "value": {"redacted": True, "reason": "query_value"},
        }
    ]
    assert rows[0].result_count == 1
    assert _run(_audit_rows(sql_session_factory, TENANT_B)) == []


def test_post_search_semantic__other_tenant_case__never_appears(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """POST /search/semantic reorders but never widens across tenants."""
    # // given the two-tenant fixture.
    ids = _run(_seed_async(sql_session_factory))
    app = _build_app(sql_session_factory)
    # // when the tenant-A actor runs the AI-ranked search.
    with TestClient(app) as client:
        response = client.post("/api/v1/search/semantic", json={"query": "onboarding"})
    # // then the candidate set stays tenant-scoped and the run is audited.
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == [str(ids["case_a"])]
    rows = _run(_audit_rows(sql_session_factory, TENANT_A))
    assert len(rows) == 1
    assert rows[0].saved_search_id is None
    assert rows[0].result_count == 1


def test_run_saved_search__other_tenant_case__never_appears(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Re-executing a saved search returns only the owner's tenant cases."""
    # // given the two-tenant fixture.
    ids = _run(_seed_async(sql_session_factory))
    app = _build_app(sql_session_factory)
    with TestClient(app) as client:
        # // when the tenant-A actor saves a status filter and re-runs it.
        saved = client.post(
            "/api/v1/search/saved",
            json={
                "name": "open cases",
                "filters": [{"field": "status", "op": "eq", "value": "open"}],
            },
        )
        assert saved.status_code == 201, saved.text
        run = client.post(f"/api/v1/search/saved/{saved.json()['id']}/run")
    # // then the live re-execution stays inside tenant A.
    assert run.status_code == 200, run.text
    assert [item["id"] for item in run.json()["items"]] == [str(ids["case_a"])]


def test_search__non_searchable_field__excluded_from_snippets_and_filters(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Marking a field non-searchable removes it from snippets and filters."""
    # // given a case whose data holds a non-searchable internal_code.
    _run(_seed_async(sql_session_factory))
    app = _build_app(sql_session_factory)
    with TestClient(app) as client:
        # // when the actor searches free text matching the case.
        response = client.post("/api/v1/search", json={"query": "Alpha"})
        assert response.status_code == 200, response.text
        snippet = response.json()["items"][0]["data_snippet"]
        # // then the searchable summary passes through ...
        assert snippet["summary"] == "alpha intake summary"
        # // ... the phi ssn is a ref marker for the intake actor ...
        assert snippet["ssn"] == {"redacted": True, "reason": "sensitive", "ref": "ssn"}
        # // ... and the non-searchable field is absent from the snippet.
        assert "internal_code" not in snippet
        # // and filtering on it is rejected as a bad request.
        rejected = client.post(
            "/api/v1/search",
            json={"filters": [{"field": "data.internal_code", "op": "eq", "value": "x"}]},
        )
        assert rejected.status_code == 400, rejected.text


def test_search__every_execution__writes_audit_with_shape_count_and_no_phi(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A search filtering on a sensitive value audits the shape, not the value."""
    # // given the two-tenant fixture.
    _run(_seed_async(sql_session_factory))
    app = _build_app(sql_session_factory)
    with TestClient(app) as client:
        # // when the actor filters on the phi ssn value itself.
        response = client.post(
            "/api/v1/search",
            json={"filters": [{"field": "data.ssn", "op": "like", "value": SSN_A}]},
        )
    # // then the search still executes against the tenant's own rows ...
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_considered"] == 1
    # // ... and the audit row carries the shape plus the count with no PHI.
    rows = _run(_audit_rows(sql_session_factory, TENANT_A))
    assert len(rows) == 1
    shape = rows[0].query_shape
    assert shape["filters"] == [
        {
            "field": "data.ssn",
            "op": "like",
            "value": {"redacted": True, "reason": "sensitive", "ref": "ssn"},
        }
    ]
    assert rows[0].result_count == body["total_considered"] == 1
    assert SSN_A not in json.dumps(shape)


def test_adhoc_search_audit_shape__validation_fallback__redacts_values() -> None:
    # given: a validation mismatch reaches the defensive audit fallback
    text_value = "name@example.test"
    filter_value = "private free-form content"
    # when: the audit shape is projected with a non-searchable field
    shape = _adhoc_audit_shape(
        tenant_id=TENANT_A,
        actor_id=INTAKE_A,
        text_query=text_value,
        filters=[SearchFilterRequest(field="data.internal_code", op="eq", value=filter_value)],
        searchable_keys=(),
        sensitive_keys=(),
    )
    # then: the fallback keeps shape metadata and stores no submitted values
    assert shape["text_query"] == {"redacted": True, "reason": "free_text"}
    assert shape["filters"] == [
        {
            "field": "data.internal_code",
            "op": "eq",
            "value": {"redacted": True, "reason": "query_value"},
        }
    ]
    assert text_value not in json.dumps(shape)
    assert filter_value not in json.dumps(shape)
