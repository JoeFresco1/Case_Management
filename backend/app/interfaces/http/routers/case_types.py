"""Authorized published case-type catalog for schema-driven intake (T051)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cases import list_published_case_types as list_types_uc
from app.application.cases.http_dependencies import CaseHttpDependencyProvider
from app.deps import UserContext, get_current_user, get_db
from app.interfaces.http.dependencies import get_case_dependencies
from app.interfaces.http.schemas.case_types import (
    PublishedCaseTypeField,
    PublishedCaseTypeListResponse,
    PublishedCaseTypeResponse,
    PublishedCaseTypeSchema,
)

router = APIRouter(prefix="/case-types", tags=["case-types"])


def _response(item: list_types_uc.PublishedCaseType) -> PublishedCaseTypeResponse:
    """Project a domain catalog item into its explicit HTTP schema."""
    return PublishedCaseTypeResponse(
        id=item.id,
        key=item.key,
        name=item.name,
        description=item.description,
        sensitivity_default=item.sensitivity_default,
        version_id=item.version_id,
        version_number=item.version_number,
        schema=PublishedCaseTypeSchema(
            fields=[
                PublishedCaseTypeField(
                    key=field.key,
                    label=field.label,
                    type=field.type,
                    required=field.required,
                    enum_values=list(field.enum_values),
                    sensitivity=field.sensitivity,
                    searchable=field.searchable,
                )
                for field in item.schema.fields
            ],
            document_classifications=list(item.schema.document_classifications),
            rules=[dict(rule) for rule in item.schema.rules],
        ),
    )


@router.get(
    "",
    response_model=PublishedCaseTypeListResponse,
    status_code=status.HTTP_200_OK,
    summary="List published case types available for intake",
)
async def list_case_types(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    limit: int = Query(default=50, ge=1, le=200),
) -> PublishedCaseTypeListResponse:
    """Return tenant-scoped schemas authorized for case creation."""
    repositories = dependencies.for_session(db)
    items = await list_types_uc.handle(
        tenant_id=user.tenant_id,
        actor=user,
        case_type_repo=repositories.case_type,
        version_repo=repositories.case_type_version,
    )
    projected = [_response(item) for item in items[:limit]]
    return PublishedCaseTypeListResponse(items=projected, limit=limit)


__all__ = ["router"]
