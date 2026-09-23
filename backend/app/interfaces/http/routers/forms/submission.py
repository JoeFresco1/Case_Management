"""Single-step form submission — ``POST /api/v1/forms/cases/{case_id}``.

# Why this module (R2 structural split)

Split out of the former single-file ``forms.py`` (720 lines, past the
R2 600-line soft limit). This module owns the canonical entry point
for P07-US1 / FM-036: it requires the ``Idempotency-Key`` and
``If-Match`` headers at the edge and delegates to
``app.application.forms.submit_form.handle``, which performs
authorization, binding resolution, visibility evaluation,
hidden-field preservation, validation, and the atomic
case+audit+outbox write.

The middleware layer (``IdempotencyMiddleware``) handles replay; the
handler does not need to know about keys.

# INTEGRATION_SEAM: HTTP entry

Authorization, tenancy, and persistence live behind the application
ports the use case receives; nothing here talks to a driver directly.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.forms import submit_form as submit_form_uc
from app.application.forms.http_dependencies import FormsHttpDependencyProvider
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.domain.errors import Conflict
from app.interfaces.http.dependencies import get_forms_dependencies
from app.interfaces.http.schemas.forms import SubmitFormRequest, SubmitFormResponse

from .common import router


@router.post(
    "/cases/{case_id}",
    response_model=SubmitFormResponse,
    status_code=status.HTTP_200_OK,
    summary="Submit a published form against a case",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Idempotency-Key header is required"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.update"},
        status.HTTP_404_NOT_FOUND: {"description": "Case, form binding, or form version not found"},
        status.HTTP_409_CONFLICT: {"description": "Stale If-Match token"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Validation failed (draft, missing required fields, rules violated)"
        },
    },
)
async def submit_form(
    case_id: UUID,
    body: SubmitFormRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: FormsHttpDependencyProvider = Depends(get_forms_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    correlation_id: str = Header(default="", alias="X-Correlation-Id"),
) -> SubmitFormResponse:
    """Submit a published form for a case (P07-US1 / FM-036).

    The form key resolves to the latest published binding for the case's
    case-type version. The form version must be published. Section visibility
    is evaluated; hidden fields are preserved (P07-US3). Visible required
    fields and form-level validation rules are enforced.

    Contracts: ``Idempotency-Key`` is required (transport retries replay
    via ``IdempotencyMiddleware``); ``If-Match`` carries the case
    ``version_token`` (stale → 409 with the current token in
    ``ETag``/``X-Current-Version-Token``). Authorization (``case.update``)
    and tenant scoping live in the use case; denial surfaces as 403
    ``ErrorResponse{forbidden}`` and validation as 422
    ``ErrorResponse{validation_failed}`` via the global domain handler.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    if not if_match or not if_match.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="If-Match header is required (use the case's version_token)",
        )
    try:
        expected_version_token = int(if_match.strip().strip('"'))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="If-Match header must be an integer version_token",
        ) from exc

    repositories = dependencies.for_session(db)
    case_repo = repositories.case
    case_type_version_repo = repositories.case_type_version
    form_binding_repo = repositories.form_binding
    form_version_loader = repositories.form_version
    audit = repositories.audit
    outbox = repositories.outbox

    try:
        result = await submit_form_uc.handle(
            submit_form_uc.SubmitFormCommand(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                case_id=case_id,
                form_key=body.form_key,
                submitted_data=body.data,
                expected_version_token=expected_version_token,
                correlation_id=correlation_id,
            ),
            case_repo=case_repo,
            case_type_version_repo=case_type_version_repo,
            form_binding_repo=form_binding_repo,
            form_version_loader=form_version_loader,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
    except Conflict as exc:
        current = await case_repo.get(user.tenant_id, case_id)
        current_token = current.version_token if current is not None else None
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
            headers={
                "ETag": f'"{current_token}"' if current_token is not None else "",
                "X-Current-Version-Token": str(current_token) if current_token is not None else "",
            },
        ) from exc
    # why: ValidationFailed/NotFound/Forbidden bubble to the global
    # ``domain_error_handler`` so the client always gets the
    # ``ErrorResponse{code,message,details,correlation_id}`` envelope
    # (AGENTS.md §17.1) instead of a bare ``detail`` string.

    await db.commit()
    return SubmitFormResponse(
        case_id=result.case.id,
        reference=result.case.reference.value,
        version_token=result.case.version_token,
        hidden_keys=sorted(result.hidden_keys),
    )
