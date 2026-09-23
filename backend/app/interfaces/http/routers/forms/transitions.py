"""Transition form validation — ``POST /api/v1/forms/bindings/{form_key}/transitions/check``.

# Why this module (R2 structural split)

Split out of the former single-file ``forms.py`` (720 lines, past the
R2 600-line soft limit). This module owns the single P07-US7 / FM-038
guard endpoint: the workflow engine asks whether every state-bound
form required by a transition's target state has been submitted, and
the response is merged with the engine's own guards before the
transition fires.

Policy: ``case.read`` on the tenant (the guard reads published
configuration, never case content). Malformed bindings rejected at
draft time never reach here; a binding whose stored ``state_forms``
fail semantic validation surfaces as 422 rather than silently
allowing the transition.

# INTEGRATION_SEAM: HTTP entry

The verdict comes from the pure
``app.application.forms.advanced_execution`` helpers; the workflow
engine that calls this endpoint is the seam point — no workflow state
is read or written here.
"""

from __future__ import annotations

from fastapi import Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.forms import advanced_execution as adv
from app.application.forms.http_dependencies import FormsHttpDependencyProvider
from app.deps import UserContext, get_current_user, get_db
from app.domain.errors import NotFound
from app.domain.workflow import WorkflowTransition
from app.interfaces.http.dependencies import get_forms_dependencies
from app.interfaces.http.schemas.forms import (
    TransitionFormCheckRequest,
    TransitionFormCheckResponse,
)

from .common import router


@router.post(
    "/bindings/{form_key}/transitions/check",
    response_model=TransitionFormCheckResponse,
    summary="Validate that every state-bound form has been submitted (P07-US7)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.read"},
        status.HTTP_404_NOT_FOUND: {"description": "No published binding for this form key"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Malformed transition binding or target state"
        },
    },
)
async def check_transition_forms(
    form_key: str,
    body: TransitionFormCheckRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: FormsHttpDependencyProvider = Depends(get_forms_dependencies),
) -> TransitionFormCheckResponse:
    """Verify the form-bound state requirements for a workflow transition.

    The workflow engine calls this before firing a transition; the
    response is merged with the engine's own guards. A transition
    that lands in a state with required state-bound forms is
    rejected when any of those forms has not been submitted yet.

    Policy: ``case.read`` on the tenant (the guard reads published
    configuration, never case content). Malformed bindings rejected
    at draft time never reach here; a binding whose stored
    ``state_forms`` fail semantic validation surfaces as 422 rather
    than silently allowing the transition.
    """
    await authorize(
        Action.CASE_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="form"),
    )

    repositories = dependencies.for_session(db)
    form_binding_repo = repositories.form_binding
    form_version_repo = repositories.form_version

    binding = await form_binding_repo.get_published_for_key(user.tenant_id, form_key)
    if binding is None:
        raise NotFound(f"no published binding for form {form_key!r}")
    form_version = await form_version_repo.get_by_id(user.tenant_id, binding.form_version_id)
    if form_version is None:
        raise NotFound(f"form version {binding.form_version_id} not found")
    requirements = adv.derive_state_requirements(binding)
    if not requirements:
        return TransitionFormCheckResponse(allowed=True)

    # Synthesize a transition whose ``to_state`` is the caller's
    # target. The validator only reads ``to_state``; the other
    # fields (``from_state`` etc.) are not consulted here. The key is
    # a constant: it must satisfy the domain's key charset
    # (alnum plus ``_``/``-``), so it cannot embed the target state
    # verbatim (a ``:`` separator would fail validation).
    transition = WorkflowTransition(
        key="transition-form-check",
        from_state="*",
        to_state=body.target_state,
    )
    # why: ValidationFailed bubbles to the global handler as 422
    # ErrorResponse{validation_failed} (malformed target state or a
    # requirement naming an undeclared state).
    verdict = adv.validate_transition_forms(
        transition=transition,
        target_state_requirements=requirements,
        submitted_form_keys=body.submitted_form_keys,
    )
    return TransitionFormCheckResponse(
        allowed=verdict.allowed,
        missing=list(verdict.missing),
        target_state=transition.to_state or None,
        evaluated_requirements=[
            {"form_key": r.form_key, "state_key": r.state_key, "required": r.required}
            for r in verdict.evaluated_requirements
        ],
    )
