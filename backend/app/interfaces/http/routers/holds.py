"""Legal hold router (P08-US1, US2 / FM-039).

Endpoints:

- ``POST /api/v1/cases/{id}/holds`` — place a legal hold. Requires
  ``Action.CASE_HOLD_PLACE`` (granted only to ``process_admin``).
  Body: ``{"reason": "<text>"}``. Returns the freshly created
  :class:`LegalHoldResponse`.
- ``DELETE /api/v1/cases/{id}/holds`` — release the active legal hold.
  Requires ``Action.CASE_HOLD_RELEASE`` (granted only to
  ``process_admin``). Body: ``{"reason": "<text>"}``. Returns the
  released :class:`LegalHoldResponse`.

# Authorization

Both endpoints require explicit grants on the hold-lifecycle actions
because they are security-administrator operations, not ordinary
case-worker operations. The deny-by-default policy raises
:class:`Forbidden` on a missing role, which the HTTP layer maps to
403 (AGENTS.md §4.R12 / interfaces/http/errors.py).

# Transaction shape

The use case stages the case ``UPDATE`` + the ``legal_holds`` row +
the ``HoldPlaced`` / ``HoldReleased`` audit row in the caller's DB
transaction. The HTTP boundary commits once after the use case
returns; on rollback everything is reverted together.

# INTEGRATION_SEAM

The :class:`InMemoryLegalHoldRepository` (under
``app.infrastructure.persistence.legal_hold_repository``) is the
seam placeholder for the H1 slice; the SQL adapter lands with the
follow-up dispatch. The router depends on the Protocol from
``app.domain.ports`` and the concrete adapter is wired through
``app.state`` so a future SQL adapter swaps in transparently.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.holds.http_dependencies import HoldHttpDependencyProvider
from app.application.holds.manage_holds import (
    PlaceHoldCommand,
    ReleaseHoldCommand,
    place_hold,
    release_hold,
)
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.domain.legal_hold import LegalHold
from app.domain.ports import LegalHoldRepository
from app.interfaces.http.dependencies import get_hold_dependencies
from app.interfaces.http.schemas.holds import (
    LegalHoldResponse,
    PlaceHoldRequest,
    ReleaseHoldRequest,
)

router = APIRouter(prefix="/cases", tags=["cases", "legal-hold"])


def _require_idempotency_key(value: str | None) -> str:
    """Enforce the ``Idempotency-Key`` contract at the router boundary.

    The global :class:`IdempotencyMiddleware` is the transport-level replay
    mechanism, but router-level enforcement keeps the contract explicit in
    OpenAPI and deterministic under test clients that disable the
    middleware (``scope_paths=()``). A missing/blank key is 400 so a
    retry without a key never reaches the use case.
    """
    if not value or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    if len(value.strip()) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key is too long",
        )
    return value.strip()


def _to_response(hold: LegalHold) -> LegalHoldResponse:
    """Project the domain ``LegalHold`` onto the wire response shape."""
    return LegalHoldResponse(
        id=hold.id,
        tenant_id=hold.tenant_id,
        case_id=hold.case_id,
        placed_by=hold.placed_by,
        placed_reason=hold.placed_reason,
        placed_at=hold.placed_at,
        released_by=hold.released_by,
        released_reason=hold.released_reason,
        released_at=hold.released_at,
        is_active=hold.is_active,
    )


def _hold_repository(request: Request) -> LegalHoldRepository:
    """Return the process-scoped hold repository from ``app.state``.

    The repository is registered on ``app.state`` by the lifespan in
    :mod:`app.main` so a single shared instance is visible across
    requests for the lifetime of the process. The router depends only
    on the ``LegalHoldRepository`` domain port so a future SQL adapter
    swaps in transparently.

    Raises:
        RuntimeError: if an app was created without the shared repository.
    """
    repo = getattr(request.app.state, "legal_hold_repository", None)
    if repo is None:
        raise RuntimeError("legal hold repository is not configured")
    return cast(LegalHoldRepository, repo)


@router.post(
    "/{case_id}/holds",
    response_model=LegalHoldResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Place a legal hold on a case",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.hold_place"},
        status.HTTP_404_NOT_FOUND: {"description": "Case not found in tenant"},
        status.HTTP_409_CONFLICT: {
            "description": "Active hold already present, or key reuse conflict"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Missing reason"},
    },
)
async def place_case_hold(
    case_id: UUID = Path(..., description="Target case id (tenant-scoped)"),
    body: PlaceHoldRequest = ...,
    request: Request = ...,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: HoldHttpDependencyProvider = Depends(get_hold_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> LegalHoldResponse:
    """Place a legal hold on the case.

    The use case refuses a second placement with a structured
    ``HoldAlreadyPresent`` error (409). The case's ``legal_hold``
    flag is flipped to ``True`` in the same transaction as the
    ``legal_holds`` insert and the ``HoldPlaced`` audit row.

    Contracts: ``Idempotency-Key`` is required (transport retries replay
    via ``IdempotencyMiddleware``); stale/concurrent double-place
    surfaces as 409 ``ErrorResponse{conflict}``.
    """
    _require_idempotency_key(idempotency_key)
    cmd = PlaceHoldCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        case_id=case_id,
        reason=body.reason,
    )
    repositories = dependencies.for_session(db)
    result = await place_hold(
        cmd,
        case_repo=repositories.case,
        hold_repo=_hold_repository(request),
        audit=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return _to_response(result.hold)


@router.delete(
    "/{case_id}/holds",
    response_model=LegalHoldResponse,
    summary="Release the active legal hold on a case",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.hold_release"},
        status.HTTP_404_NOT_FOUND: {"description": "Case not found in tenant"},
        status.HTTP_409_CONFLICT: {
            "description": "No active hold to release, or key reuse conflict"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Missing reason"},
    },
)
async def release_case_hold(
    case_id: UUID = Path(..., description="Target case id (tenant-scoped)"),
    body: ReleaseHoldRequest = ...,
    request: Request = ...,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: HoldHttpDependencyProvider = Depends(get_hold_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> LegalHoldResponse:
    """Release the active legal hold on the case.

    The use case refuses a release against a case with no active
    hold with a structured ``HoldNotPresent`` error (409). The
    case's ``legal_hold`` flag is flipped back to ``False`` in the
    same transaction as the ``legal_holds`` update and the
    ``HoldReleased`` audit row.

    Contracts: ``Idempotency-Key`` is required even on DELETE so a
    retried release replays instead of double-mutating; concurrent
    release-while-absent surfaces as 409 ``ErrorResponse{conflict}``.
    """
    _require_idempotency_key(idempotency_key)
    cmd = ReleaseHoldCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        case_id=case_id,
        reason=body.reason,
    )
    repositories = dependencies.for_session(db)
    result = await release_hold(
        cmd,
        case_repo=repositories.case,
        hold_repo=_hold_repository(request),
        audit=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return _to_response(result.hold)


__all__ = ["router"]
