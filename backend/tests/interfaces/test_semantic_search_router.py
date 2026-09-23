"""SQLite endpoint coverage for the semantic search HTTP boundary (FM-034).

Exercises ``POST /api/v1/search/semantic`` end-to-end through the
production provider composition (``SqlSearchHttpDependencyProvider``):

- an authorized actor gets the hit back with the ranker's ordering applied;
- the default no-op ranker preserves the structured order (FM-050);
- an actor without ``case.read`` is denied with 403 (fail-closed);
- a filter on a non-searchable field is rejected with 400.

Naming follows AGENTS.md §4.R5: ``test_<unit>_<scenario>__<expectation>``
with ``// given`` / ``// when`` / ``// then`` structure. Seeding goes
through the SQL repositories (``SqlCaseRepository`` + case-type repos)
so the P05 schema contract and the SQL LIKE adapter are both on the
path, exactly as in production.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ai.search_ranking import RankedCandidate, RankingHints, RankingResult
from app.application.search.http_dependencies import SearchHttpDependencyProvider
from app.deps import UserContext, get_current_user, get_db
from app.domain.case import Case, CaseReference
from app.domain.case_type import CaseType, CaseTypeField, CaseTypeSchema, CaseTypeVersion
from app.domain.errors import DomainError
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

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
INTAKE_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def _run(coro: Any) -> Any:
    """Run an async seed operation for the synchronous TestClient test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _ReversingRanker:
    """Test-only ranker that reverses the authorized candidate order.

    Proves the endpoint actually consults the ``SearchRanker`` port:
    with two seeded cases the wire order must be the exact reverse of
    the structured (``created_at`` desc) order.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.last_candidate_ids: tuple[UUID, ...] = ()

    async def rank(
        self,
        *,
        candidate_ids: tuple[UUID, ...],
        hints: RankingHints,
    ) -> RankingResult:
        """Return the candidates in reverse order with high confidence."""
        self.calls += 1
        self.last_candidate_ids = candidate_ids
        ordered = tuple(reversed(candidate_ids))
        return RankingResult(
            candidates=tuple(
                RankedCandidate(case_id=cid, confidence=0.9, rank=idx)
                for idx, cid in enumerate(ordered)
            ),
            model_id="test-reverser",
            confidence=0.9,
        )


class _RankerOverrideProvider:
    """Wrap the SQL provider while swapping only the ranker (test seam)."""

    def __init__(self, inner: SearchHttpDependencyProvider, ranker: _ReversingRanker) -> None:
        self._inner = inner
        self._ranker = ranker

    def for_session(self, session: object) -> Any:
        """Return the SQL bundle with the test ranker spliced in."""
        return replace(self._inner.for_session(session), ranker=self._ranker)


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    roles: tuple[str, ...] = ("intake",),
    user_id: UUID = INTAKE_ID,
    tenant_id: UUID = TENANT_A,
    ranker: _ReversingRanker | None = None,
) -> FastAPI:
    """Build a minimal app using the production search provider composition."""

    async def _override_get_db() -> Any:
        async with session_factory() as session:
            yield session

    provider: SearchHttpDependencyProvider = SqlSearchHttpDependencyProvider()
    if ranker is not None:
        provider = _RankerOverrideProvider(provider, ranker)
    app = FastAPI()
    app.state.search_http_dependencies = provider
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


async def _seed_async(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, UUID]:
    """Seed tenant + published type with a searchable field + two open cases."""
    async with session_factory() as session:
        session.add(
            TenantRow(
                id=TENANT_A,
                key="TA",
                name="Tenant A",
                created_at=FIXED_NOW,
            )
        )
        case_type = CaseType.create(
            tenant_id=TENANT_A,
            key="ServiceRequest",
            name="ServiceRequest",
            description="",
            now=FIXED_NOW,
        )
        schema = CaseTypeSchema(
            fields=(
                CaseTypeField(
                    key="summary",
                    label="Summary",
                    type="text",
                    required=False,
                    sensitivity="internal",
                    searchable=True,
                ),
            )
        )
        version = CaseTypeVersion.draft(
            tenant_id=TENANT_A,
            case_type_id=case_type.id,
            version_number=1,
            schema=schema,
            now=FIXED_NOW,
        ).publish(actor_id=INTAKE_ID, at=FIXED_NOW)
        case_type = case_type.with_published_version(version)
        await SqlCaseTypeRepository(session).add(case_type)
        await SqlCaseTypeVersionRepository(session).add(version)
        old = Case.create(
            tenant_id=TENANT_A,
            reference=CaseReference("C-TEST-2026-000001"),
            case_type_version=version,
            title="Alpha onboarding request",
            description="First synthetic case.",
            data={"summary": "alpha intake summary"},
            created_by=INTAKE_ID,
            now=FIXED_NOW,
        )
        new = Case.create(
            tenant_id=TENANT_A,
            reference=CaseReference("C-TEST-2026-000002"),
            case_type_version=version,
            title="Beta onboarding request",
            description="Second synthetic case.",
            data={"summary": "beta intake summary"},
            created_by=INTAKE_ID,
            now=FIXED_NOW + timedelta(minutes=5),
        )
        await SqlCaseRepository(session).add(old)
        await SqlCaseRepository(session).add(new)
        await session.commit()
        return {"old": old.id, "new": new.id}


def test_semantic_search_authorized__returns_hit_with_ranking_applied(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Authorized search returns hits in the ranker's (reversed) order."""
    # // given a tenant with a searchable field and two open cases.
    ids = _run(_seed_async(sql_session_factory))
    ranker = _ReversingRanker()
    app = _build_app(sql_session_factory, ranker=ranker)
    # // when the actor runs a semantic search matching both cases.
    with TestClient(app) as client:
        response = client.post("/api/v1/search/semantic", json={"query": "onboarding"})
    # // then the page carries both hits in the ranker's reversed order.
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == [str(ids["old"]), str(ids["new"])]
    assert ranker.calls == 1
    assert set(ranker.last_candidate_ids) == {ids["old"], ids["new"]}


def test_semantic_search_noop_ranker__preserves_structured_order(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With the default no-op ranker the structured order is preserved."""
    # // given the same seeded tenant and cases.
    ids = _run(_seed_async(sql_session_factory))
    app = _build_app(sql_session_factory)
    # // when the actor searches with a free-text query matching one case.
    with TestClient(app) as client:
        response = client.post("/api/v1/search/semantic", json={"query": "Alpha"})
    # // then the authorized hit is returned (noop keeps SQL order).
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == [str(ids["old"])]
    assert body["items"][0]["reference"] == "C-TEST-2026-000001"


def test_semantic_search_unauthorized__denies_with_forbidden(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An actor without ``case.read`` gets 403 (fail-closed)."""
    # // given seeded cases and an actor carrying no roles.
    _run(_seed_async(sql_session_factory))
    app = _build_app(sql_session_factory, roles=())
    # // when the role-less actor runs a semantic search.
    with TestClient(app) as client:
        response = client.post("/api/v1/search/semantic", json={"query": "Alpha"})
    # // then the deterministic authorization gate denies the request.
    assert response.status_code == 403, response.text


def test_semantic_search_non_searchable_filter__rejects_with_bad_request(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A filter on a non-searchable field gets 400 like the standard path."""
    # // given seeded cases where only ``summary`` is declared searchable.
    _run(_seed_async(sql_session_factory))
    app = _build_app(sql_session_factory)
    # // when the actor filters on an undeclared ``data.private_note`` key.
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/search/semantic",
            json={
                "query": None,
                "filters": [{"field": "data.private_note", "op": "eq", "value": "x"}],
            },
        )
    # // then the P05 schema contract rejects the filter as a bad request.
    assert response.status_code == 400, response.text
