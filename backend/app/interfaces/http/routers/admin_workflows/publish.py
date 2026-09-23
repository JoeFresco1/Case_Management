"""Workflow publish routes: canonical (id + number) and keyed draft publish.

# Why this module (R2 structural split)

Route group extracted from the former single-file
``admin_workflows.py`` (722 lines). This module owns both publish
endpoints:

- ``POST /api/v1/admin/workflows/{id}/versions/{n}/publish`` — publish
  by stable workflow id + version number; replay is successful
  (idempotent).
- ``POST /api/v1/admin/workflows/{key}/versions/{version_id}/publish``
  — validate + publish a draft addressed by key + version id.
  Re-publishing a published version raises ``InvariantViolation``
  because the row's status is no longer ``draft`` (FR-015 / AC-007).

# Why handler order matters here

Both handlers decorate the *identical* Starlette path pattern
``POST /{a}/versions/{b}/publish`` (path params are ``[^/]+`` at the
routing layer; the int/UUID distinction happens only during parameter
validation). Starlette resolves first-match, so the canonical
``version_number`` handler must stay defined **first** — exactly as in
the original single file — otherwise every canonical publish request
would be captured by the version-id route and rejected with ``422``.

# Authorization & transaction shape

Both endpoints require ``Action.WORKFLOW_PUBLISH`` (granted to the
``process_admin`` static role); fail-closed → ``403 Forbidden``. Each
runs inside the same DB transaction as its
``ConfigurationPublished`` audit event so the status flip, the
current-version pointer, and the audit row commit atomically
(Constitution IV / plan §4.R12); the HTTP boundary commits once after
the use case returns.

# INTEGRATION_SEAM: HTTP entry

Business decisions delegate to ``app.application.workflow.publish_workflow``;
persistence reaches the process through the
``AdminWorkflowsHttpDependencyProvider`` session bundle.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.workflow import publish_workflow
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
from app.domain.errors import NotFound
from app.interfaces.http.dependencies import get_admin_workflows_dependencies
from app.interfaces.http.schemas.admin_workflows import PublishWorkflowResultResponse

from .common import router


@router.post(
    "/{workflow_id}/versions/{version_number}/publish",
    response_model=PublishWorkflowResultResponse,
    summary="Publish a workflow version idempotently",
)
async def publish_canonical_workflow_version(
    workflow_id: UUID,
    version_number: int = Path(ge=1),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminWorkflowsHttpDependencyProvider = Depends(get_admin_workflows_dependencies),
) -> PublishWorkflowResultResponse:
    """Publish by stable workflow id/version number; replay is successful."""
    repositories = dependencies.for_session(db)
    await authorize(
        Action.WORKFLOW_PUBLISH,
        user,
        Resource(tenant_id=user.tenant_id, kind="workflow"),
        role_repository=repositories.role,
    )
    workflow = await repositories.workflow.get(user.tenant_id, workflow_id)
    version = await repositories.workflow_version.get_by_version_number(
        user.tenant_id, workflow_id, version_number
    )
    if workflow is None or version is None:
        raise NotFound("workflow version not found")
    result = await publish_workflow.handle(
        publish_workflow.PublishWorkflowVersionCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            workflow_version_id=version.id,
            correlation_id=correlation.correlation_id,
        ),
        workflow_repo=repositories.workflow,
        workflow_version_repo=repositories.workflow_version,
        queue_repo=repositories.queue,
        audit_store=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return PublishWorkflowResultResponse(
        workflow_definition_id=result.workflow_definition_id,
        workflow_version_id=result.workflow_version_id,
        version_number=result.version_number,
        status=result.status.value,
    )


@router.post(
    "/{workflow_key}/versions/{version_id}/publish",
    response_model=PublishWorkflowResultResponse,
    status_code=status.HTTP_200_OK,
    summary="Validate and publish a draft WorkflowVersion",
)
async def publish_workflow_version(
    workflow_key: str = Path(min_length=1, max_length=64),  # noqa: ARG001
    version_id: UUID = ...,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminWorkflowsHttpDependencyProvider = Depends(get_admin_workflows_dependencies),
) -> PublishWorkflowResultResponse:
    """Validate + publish a draft ``WorkflowVersion``.

    Re-publishing an already-published version raises
    ``InvariantViolation`` (mapped to ``409 Conflict``) because the
    row's status is no longer ``draft``. The HTTP boundary commits
    once after the use case returns so the status flip, the
    current-version pointer, and the audit row land atomically
    (Constitution IV / plan §4.R12).
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.WORKFLOW_PUBLISH,
        user,
        Resource(tenant_id=user.tenant_id, kind="workflow"),
        role_repository=repositories.role,
    )

    result = await publish_workflow.publish_handle(
        publish_workflow.PublishWorkflowVersionCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            workflow_version_id=version_id,
            correlation_id=correlation.correlation_id,
        ),
        workflow_repo=repositories.workflow,
        workflow_version_repo=repositories.workflow_version,
        queue_repo=repositories.queue,
        audit_store=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return PublishWorkflowResultResponse(
        workflow_definition_id=result.workflow_definition_id,
        workflow_version_id=result.workflow_version_id,
        version_number=result.version_number,
        status=result.status.value,
    )
