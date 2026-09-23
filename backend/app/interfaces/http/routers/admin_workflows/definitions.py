"""Workflow definition header routes: create and list.

# Why this module (R2 structural split)

Route group extracted from the former single-file
``admin_workflows.py`` (722 lines). This module owns the definition
*header* surface:

- ``POST /api/v1/admin/workflows`` — create an empty workflow header;
  versions are added separately (FM-013 / FR-015, P02-US2).
- ``GET /api/v1/admin/workflows`` — list workflow headers visible to
  the current tenant administrator, including each header's highest
  version number via the read projection.

# Authorization

Both endpoints require ``Action.WORKFLOW_PUBLISH`` (granted to the
``process_admin`` static role); fail-closed → ``403 Forbidden``.
Both endpoints are tenant-scoped through ``user.tenant_id``.

# INTEGRATION_SEAM: HTTP entry

Business decisions delegate to ``app.application.workflow.create_workflow``;
reads go through the ``WorkflowReadRepository`` projection resolved by
:func:`app.interfaces.http.routers.admin_workflows.common._read_repository`.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.workflow import create_workflow
from app.application.workflow.http_dependencies import AdminWorkflowsHttpDependencyProvider
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.interfaces.http.dependencies import get_admin_workflows_dependencies
from app.interfaces.http.schemas.admin_workflows import (
    CreateWorkflowRequest,
    WorkflowDefinitionResponse,
    WorkflowSummaryResponse,
)

from .common import _read_repository, router


@router.post(
    "",
    response_model=WorkflowDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workflow definition header",
)
async def create_workflow_definition(
    body: CreateWorkflowRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminWorkflowsHttpDependencyProvider = Depends(get_admin_workflows_dependencies),
) -> WorkflowDefinitionResponse:
    """Create an empty workflow header; versions are added separately."""
    repositories = dependencies.for_session(db)
    await authorize(
        Action.WORKFLOW_PUBLISH,
        user,
        Resource(tenant_id=user.tenant_id, kind="workflow"),
        role_repository=repositories.role,
    )
    workflow = await create_workflow.handle(
        create_workflow.CreateWorkflowCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            key=body.key,
            name=body.name,
            description=body.description,
            correlation_id=correlation.correlation_id,
        ),
        workflow_repo=repositories.workflow,
        audit_store=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return WorkflowDefinitionResponse(
        id=workflow.id,
        tenant_id=workflow.tenant_id,
        key=workflow.key,
        name=workflow.name,
        description=workflow.description,
        current_version_id=workflow.current_version_id,
    )


@router.get("", response_model=list[WorkflowSummaryResponse], summary="List workflows")
async def list_workflows(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AdminWorkflowsHttpDependencyProvider = Depends(get_admin_workflows_dependencies),
) -> list[WorkflowSummaryResponse]:
    """List workflow headers visible to the current tenant administrator."""
    repositories = dependencies.for_session(db)
    await authorize(
        Action.WORKFLOW_PUBLISH,
        user,
        Resource(tenant_id=user.tenant_id, kind="workflow"),
        role_repository=repositories.role,
    )
    reader = _read_repository(repositories)
    workflows = await reader.list_workflows(user.tenant_id)
    version_numbers: dict[UUID, int | None] = {}
    for workflow in workflows:
        versions = await reader.list_versions(user.tenant_id, workflow.id)
        version_numbers[workflow.id] = max(
            (version.version_number for version in versions), default=None
        )
    return [
        WorkflowSummaryResponse(
            id=workflow.id,
            key=workflow.key,
            name=workflow.name,
            description=workflow.description,
            current_version_id=workflow.current_version_id,
            current_version_number=version_numbers[workflow.id],
        )
        for workflow in workflows
    ]
