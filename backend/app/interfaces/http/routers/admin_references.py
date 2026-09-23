"""Admin API for per-tenant reference format configuration (FM-009 / P01-US9).

Endpoints:

- ``GET    /api/v1/admin/reference-format`` — return the actor tenant's
  effective reference template (override if set, otherwise the platform
  default ``C-{TENANT}-{YYYY}-{SEQ}``).
- ``POST   /api/v1/admin/reference-format`` — set a new override
  template. The use case validates the template against the closed
  token vocabulary; a malformed template returns ``422``.
- ``DELETE /api/v1/admin/reference-format`` — clear the override and
  fall back to the platform default. Existing cases are never
  retroactively re-numbered (Pillar P01 §6 Q1) — only future
  ``next_reference`` calls pick up the new template.

# Authorization

Every endpoint requires ``ADMIN_TENANT_CONFIG``; only the
``process_admin`` role grants it (``app/application/authorization.py``).
Fail-closed: missing role -> ``403 Forbidden``.

# Transaction shape

Configuration writes run inside the same DB transaction as the
``ReferenceFormatConfigured`` / ``ReferenceFormatReset`` audit event so
the configuration row and the audit row commit atomically
(Constitution IV / plan §4.R12). The HTTP boundary commits once after
the use case returns.

# Idempotency

The ``POST`` endpoint accepts the standard ``Idempotency-Key`` header
via the global ``IdempotencyMiddleware`` (``interfaces/http/idempotency.py``).
Replays of a successful configure return the original result.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.references import configure_references
from app.application.references.http_dependencies import (
    AdminReferencesHttpDependencyProvider,
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
from app.interfaces.http.dependencies import get_admin_references_dependencies
from app.interfaces.http.schemas.admin_references import (
    ConfigureReferenceFormatRequest,
    ReferenceFormatResponse,
)

router = APIRouter(prefix="/admin/reference-format", tags=["admin", "configuration"])


def _to_response(
    result: configure_references.ConfigureReferenceFormatResult
    | configure_references.GetReferenceFormatResult,
) -> ReferenceFormatResponse:
    return ReferenceFormatResponse(
        tenant_id=result.tenant_id,
        template=result.template,
        is_default=result.is_default,
    )


@router.get(
    "",
    response_model=ReferenceFormatResponse,
    summary="Get the actor tenant's effective reference format",
)
async def get_reference_format(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AdminReferencesHttpDependencyProvider = Depends(
        get_admin_references_dependencies
    ),
) -> ReferenceFormatResponse:
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="reference_format"),
    )
    repositories = dependencies.for_session(db)
    result = await configure_references.get_reference_format(
        user.tenant_id,
        repo=repositories.reference_format,
    )
    return _to_response(result)


@router.post(
    "",
    response_model=ReferenceFormatResponse,
    status_code=status.HTTP_200_OK,
    summary="Set the actor tenant's reference format template",
)
async def configure_reference_format(
    body: ConfigureReferenceFormatRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminReferencesHttpDependencyProvider = Depends(
        get_admin_references_dependencies
    ),
) -> ReferenceFormatResponse:
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="reference_format"),
    )

    repositories = dependencies.for_session(db)

    result = await configure_references.configure_reference_format(
        configure_references.ConfigureReferenceFormatCommand(
            tenant_id=user.tenant_id,
            actor=user,
            correlation_id=correlation.correlation_id,
            template=body.template,
        ),
        repo=repositories.reference_format,
        audit=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return _to_response(result)


@router.delete(
    "",
    response_model=ReferenceFormatResponse,
    summary="Reset the actor tenant's reference format to the platform default",
)
async def reset_reference_format(
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminReferencesHttpDependencyProvider = Depends(
        get_admin_references_dependencies
    ),
) -> ReferenceFormatResponse:
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="reference_format"),
    )

    repositories = dependencies.for_session(db)

    result = await configure_references.reset_reference_format(
        configure_references.ResetReferenceFormatCommand(
            tenant_id=user.tenant_id,
            actor=user,
            correlation_id=correlation.correlation_id,
        ),
        repo=repositories.reference_format,
        audit=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return _to_response(result)


__all__ = ["router"]
