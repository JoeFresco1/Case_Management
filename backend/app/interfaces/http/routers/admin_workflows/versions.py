"""Workflow version inspection routes: view, validate, diff.

# Why this module (R2 structural split)

Route group extracted from the former single-file
``admin_workflows.py`` (722 lines). This module owns the read-only /
validator surface over *persisted* versions addressed by stable
workflow id + version number:

- ``GET /api/v1/admin/workflows/{id}/versions/{n}`` — view an
  immutable ``WorkflowVersion``.
- ``POST /api/v1/admin/workflows/{id}/versions/{n}/validate`` — run
  the pure validator against a persisted version without changing its
  lifecycle state.
- ``GET /api/v1/admin/workflows/{id}/versions/{n}/diff`` — compare a
  version with its predecessor by stable state/transition key sets
  (no mutation).

# Authorization

All endpoints require ``Action.WORKFLOW_PUBLISH`` (granted to the
``process_admin`` static role); fail-closed → ``403 Forbidden``.
Missing workflow/version rows surface as ``404`` via the domain
``NotFound`` handler so cross-tenant ids cannot be probed
(Constitution V). None of these endpoints commit a transaction —
they are read-only or side-effect-free.

# INTEGRATION_SEAM: HTTP entry

Reads reach the process through the ``AdminWorkflowsHttpDependencyProvider``
session bundle; validation is the pure application function
``app.application.configuration.validate_definition.validate_definition``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.configuration.validate_definition import (
    validate_definition as validate_workflow_definition,
)
from app.application.workflow.http_dependencies import AdminWorkflowsHttpDependencyProvider
from app.deps import UserContext, get_current_user, get_db
from app.domain.errors import NotFound
from app.interfaces.http.dependencies import get_admin_workflows_dependencies
from app.interfaces.http.schemas.admin_workflows import (
    WorkflowDiffResponse,
    WorkflowValidationResponse,
    WorkflowVersionResponse,
)

from .common import _read_repository, _version_response, router


@router.get(
    "/{workflow_id}/versions/{version_number}",
    response_model=WorkflowVersionResponse,
    summary="View an immutable workflow version",
)
async def get_workflow_version(
    workflow_id: UUID,
    version_number: int = Path(ge=1),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AdminWorkflowsHttpDependencyProvider = Depends(get_admin_workflows_dependencies),
) -> WorkflowVersionResponse:
    """Return a tenant-scoped workflow version for read-only inspection."""
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
    return _version_response(version)


@router.post(
    "/{workflow_id}/versions/{version_number}/validate",
    response_model=WorkflowValidationResponse,
    summary="Validate a persisted workflow version",
)
async def validate_persisted_workflow_version(
    workflow_id: UUID,
    version_number: int = Path(ge=1),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AdminWorkflowsHttpDependencyProvider = Depends(get_admin_workflows_dependencies),
) -> WorkflowValidationResponse:
    """Validate an existing version without changing its lifecycle state."""
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
    queue_keys = await repositories.queue.list_keys(user.tenant_id)
    validate_workflow_definition(version, known_queues=queue_keys)
    return WorkflowValidationResponse(
        workflow_definition_id=workflow.id,
        states=list(version.state_keys()),
        transitions=list(version.transition_keys()),
    )


@router.get(
    "/{workflow_id}/versions/{version_number}/diff",
    response_model=WorkflowDiffResponse,
    summary="Compare a workflow version with its predecessor",
)
async def diff_workflow_version(
    workflow_id: UUID,
    version_number: int = Path(ge=1),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AdminWorkflowsHttpDependencyProvider = Depends(get_admin_workflows_dependencies),
) -> WorkflowDiffResponse:
    """Return stable state/transition key differences without mutation."""
    repositories = dependencies.for_session(db)
    await authorize(
        Action.WORKFLOW_PUBLISH,
        user,
        Resource(tenant_id=user.tenant_id, kind="workflow"),
        role_repository=repositories.role,
    )
    workflow = await repositories.workflow.get(user.tenant_id, workflow_id)
    versions = await _read_repository(repositories).list_versions(user.tenant_id, workflow_id)
    current = next((v for v in versions if v.version_number == version_number), None)
    if workflow is None or current is None:
        raise NotFound("workflow version not found")
    previous = next(
        (v for v in versions if v.version_number == version_number - 1),
        None,
    )
    current_states, current_transitions = set(current.state_keys()), set(current.transition_keys())
    previous_states = set(previous.state_keys()) if previous else set()
    previous_transitions = set(previous.transition_keys()) if previous else set()
    return WorkflowDiffResponse(
        workflow_definition_id=workflow_id,
        version_number=version_number,
        previous_version_number=previous.version_number if previous else None,
        added_states=sorted(current_states - previous_states),
        removed_states=sorted(previous_states - current_states),
        added_transitions=sorted(current_transitions - previous_transitions),
        removed_transitions=sorted(previous_transitions - current_transitions),
    )
