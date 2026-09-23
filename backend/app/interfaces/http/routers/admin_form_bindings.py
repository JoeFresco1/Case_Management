"""Admin API for form-binding lifecycle (P07 / FM-036).

Endpoints:

- ``POST   /admin/form-bindings``                  — draft a new binding.
- ``POST   /admin/form-bindings/{id}/publish``    — publish a draft binding.
- ``POST   /admin/form-bindings/{id}/retire``    — retire a published binding.
- ``GET    /admin/form-bindings/{id}``           — get one binding by id.
- ``GET    /admin/form-bindings?form_version_id=`` — list bindings for a form version.

# Authorization

All endpoints require ``Action.ADMIN_TENANT_CONFIG``.

# Transaction shape

Each mutation runs inside the same DB transaction as its audit event
so the status flip and the audit row commit atomically.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.forms.admin.http_dependencies import (
    AdminFormBindingsHttpDependencyProvider,
    AdminFormBindingsHttpRepositories,
)
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.domain.errors import NotFound, ValidationFailed
from app.domain.form import FormBinding
from app.interfaces.http.dependencies import get_admin_form_bindings_dependencies
from app.interfaces.http.schemas.admin_form_bindings import (
    CreateFormBindingRequest,
    FormBindingDetailResponse,
    Page,
)

router = APIRouter(prefix="/admin/form-bindings", tags=["admin", "configuration"])


def _to_detail(b: FormBinding) -> FormBindingDetailResponse:
    return FormBindingDetailResponse(
        id=b.id,
        tenant_id=b.tenant_id,
        case_type_version_id=b.case_type_version_id,
        form_version_id=b.form_version_id,
        published_at=b.published_at.isoformat() if b.published_at else None,
        published_by=b.published_by,
        created_at=b.created_at.isoformat() if b.created_at else "",
        state_forms=[dict(s) for s in b.state_forms],
        retired_at=b.retired_at.isoformat() if getattr(b, "retired_at", None) else None,
        retired_by=getattr(b, "retired_by", None),
    )


@router.post(
    "",
    response_model=FormBindingDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Draft a new form binding (FM-036)",
)
async def create_form_binding(
    body: CreateFormBindingRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminFormBindingsHttpDependencyProvider = Depends(
        get_admin_form_bindings_dependencies
    ),
) -> FormBindingDetailResponse:
    """Draft a new form binding (draft status: published_at is None).

    ``state_forms`` entries are validated semantically here (in
    addition to the Pydantic shape in ``CreateFormBindingRequest``):
    blank-after-strip keys and duplicate ``(form_key, state_key)``
    pairs are rejected with 422 ``validation_failed`` before any DB
    write so a malformed transition binding can never drift into the
    workflow engine (P07-US7).
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="form_binding"),
        role_repository=repositories.role,
    )

    state_forms = _validated_state_forms([e.model_dump() for e in body.state_forms])
    now = clock.now()
    draft = FormBinding.draft(
        tenant_id=user.tenant_id,
        case_type_version_id=body.case_type_version_id,
        form_version_id=body.form_version_id,
        now=now,
        state_forms=state_forms,
    )
    await repositories.form_binding.add(draft)
    await _audit(
        repositories,
        user,
        correlation.correlation_id,
        "ConfigurationDrafted",
        draft.id,
        {
            "case_type_version_id": str(draft.case_type_version_id),
            "form_version_id": str(draft.form_version_id),
        },
        now,
    )
    await db.commit()
    return _to_detail(draft)


@router.post(
    "/{binding_id}/publish",
    response_model=FormBindingDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Publish a draft form binding (FM-036)",
)
async def publish_form_binding(
    binding_id: UUID = Path(...),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminFormBindingsHttpDependencyProvider = Depends(
        get_admin_form_bindings_dependencies
    ),
) -> FormBindingDetailResponse:
    """Publish a draft form binding. Idempotent on re-publish.

    Re-publishing an already-published binding returns the stored
    published view without a second audit row so transport retries
    with the same ``Idempotency-Key`` (enforced by
    ``IdempotencyMiddleware`` for all POSTs) never duplicate the
    lifecycle event. A malformed ``state_forms`` payload stored
    before validation existed is rejected with 422 instead of being
    published.
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="form_binding"),
        role_repository=repositories.role,
    )

    binding = await repositories.form_binding.get_by_id(user.tenant_id, binding_id)
    if binding is None:
        raise NotFound(f"form binding {binding_id} not found")

    # why: bindings drafted before state_forms validation existed may
    # carry blank/duplicate entries; publishing them would plant a
    # transition guard the engine cannot evaluate. Fail closed here.
    _validated_state_forms([dict(s) for s in binding.state_forms])
    if binding.published_at is not None:
        return _to_detail(binding)

    now = clock.now()
    published = binding.publish(actor_id=user.user_id, at=now)
    await repositories.form_binding.mark_published(
        user.tenant_id,
        binding_id,
        published_at=now,
        published_by=user.user_id,
    )
    await _audit(
        repositories,
        user,
        correlation.correlation_id,
        "ConfigurationPublished",
        binding_id,
        {
            "case_type_version_id": str(published.case_type_version_id),
            "form_version_id": str(published.form_version_id),
        },
        now,
    )
    await db.commit()
    return _to_detail(published)


@router.post(
    "/{binding_id}/retire",
    response_model=FormBindingDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Retire a published form binding (FM-036)",
)
async def retire_form_binding(
    binding_id: UUID = Path(...),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminFormBindingsHttpDependencyProvider = Depends(
        get_admin_form_bindings_dependencies
    ),
) -> FormBindingDetailResponse:
    """Retire a published form binding.

    Retired bindings are no longer eligible for new submissions.
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="form_binding"),
        role_repository=repositories.role,
    )

    binding = await repositories.form_binding.get_by_id(user.tenant_id, binding_id)
    if binding is None:
        raise NotFound(f"form binding {binding_id} not found")

    now = clock.now()
    retired = binding.retire(actor_id=user.user_id, at=now)
    await repositories.form_binding.mark_retired(
        user.tenant_id,
        binding_id,
        retired_at=now,
        retired_by=user.user_id,
    )
    await _audit(
        repositories,
        user,
        correlation.correlation_id,
        "ConfigurationRetired",
        binding_id,
        {
            "case_type_version_id": str(retired.case_type_version_id),
            "form_version_id": str(retired.form_version_id),
        },
        now,
    )
    await db.commit()
    return _to_detail(retired)


@router.get(
    "/{binding_id}",
    response_model=FormBindingDetailResponse,
    summary="Get a form binding by id (FM-036)",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Form binding not found"},
    },
)
async def get_form_binding(
    binding_id: UUID = Path(...),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AdminFormBindingsHttpDependencyProvider = Depends(
        get_admin_form_bindings_dependencies
    ),
) -> FormBindingDetailResponse:
    """Return the full detail of one form binding."""
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="form_binding"),
        role_repository=repositories.role,
    )

    binding = await repositories.form_binding.get_by_id(user.tenant_id, binding_id)
    if binding is None:
        raise NotFound(f"form binding {binding_id} not found")
    return _to_detail(binding)


@router.get(
    "",
    response_model=Page[FormBindingDetailResponse],
    summary="List form bindings for a form version (FM-036)",
)
async def list_form_bindings(
    form_version_id: UUID = Query(..., description="Filter by form version id"),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AdminFormBindingsHttpDependencyProvider = Depends(
        get_admin_form_bindings_dependencies
    ),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[FormBindingDetailResponse]:
    """List all bindings (any status) for one form version.

    Paginated with the canonical ``Page[T]`` envelope (AGENTS.md
    §17.1): ``limit`` defaults to 50 and caps at 200; ``next_cursor``
    is an opaque ``<offset>:<limit>`` string, absent on the last page.
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="form_binding"),
        role_repository=repositories.role,
    )

    bindings = await repositories.form_binding.list_for_form_version(
        user.tenant_id, form_version_id, limit=limit, offset=offset
    )
    items = [_to_detail(b) for b in bindings]
    next_cursor: str | None = None
    if len(bindings) == limit:
        next_cursor = f"{offset + limit}:{limit}"
    return Page[FormBindingDetailResponse](items=items, next_cursor=next_cursor, limit=limit)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validated_state_forms(
    entries: list[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Validate ``state_forms`` entries semantically.

    Pydantic already enforces presence, length (1-64), and the
    ``required`` bool. This checks what the schema cannot: keys that
    are blank after stripping whitespace, and duplicate
    ``(form_key, state_key)`` pairs (which would make the transition
    guard ambiguous). Raises ``ValidationFailed`` so the global
    handler renders 422 ``ErrorResponse{validation_failed}``.
    """
    seen: set[tuple[str, str]] = set()
    cleaned: list[Mapping[str, Any]] = []
    for entry in entries:
        raw_form = entry.get("form_key") if isinstance(entry, Mapping) else None
        raw_state = entry.get("state_key") if isinstance(entry, Mapping) else None
        form_key = str(raw_form or "").strip()
        state_key = str(raw_state or "").strip()
        if not form_key or not state_key:
            raise ValidationFailed("state_forms entries need non-blank form_key/state_key")
        if len(form_key) > 64 or len(state_key) > 64:
            raise ValidationFailed("state_forms keys must be 1-64 characters")
        pair = (form_key, state_key)
        if pair in seen:
            raise ValidationFailed(
                f"duplicate state_forms entry for form {form_key!r} state {state_key!r}"
            )
        seen.add(pair)
        required = entry.get("required", True) if isinstance(entry, Mapping) else True
        cleaned.append({"form_key": form_key, "state_key": state_key, "required": bool(required)})
    return tuple(cleaned)


async def _audit(
    repositories: AdminFormBindingsHttpRepositories,
    user: UserContext,
    correlation_id: str,
    event_type: str,
    subject_id: UUID,
    payload: dict[str, str],
    now: datetime,
) -> None:
    await repositories.audit.append(
        tenant_id=user.tenant_id,
        event_type=event_type,
        actor_id=user.user_id,
        actor_type="user",
        subject_type="FormBinding",
        subject_id=subject_id,
        case_id=None,
        correlation_id=correlation_id,
        causation_id=None,
        payload=payload,
        occurred_at=now,
    )


__all__ = ["router"]
