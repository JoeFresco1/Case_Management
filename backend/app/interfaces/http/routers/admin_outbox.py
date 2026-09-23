"""Admin API for outbox observability and replay (FM-052 / P13-US9 / P12-US4).

Endpoints:

- ``GET  /api/v1/admin/outbox``              — lag summary plus a page of
  pending rows (dead-lettered and retrying).
- ``POST /api/v1/admin/outbox/{id}/replay``  — reset a row's retry state so
  the dispatcher re-attempts it on its next tick.

# Authorization

Both endpoints require ``ADMIN_TENANT_CONFIG`` — outbox operations are
operator-only and must not be exposed to ordinary case workers. The
authorization check uses ``app.application.authorization.authorize``;
non-admin actors receive 403 (the standard ``Forbidden`` mapping in
``interfaces/http/errors.py``).

# Cross-tenant posture

The outbox is global, not tenant-scoped — one ``outbox`` table serves
every tenant. The operator console therefore exposes ALL rows in the
system, not just the actor's tenant, and the response carries each
row's ``tenant_id`` so the UI can label it. Tenants cannot see each
other's row payloads because the projection below only exposes
metadata (``event_type``, ``attempt_count``, ``last_error``, ``payload``
is intentionally omitted — payloads may carry sensitive references).

# Replay semantics

A replay resets ``attempt_count`` to 0 and clears the row for re-dispatch
on the next ``tick``. The previous ``last_error`` is preserved (audit
value) but the row becomes eligible again. A replay of an already-
dispatched row returns ``reset=False`` (no-op) so the operator UI can
distinguish "your action did nothing" from "your action succeeded".
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.outbox.http_dependencies import (
    OUTBOX_MAX_ATTEMPTS,
    AdminOutboxHttpDependencyProvider,
    OutboxAdminService,
    OutboxRowView,
)
from app.deps import UserContext, get_current_user, get_db
from app.interfaces.http.dependencies import get_admin_outbox_dependencies
from app.interfaces.http.schemas.admin_outbox import (
    OutboxLagSummary,
    OutboxListResponse,
    OutboxReplayResponse,
    OutboxRowResponse,
)

router = APIRouter(prefix="/admin/outbox", tags=["admin", "outbox"])


def _outbox_service(request: Request) -> OutboxAdminService:
    """Return the application-scoped outbox admin service or raise 503.

    Why 503 and not 404: if the dispatcher is missing the platform is
    not operating correctly (the lifespan in ``main.py`` always wires
    one). 503 tells the operator the dependency is temporarily
    unavailable rather than masking the failure as a 404.

    Why a typed ``getattr`` and not the infrastructure class: the
    dispatcher lives in ``app.infrastructure.messaging`` which this
    module must not import (import-linter "Interfaces do not import
    infrastructure directly"). The ``OutboxAdminService`` Protocol
    carries the ``count_lag`` / ``list_pending`` / ``replay`` surface
    the console needs at type-check time; the composition root
    guarantees the installed object satisfies it at runtime. No
    ``cast`` is needed — the annotation narrows the attribute access
    to the Protocol without importing the dispatcher.
    """
    service: OutboxAdminService | None = getattr(request.app.state, "outbox_dispatcher", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "dependency_unavailable",
                "message": "outbox dispatcher is not configured",
            },
        )
    return service


def _to_row_response(row: OutboxRowView) -> OutboxRowResponse:
    return OutboxRowResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        event_type=row.event_type,
        event_id=row.event_id,
        created_at=row.created_at,
        dispatched_at=row.dispatched_at,
        attempt_count=row.attempt_count,
        last_error=row.last_error,
        is_dead_lettered=row.attempt_count >= OUTBOX_MAX_ATTEMPTS,
    )


@router.get(
    "",
    response_model=OutboxListResponse,
    summary="Outbox lag + a page of pending rows",
)
async def get_outbox(
    request: Request,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AdminOutboxHttpDependencyProvider = Depends(get_admin_outbox_dependencies),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum rows in the page (default 50, max 200)",
    ),
    include_dead_lettered: bool = Query(
        default=True,
        description=(
            "Include rows that have hit MAX_ATTEMPTS. Default true so "
            "the operator sees poison messages; set false for a "
            "non-dead-lettered-only view."
        ),
    ),
) -> OutboxListResponse:
    """Return outbox lag counters and a page of pending rows."""
    role_repo = dependencies.for_session(db).role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="outbox"),
        role_repository=role_repo,
    )
    service = _outbox_service(request)
    lag = await service.count_lag()
    rows = await service.list_pending(
        include_dead_lettered=include_dead_lettered,
        limit=limit,
    )
    return OutboxListResponse(
        items=[_to_row_response(r) for r in rows],
        limit=limit,
        lag=OutboxLagSummary(**lag),
    )


@router.post(
    "/{row_id}/replay",
    response_model=OutboxReplayResponse,
    summary="Reset a row's retry state so the next dispatcher tick re-attempts it",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks admin.tenant_config"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Outbox dispatcher is not configured"},
    },
)
async def replay_outbox_row(
    row_id: UUID,
    request: Request,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AdminOutboxHttpDependencyProvider = Depends(get_admin_outbox_dependencies),
) -> OutboxReplayResponse:
    """Replay a single outbox row.

    Idempotent: a second replay of the same row, before the dispatcher
    has had a chance to attempt it again, returns ``reset=False`` only
    if the row was already dispatched in between; otherwise it returns
    ``reset=True`` once and ``reset=False`` thereafter (the second
    reset is a no-op because ``attempt_count`` is already 0 — the
    dispatcher's ``tick`` will pick it up).
    """
    role_repo = dependencies.for_session(db).role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="outbox"),
        role_repository=role_repo,
    )
    service = _outbox_service(request)
    reset = await service.replay(row_id)
    return OutboxReplayResponse(id=row_id, reset=reset)


__all__ = ["router"]
