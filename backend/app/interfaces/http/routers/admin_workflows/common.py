"""Shared admin-workflow ``router`` and read-projection helpers.

# Why this module (R2 structural split)

The former single-file ``admin_workflows.py`` module (722 lines) passed
the R2 600-line soft limit. This module holds the pieces every route
group in the package shares:

- :data:`router` — the one
  ``APIRouter(prefix="/admin/workflows", tags=["admin", "configuration"])``
  that the route modules (:mod:`definitions`, :mod:`versions`,
  :mod:`draft`, :mod:`publish`, :mod:`simulate`) register onto.
  Registration order is irrelevant for correctness: the only two paths
  that share an identical Starlette pattern (``POST /{a}/versions/{b}/publish``)
  both live in :mod:`publish`, with the canonical handler defined first,
  exactly as in the original file — so first-match resolution is
  unchanged.
- :func:`_read_repository` — resolves the optional read projection from
  the session repository bundle, failing closed with ``503`` when the
  projection is unavailable.
- :func:`_version_response` — domain ``WorkflowVersion`` → wire schema
  projection (the Pydantic schema is the OpenAPI contract; hand-editing
  fields at a route is forbidden, AGENTS.md §4.R7).

# INTEGRATION_SEAM: HTTP entry

This package is the only HTTP entry to the admin workflow
configuration lifecycle surface (see the package docstring in
:mod:`app.interfaces.http.routers.admin_workflows` for the endpoint
inventory). Handlers delegate every business decision to the
application layer under ``app.application.workflow`` and
``app.application.configuration``; the router only parses the wire
input, authorizes, maps results/errors. All persistence reaches the
process through the ``AdminWorkflowsHttpDependencyProvider`` session
bundle, so SQL and Azure adapters swap in at composition time without
touching this package.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.application.workflow.http_dependencies import WorkflowReadRepository
from app.domain.workflow import WorkflowVersion
from app.interfaces.http.schemas.admin_workflows import (
    WorkflowStateResponse,
    WorkflowTransitionResponse,
    WorkflowVersionResponse,
)

router = APIRouter(prefix="/admin/workflows", tags=["admin", "configuration"])


def _read_repository(repositories: object) -> WorkflowReadRepository:
    """Resolve the optional read projection, failing closed if unavailable.

    Why fail-closed: the read projection may be wired behind a separate
    adapter than the write repositories; an un-wired projection must
    surface as ``503 Service Unavailable`` rather than silently serving
    an empty list (R12 deny-by-default).

    Args:
        repositories: Session repository bundle from the HTTP
            dependency provider.

    Returns:
        The ``WorkflowReadRepository`` projection.

    Raises:
        HTTPException: ``503`` when the bundle carries no
            ``workflow_read`` projection.
    """
    reader = getattr(repositories, "workflow_read", None)
    if reader is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="workflow read projection is unavailable",
        )
    return reader


def _version_response(version: WorkflowVersion) -> WorkflowVersionResponse:
    """Project a domain version without exposing mutable persistence details.

    Why a projection: the wire schema is the OpenAPI contract — it
    exposes stable identity/state fields only, never the domain
    entity's mutability or persistence internals (AGENTS.md §4.R7).

    Args:
        version: Immutable domain workflow version.

    Returns:
        The wire ``WorkflowVersionResponse``.
    """
    return WorkflowVersionResponse(
        workflow_definition_id=version.workflow_definition_id,
        workflow_version_id=version.id,
        version_number=version.version_number,
        status=version.status.value,
        states=[
            WorkflowStateResponse(
                key=state.key,
                label=state.label,
                is_initial=state.is_initial,
                is_terminal=state.is_terminal,
            )
            for state in version.states
        ],
        transitions=[
            WorkflowTransitionResponse(
                key=transition.key,
                from_state=transition.from_state,
                to_state=transition.to_state,
                human_driven=transition.human_driven,
                guard=transition.guard,
                actions=[dict(action) for action in transition.actions],
            )
            for transition in version.transitions
        ],
    )
