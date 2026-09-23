"""Published form reads — ``GET /api/v1/forms`` surface.

# Why this module (R2 structural split)

Split out of the former single-file ``forms.py`` (720 lines, past the
R2 600-line soft limit). This module owns the three published-read
endpoints:

- ``GET /api/v1/forms`` — list published form versions for the tenant;
- ``GET /api/v1/forms/{form_key}`` — the latest published version of
  one form (drafts never resolve, so a draft key is a 404);
- ``GET /api/v1/forms/{form_key}/bindings`` — published case-type
  bindings for a form key.

All three authorize ``case.read`` on the tenant before any DB touch
(fail-closed, no use case needed for these pure configuration reads)
and return the canonical ``Page[T]`` envelope where list-shaped.

# INTEGRATION_SEAM: HTTP entry

Repository access arrives through the session dependency bundle
(``FormsHttpDependencyProvider``); SQL vs. future adapters swap behind
that port, never here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.forms.http_dependencies import FormsHttpDependencyProvider
from app.deps import UserContext, get_current_user, get_db
from app.domain.errors import NotFound
from app.interfaces.http.dependencies import get_forms_dependencies
from app.interfaces.http.schemas.forms import (
    FormBindingListItem,
    FormListItem,
    FormVersionResponse,
    Page,
)

from .common import _form_version_to_response, router


@router.get(
    "",
    response_model=Page[FormListItem],
    summary="List published form versions for the current tenant",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.read"},
    },
)
async def list_form_versions(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: FormsHttpDependencyProvider = Depends(get_forms_dependencies),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[FormListItem]:
    """List all published form versions for the tenant.

    Policy: ``case.read`` on the tenant (published forms are readable
    by anyone who can read cases; the use-case-free read still fails
    closed via ``authorize`` before any DB touch). Tenant scope is the
    caller's ``tenant_id``; cross-tenant rows are never returned.
    """
    await authorize(
        Action.CASE_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="form"),
    )
    form_version_repo = dependencies.for_session(db).form_version
    forms = await form_version_repo.list_published(user.tenant_id, limit=limit, offset=offset)

    items = [
        FormListItem(
            id=f.id,
            key=f.key,
            version_number=f.version_number,
            status=f.status.value,
            published_at=f.published_at.isoformat() if f.published_at else None,
        )
        for f in forms
    ]

    next_cursor: str | None = None
    if len(forms) == limit:
        next_cursor = f"{offset + limit}:{limit}"

    return Page[FormListItem](items=items, next_cursor=next_cursor, limit=limit)


@router.get(
    "/{form_key}",
    response_model=FormVersionResponse,
    summary="Get the latest published version of a form by key",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.read"},
        status.HTTP_404_NOT_FOUND: {"description": "No published form version for this key"},
    },
)
async def get_form_version(
    form_key: str,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: FormsHttpDependencyProvider = Depends(get_forms_dependencies),
) -> FormVersionResponse:
    """Return the latest published form version for a key.

    Draft versions are never returned: only ``published`` rows resolve
    (a draft key surfaces as 404 ``ErrorResponse{not_found}`` so the
    caller cannot distinguish "no such form" from "not yet
    published").
    """
    await authorize(
        Action.CASE_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="form"),
    )
    form_version_repo = dependencies.for_session(db).form_version
    fv = await form_version_repo.get_latest_published_for_key(user.tenant_id, form_key)
    if fv is None:
        raise NotFound(f"no published form version for {form_key!r}")
    return _form_version_to_response(fv)


@router.get(
    "/{form_key}/bindings",
    response_model=Page[FormBindingListItem],
    summary="List published bindings for a form key",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.read"},
        status.HTTP_404_NOT_FOUND: {"description": "No published form version for this key"},
    },
)
async def list_form_bindings(
    form_key: str,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: FormsHttpDependencyProvider = Depends(get_forms_dependencies),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[FormBindingListItem]:
    """List all published bindings for a form key.

    Only bindings whose form version is published resolve; retired
    bindings are excluded by the repository (no longer eligible for
    new submissions). Paginated with the canonical ``Page[T]``
    envelope.
    """
    await authorize(
        Action.CASE_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="form"),
    )
    repositories = dependencies.for_session(db)
    form_binding_repo = repositories.form_binding
    form_version_repo = repositories.form_version

    fv = await form_version_repo.get_latest_published_for_key(user.tenant_id, form_key)
    if fv is None:
        raise NotFound(f"no published form version for {form_key!r}")

    bindings = await form_binding_repo.list_published_for_form_version(
        user.tenant_id, fv.id, limit=limit, offset=offset
    )

    items = [
        FormBindingListItem(
            id=b.id,
            case_type_version_id=b.case_type_version_id,
            form_version_id=b.form_version_id,
            published_at=b.published_at.isoformat() if b.published_at else None,
        )
        for b in bindings
    ]

    next_cursor: str | None = None
    if len(bindings) == limit:
        next_cursor = f"{offset + limit}:{limit}"

    return Page[FormBindingListItem](items=items, next_cursor=next_cursor, limit=limit)
