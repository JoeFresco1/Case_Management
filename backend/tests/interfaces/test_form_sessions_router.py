"""SQLite endpoint coverage for multi-step form sessions (FM-038 / P07-US6).

Exercises the production provider composition
(``SqlFormsHttpDependencyProvider`` with ``SqlFormSessionRepository``)
end-to-end: start → save step → resume → idempotent re-start →
stale-token conflict. Proves the session rows persist across requests
(the router commits) and the ``If-Match`` token is enforced.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.deps import UserContext, get_current_user, get_db
from app.domain.case import Case, CaseReference, CaseStatus
from app.domain.errors import DomainError
from app.domain.form import (
    FormBinding,
    FormDefinition,
    FormFieldSpec,
    FormSectionSpec,
    FormVersion,
)
from app.infrastructure.persistence.forms_http_dependencies import (
    SqlFormsHttpDependencyProvider,
)
from app.infrastructure.persistence.repositories import SqlCaseRepository
from app.infrastructure.persistence.repositories.form_repository import (
    SqlFormBindingRepository,
    SqlFormVersionRepository,
)
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.forms import router

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
WORKER_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
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
    roles: tuple[str, ...] = ("reviewer",),
    user_id: UUID = WORKER_ID,
    tenant_id: UUID = TENANT_A,
) -> FastAPI:
    """Build a minimal app using the production provider composition."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.state.forms_http_dependencies = SqlFormsHttpDependencyProvider()
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        display_name="Synthetic Worker",
        roles=roles,
    )
    return app


def _two_step_definition() -> FormDefinition:
    """A published two-step form: ``basics`` then ``review``."""
    sections = (
        FormSectionSpec(
            key="basics",
            label="Basics",
            fields=(FormFieldSpec(key="title", label="Title", required=True),),
        ),
        FormSectionSpec(
            key="review",
            label="Review",
            fields=(FormFieldSpec(key="decision", label="Decision", required=True),),
        ),
    )
    steps = (
        {"step_key": "basics", "label": "Basics", "section_keys": ["basics"]},
        {"step_key": "review", "label": "Review", "section_keys": ["review"]},
    )
    return FormDefinition(sections=sections, validations=(), steps=steps)


async def _seed_case_and_form(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, str]:
    """Stage an open case plus a published two-step binding; return ids."""
    case_id = uuid4()
    version_id = uuid4()
    async with session_factory() as session:
        await SqlCaseRepository(session).add(
            Case(
                id=case_id,
                tenant_id=TENANT_A,
                reference=CaseReference("C-ACME-2026-000001"),
                case_type_version_id=version_id,
                status=CaseStatus.OPEN,
                priority="normal",
                title="Synthetic",
                description="",
                data={},
                current_workflow_state=None,
                version_token=1,
                created_at=NOW,
                updated_at=NOW,
                created_by=WORKER_ID,
            )
        )
        form_version = FormVersion.draft(
            tenant_id=TENANT_A,
            key="MultiStep",
            version_number=1,
            definition=_two_step_definition(),
            now=NOW,
        ).publish(actor_id=WORKER_ID, at=NOW)
        await SqlFormVersionRepository(session).add(form_version)
        binding = FormBinding.draft(
            tenant_id=TENANT_A,
            case_type_version_id=version_id,
            form_version_id=form_version.id,
            now=NOW,
        ).publish(actor_id=WORKER_ID, at=NOW)
        await SqlFormBindingRepository(session).add(binding)
        await session.commit()
    return case_id, "MultiStep"


def test_form_sessions__start_save_resume__round_trips_through_sql(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Start → save step → resume returns persisted progress (FM-038)."""
    case_id, form_key = _run(_seed_case_and_form(sql_session_factory))
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        started = client.post(
            f"/api/v1/forms/cases/{case_id}/sessions",
            json={"form_key": form_key},
            headers={"Idempotency-Key": "legacy-start-1"},
        )
        assert started.status_code == 200, started.text
        session_id = started.json()["id"]
        assert started.json()["current_step_index"] == 0

        saved = client.patch(
            f"/api/v1/forms/cases/{case_id}/sessions/{session_id}/steps/basics",
            json={
                "step_key": "basics",
                "data": {"title": "Draft title"},
                "expected_version_token": 1,
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["completed_steps"] == ["basics"]
        assert saved.json()["version_token"] == 2

        resumed = client.get(
            f"/api/v1/forms/cases/{case_id}/sessions",
            params={"form_key": form_key},
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["id"] == session_id
        assert resumed.json()["step_data"] == {"title": "Draft title"}


def test_form_sessions__second_start__returns_same_session(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Re-starting for the same triple resumes (no duplicate rows)."""
    case_id, form_key = _run(_seed_case_and_form(sql_session_factory))
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        first = client.post(
            f"/api/v1/forms/cases/{case_id}/sessions",
            json={"form_key": form_key},
            headers={"Idempotency-Key": "legacy-start-2a"},
        )
        second = client.post(
            f"/api/v1/forms/cases/{case_id}/sessions",
            json={"form_key": form_key},
            headers={"Idempotency-Key": "legacy-start-2b"},
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] == second.json()["id"]


def test_form_sessions__stale_token__conflicts(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A save echoing a superseded token is a 409, not a silent overwrite."""
    case_id, form_key = _run(_seed_case_and_form(sql_session_factory))
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        started = client.post(
            f"/api/v1/forms/cases/{case_id}/sessions",
            json={"form_key": form_key},
            headers={"Idempotency-Key": "legacy-start-3"},
        )
        session_id = started.json()["id"]
        first_save = client.patch(
            f"/api/v1/forms/cases/{case_id}/sessions/{session_id}/steps/basics",
            json={
                "step_key": "basics",
                "data": {"title": "v1"},
                "expected_version_token": 1,
            },
        )
        assert first_save.status_code == 200, first_save.text
        stale_save = client.patch(
            f"/api/v1/forms/cases/{case_id}/sessions/{session_id}/steps/basics",
            json={
                "step_key": "basics",
                "data": {"title": "stale"},
                "expected_version_token": 1,
            },
        )

    assert stale_save.status_code == 409, stale_save.text


def test_form_sessions__cross_tenant_actor__forbidden(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A tenant-B actor cannot start sessions on tenant-A cases."""
    case_id, form_key = _run(_seed_case_and_form(sql_session_factory))
    app = _build_app(sql_session_factory, tenant_id=TENANT_B)

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/forms/cases/{case_id}/sessions",
            json={"form_key": form_key},
            headers={"Idempotency-Key": "legacy-start-4"},
        )

    assert response.status_code in (403, 404), response.text
