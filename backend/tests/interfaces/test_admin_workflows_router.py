"""SQLite endpoint coverage for the admin workflow dependency boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.authorization import Action
from app.application.workflow.http_dependencies import (
    AdminWorkflowsHttpRepositories,
)
from app.deps import UserContext, get_current_user, get_db
from app.domain.errors import DomainError
from app.domain.workflow import (
    WorkflowDefinition,
    WorkflowState,
    WorkflowTransition,
    WorkflowVersion,
)
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.admin_workflows import router

TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")
ADMIN_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
WORKFLOW_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


def _run(coro: Any) -> Any:
    """Run an async seed operation for the synchronous TestClient test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _RoleRepository:
    """Authorize the synthetic process administrator in this endpoint test."""

    async def permissions_for_user(self, tenant_id: UUID, user_id: UUID) -> frozenset[str]:
        """Return the workflow-publish permission for the test actor."""
        assert tenant_id == TENANT_ID
        assert user_id == ADMIN_ID
        return frozenset({Action.WORKFLOW_PUBLISH.value})


class _WorkflowRepository:
    """Expose one synthetic workflow definition to the endpoint."""

    def __init__(self, workflow: WorkflowDefinition) -> None:
        self.workflow = workflow

    async def get(self, tenant_id: UUID, workflow_definition_id: UUID) -> WorkflowDefinition | None:
        """Return the workflow when both tenant and id match."""
        if tenant_id == self.workflow.tenant_id and workflow_definition_id == self.workflow.id:
            return self.workflow
        return None


class _WorkflowVersionRepository:
    """Expose one synthetic workflow version to the endpoint."""

    def __init__(self, version: WorkflowVersion) -> None:
        self.version = version

    async def get_by_version_number(
        self, tenant_id: UUID, workflow_definition_id: UUID, version_number: int
    ) -> WorkflowVersion | None:
        """Return the version when all lookup keys match."""
        if (
            tenant_id == self.version.tenant_id
            and workflow_definition_id == self.version.workflow_definition_id
            and version_number == self.version.version_number
        ):
            return self.version
        return None


class _QueueRepository:
    """Provide the empty queue catalog required by the simulator path."""

    async def list_keys(self, tenant_id: UUID) -> list[str]:
        """Return no queue keys for a workflow with no queue actions."""
        assert tenant_id == TENANT_ID
        return []


class _Provider:
    """Build a workflow contract bundle while the request uses SQLite."""

    def __init__(self, repositories: AdminWorkflowsHttpRepositories) -> None:
        self.repositories = repositories

    def for_session(self, session: object) -> AdminWorkflowsHttpRepositories:
        """Return the test ports; the SQLite session remains request-scoped."""
        assert isinstance(session, AsyncSession)
        return self.repositories


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
    provider: _Provider,
) -> FastAPI:
    """Build a minimal app using the router's state-based resolver."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.state.admin_workflows_http_dependencies = provider
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=ADMIN_ID,
        tenant_id=TENANT_ID,
        display_name="Synthetic Admin",
        roles=("process_admin",),
    )
    return app


def test_admin_workflows_router__simulate_with_sqlite_session__returns_trace(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The simulation endpoint resolves its ports without importing infrastructure."""
    workflow = WorkflowDefinition(
        id=WORKFLOW_ID,
        tenant_id=TENANT_ID,
        key="ServiceRequest",
        name="Service Request",
    )
    version = WorkflowVersion.draft(
        tenant_id=TENANT_ID,
        workflow_definition_id=WORKFLOW_ID,
        version_number=1,
        states=(
            WorkflowState(key="intake", label="Intake", is_initial=True),
            WorkflowState(key="closed", label="Closed", is_terminal=True),
        ),
        transitions=(
            WorkflowTransition(
                key="close",
                from_state="intake",
                to_state="closed",
                human_driven=True,
            ),
        ),
    )
    provider = _Provider(
        AdminWorkflowsHttpRepositories(
            workflow=_WorkflowRepository(workflow),
            workflow_version=_WorkflowVersionRepository(version),
            queue=_QueueRepository(),
            audit=object(),
            role=_RoleRepository(),
        )
    )
    app = _build_app(sql_session_factory, provider)

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/admin/workflows/{WORKFLOW_ID}/versions/1/simulate",
            json={"synthetic_case": {}, "actions": [{"transition_key": "close"}]},
        )

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "completed"
    assert response.json()["steps"][0]["transition"] == "close"
