"""Multi-step / resumable form sessions — P07-US6 / FM-038 routes.

# Why this module (R2 structural split)

Split out of the former single-file ``forms.py`` (720 lines, past the
R2 600-line soft limit). This module owns the three session endpoints:

- ``POST /api/v1/forms/cases/{case_id}/sessions`` — start or resume a
  multi-step session (idempotent per (case, form, actor) triple);
- ``PATCH /api/v1/forms/cases/{case_id}/sessions/{session_id}/steps/{step_key}``
  — save one step's progress (``If-Match`` optimistic concurrency);
- ``GET /api/v1/forms/cases/{case_id}/sessions`` — read-only resume of
  the open session for a form key.

# INTEGRATION_SEAM: the FormSessionRepository port has no SQL adapter yet
(FM-038 is deferred). The endpoint returns 501 Not Implemented when
the persistence seam is unwired so the SPA can ship a UI against a
known shape without lying about being implemented.

The seam guard is :func:`_form_session_repo_or_501`; a ``None``
provider (test doubles, future Azure seam work) keeps the 501
fail-closed shape. Authorization, tenancy, and CAS concurrency live in
the ``app.application.forms.advanced_execution`` use cases — this
module only translates between HTTP and the application shape.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.forms import advanced_execution as adv
from app.application.forms.advanced_execution import FormSessionRepository
from app.application.forms.http_dependencies import FormsHttpDependencyProvider
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.domain.errors import Conflict, NotFound
from app.interfaces.http.dependencies import get_forms_dependencies
from app.interfaces.http.schemas.forms import (
    FormSessionResponse,
    FormStepSpec,
    SaveFormStepRequest,
    StartFormSessionRequest,
)

from .common import router


def _session_view_to_response(view: adv.FormSessionView) -> FormSessionResponse:
    current = view.current_step
    return FormSessionResponse(
        id=view.session.id,
        tenant_id=view.session.tenant_id,
        case_id=view.session.case_id,
        form_version_id=view.session.form_version_id,
        actor_id=view.session.actor_id,
        status=view.session.status,
        current_step_index=view.session.current_step_index,
        version_token=view.session.version_token,
        completed_steps=sorted(view.session.completed_steps),
        steps=[
            FormStepSpec(
                step_key=step.step_key,
                label=step.label,
                section_keys=list(step.section_keys),
                required=step.required,
            )
            for step in view.steps
        ],
        progress=[list(p) for p in view.progress],
        current_step=FormStepSpec(
            step_key=current.step_key,
            label=current.label,
            section_keys=list(current.section_keys),
            required=current.required,
        )
        if current is not None
        else None,
        # why: the view strips conditionally-hidden answers
        # server-side (P07-US3 / Constitution V). The wire never
        # carries fields the worker's current answers hide; the full
        # accumulation stays in the session row for preservation.
        step_data=dict(view.visible_step_data),
        updated_at=view.session.updated_at.isoformat() if view.session.updated_at else None,
    )


@router.post(
    "/cases/{case_id}/sessions",
    response_model=FormSessionResponse,
    status_code=status.HTTP_200_OK,
    summary="Start or resume a multi-step form session (P07-US6)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Idempotency-Key header is required"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.update"},
        status.HTTP_404_NOT_FOUND: {"description": "Case, form binding, or form version not found"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Validation failed (draft form, binding mismatch, no steps)"
        },
    },
)
async def start_form_session(
    case_id: UUID,
    body: StartFormSessionRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: FormsHttpDependencyProvider = Depends(get_forms_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    correlation_id: str = Header(default="", alias="X-Correlation-Id"),
) -> FormSessionResponse:
    """Begin a multi-step form session for a case (P07-US6).

    Idempotent: when the actor already has an open session for the
    (case, form, actor) triple, that session is returned so a worker
    who closed the browser mid-form resumes exactly where they left
    off (acceptance: ``resume after partial``). Transport retries
    replay via ``IdempotencyMiddleware``; the header is required here
    (like the single-step submit) so a missing key fails fast with
    400 before any DB touch.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )

    repositories = dependencies.for_session(db)
    case_repo = repositories.case
    form_binding_repo = repositories.form_binding
    form_version_loader = repositories.form_version
    session_repo = _form_session_repo_or_501(repositories.session)

    # why: Forbidden/ValidationFailed/NotFound bubble to the global
    # domain handler (ErrorResponse envelope); only the 501 seam needs
    # local mapping.
    view = await adv.start_session(
        adv.StartFormSessionCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            case_id=case_id,
            form_key=body.form_key,
            correlation_id=correlation_id,
        ),
        case_repo=case_repo,
        form_binding_repo=form_binding_repo,
        form_version_loader=form_version_loader,
        session_repo=session_repo,
        clock=clock,
    )
    await db.commit()
    return _session_view_to_response(view)


@router.patch(
    "/cases/{case_id}/sessions/{session_id}/steps/{step_key}",
    response_model=FormSessionResponse,
    status_code=status.HTTP_200_OK,
    summary="Save progress on a multi-step form (P07-US6)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Session belongs to another actor"},
        status.HTTP_404_NOT_FOUND: {"description": "Session or case not found"},
        status.HTTP_409_CONFLICT: {"description": "Stale version_token"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Unknown step or step ahead of current"
        },
    },
)
async def save_form_step(
    case_id: UUID,
    session_id: UUID,
    step_key: str,
    body: SaveFormStepRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: FormsHttpDependencyProvider = Depends(get_forms_dependencies),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> FormSessionResponse:
    """Save one step's partial data on a multi-step form session.

    The SPA sends ``If-Match: <version_token>`` for optimistic
    concurrency: two concurrent saves against the same session
    surface as ``409 Conflict`` rather than silently losing updates.
    """

    expected_version_token = body.expected_version_token
    if if_match and if_match.strip():
        try:
            expected_version_token = int(if_match.strip().strip('"'))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="If-Match header must be an integer version_token",
            ) from exc

    repositories = dependencies.for_session(db)
    case_repo = repositories.case
    form_version_loader = repositories.form_version
    session_repo = _form_session_repo_or_501(repositories.session)

    try:
        view = await adv.save_step(
            adv.SaveFormStepCommand(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                case_id=case_id,
                session_id=session_id,
                step_key=step_key,
                data=body.data,
                expected_version_token=expected_version_token,
            ),
            case_repo=case_repo,
            form_version_loader=form_version_loader,
            session_repo=session_repo,
            clock=clock,
        )
    except Conflict as exc:
        # why: the client needs the server's current token to
        # recover, not an echo of the stale token it sent. Re-read
        # tenant-scoped so a cross-tenant id never leaks existence.
        current = await session_repo.get(user.tenant_id, session_id)
        current_token = current.version_token if current is not None else expected_version_token
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
            headers={
                "ETag": f'"{current_token}"',
                "X-Current-Version-Token": str(current_token),
            },
        ) from exc
    # why: Forbidden/ValidationFailed/NotFound bubble to the global
    # domain handler (ErrorResponse envelope).
    await db.commit()
    return _session_view_to_response(view)


@router.get(
    "/cases/{case_id}/sessions",
    response_model=FormSessionResponse | None,
    summary="Resume an in-progress multi-step form session (P07-US6)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.update"},
        status.HTTP_404_NOT_FOUND: {"description": "No open session for this form key"},
    },
)
async def resume_form_session(
    case_id: UUID,
    form_key: str = Query(..., min_length=1, max_length=64),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: FormsHttpDependencyProvider = Depends(get_forms_dependencies),
) -> FormSessionResponse | None:
    """Return the open session for the (case, form, actor) triple or 404.

    Idempotent: repeated calls return the same session with the same
    progress and never create or advance work. The response carries
    only the worker-visible payload (hidden-section answers stay
    server-side). The SPA mounts the multi-step form by calling this
    on load; if it returns a session, the UI renders the current
    step; if it returns 404, the UI starts a new session with
    ``POST /forms/cases/{id}/sessions``.
    """

    repositories = dependencies.for_session(db)
    form_binding_repo = repositories.form_binding
    form_version_loader = repositories.form_version
    session_repo = _form_session_repo_or_501(repositories.session)

    # why: resume is read-only and idempotent — repeated calls return
    # the same session and never create work. Auth/tenant failures
    # bubble as ErrorResponse (never an existence oracle).
    view = await adv.resume_session(
        adv.ResumeFormSessionQuery(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            case_id=case_id,
            form_key=form_key,
        ),
        case_repo=repositories.case,
        form_binding_repo=form_binding_repo,
        form_version_loader=form_version_loader,
        session_repo=session_repo,
    )
    if view is None:
        raise NotFound(f"no open form session for form {form_key!r}")
    return _session_view_to_response(view)


def _form_session_repo_or_501(session_repo: FormSessionRepository | None) -> FormSessionRepository:
    """Return the session repository or raise 501 when the seam is unwired.

    The SQL adapter (``SqlFormSessionRepository``, FM-038) is the
    production wiring; a ``None`` provider (test doubles, future Azure
    seam work) keeps the 501 fail-closed shape so the SPA never gets a
    success response the platform cannot honor.
    """
    if session_repo is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "FormSessionRepository SQL adapter is reserved for the "
                "FM-038 follow-up (see advanced_execution)."
            ),
        )
    return session_repo
