"""HTTP acceptance tests for canonical workflow administration paths."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.workflow.http_dependencies import AdminWorkflowsHttpRepositories
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

TENANT = UUID("11111111-1111-1111-1111-111111111111")
ADMIN = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


class _Role:
    async def permissions_for_user(self, tenant_id: UUID, user_id: UUID) -> frozenset[str]:
        """Grant only the workflow administration permission."""
        _ = tenant_id, user_id
        return frozenset({"workflow.publish"})


class _Audit:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def append(self, **kwargs: Any) -> None:
        """Capture event types without persisting test data."""
        self.events.append(kwargs["event_type"])


class _Workflow:
    def __init__(self) -> None:
        self.items: dict[UUID, WorkflowDefinition] = {}

    async def get(self, tenant_id: UUID, workflow_id: UUID) -> WorkflowDefinition | None:
        """Return a tenant-scoped definition."""
        value = self.items.get(workflow_id)
        return value if value and value.tenant_id == tenant_id else None

    async def get_by_key(self, tenant_id: UUID, key: str) -> WorkflowDefinition | None:
        """Return a tenant-scoped definition by key."""
        return next(
            (v for v in self.items.values() if v.tenant_id == tenant_id and v.key == key), None
        )

    async def add(self, value: WorkflowDefinition) -> None:
        """Stage a definition header."""
        self.items[value.id] = value

    async def set_current_version(
        self, tenant_id: UUID, workflow_id: UUID, version_id: UUID
    ) -> None:
        """Promote the effective version."""
        value = self.items[workflow_id]
        self.items[workflow_id] = replace(value, current_version_id=version_id)


class _Versions:
    def __init__(self, version: WorkflowVersion) -> None:
        self.items = {version.id: version}

    async def get(self, tenant_id: UUID, version_id: UUID) -> WorkflowVersion | None:
        """Return a tenant-scoped version."""
        value = self.items.get(version_id)
        return value if value and value.tenant_id == tenant_id else None

    async def get_by_version_number(
        self, tenant_id: UUID, workflow_id: UUID, number: int
    ) -> WorkflowVersion | None:
        """Return a version by its stable number."""
        return next(
            (
                v
                for v in self.items.values()
                if v.tenant_id == tenant_id
                and v.workflow_definition_id == workflow_id
                and v.version_number == number
            ),
            None,
        )

    async def mark_status(
        self, tenant_id: UUID, version_id: UUID, *, new_status, published_at, published_by
    ) -> None:
        """Apply the publish status in the fake repository."""
        value = self.items[version_id]
        self.items[version_id] = value.publish(actor_id=published_by, at=published_at)


class _Queue:
    async def list_keys(self, tenant_id: UUID) -> list[str]:
        """Provide the empty queue catalog for this workflow."""
        _ = tenant_id
        return []


class _Provider:
    def __init__(self, repositories: AdminWorkflowsHttpRepositories) -> None:
        self.repositories = repositories

    def for_session(self, session: object) -> AdminWorkflowsHttpRepositories:
        """Return the test repository bundle."""
        return self.repositories


def _app(factory: async_sessionmaker[AsyncSession], provider: _Provider) -> FastAPI:
    """Build a minimal app around the admin router."""
    app = FastAPI()

    async def db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.include_router(router, prefix="/api/v1")
    app.state.admin_workflows_http_dependencies = provider
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=ADMIN, tenant_id=TENANT, display_name="Synthetic Admin", roles=("process_admin",)
    )
    return app


def test_admin_workflows_lifecycle__canonical_paths__create_validate_publish_replay(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Canonical endpoints create, validate, publish, and replay safely."""
    workflow_repo = _Workflow()
    workflow = WorkflowDefinition.create(
        tenant_id=TENANT, key="ServiceRequest", name="Service Request"
    )
    workflow_repo.items[workflow.id] = workflow
    version = WorkflowVersion.draft(
        tenant_id=TENANT,
        workflow_definition_id=workflow.id,
        version_number=1,
        states=(
            WorkflowState(key="intake", label="Intake", is_initial=True),
            WorkflowState(key="closed", label="Closed", is_terminal=True),
        ),
        transitions=(
            WorkflowTransition(
                key="close", from_state="intake", to_state="closed", human_driven=True
            ),
        ),
        now=datetime(2026, 9, 1, tzinfo=UTC),
    )
    audit = _Audit()
    versions = _Versions(version)
    provider = _Provider(
        AdminWorkflowsHttpRepositories(
            workflow=workflow_repo,
            workflow_version=versions,
            queue=_Queue(),
            audit=audit,
            role=_Role(),
        )
    )
    app = _app(sql_session_factory, provider)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/admin/workflows", json={"key": "NewFlow", "name": "New Flow"}
        )
        validated = client.post(f"/api/v1/admin/workflows/{workflow.id}/versions/1/validate")
        published = client.post(f"/api/v1/admin/workflows/{workflow.id}/versions/1/publish")
        replay = client.post(f"/api/v1/admin/workflows/{workflow.id}/versions/1/publish")

    assert created.status_code == 201
    assert validated.status_code == 200
    assert published.status_code == replay.status_code == 200
    assert published.json() == replay.json()
    assert audit.events.count("ConfigurationPublished") == 1
