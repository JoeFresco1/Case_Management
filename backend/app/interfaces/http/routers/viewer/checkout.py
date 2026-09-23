"""Viewer checkout routes: persistent exclusive lock with holder + expiry.

# INTEGRATION_SEAM: HTTP entry

Checkout gives one actor an exclusive editing lease on a document. A
live foreign lock raises ``CheckoutConflict`` (a domain ``Conflict``);
these handlers render the structured conflict inline (holder + expiry)
so the 409 body is stable even if the global handler changes. The
checkout trail stays readable after release for the "who held the
document when" history.

Endpoints (all under the shared ``/documents`` router):

- ``POST /api/v1/documents/{document_id}/checkout`` — acquire the
  checkout, or renew it when the holder re-checks-out (idempotency
  required; foreign lock → 409 with holder + expiry).
- ``POST /api/v1/documents/{document_id}/checkin`` — release the
  holder's checkout; the row stays for history (idempotency required).
- ``GET  /api/v1/documents/{document_id}/checkout/history`` — the
  checkout trail, newest first.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.documents import viewer as viewer_uc
from app.application.documents.http_dependencies import ViewerHttpDependencyProvider
from app.deps import (
    Clock,
    UserContext,
    get_clock,
    get_current_user,
    get_db,
)
from app.domain.annotations import CheckoutConflict
from app.interfaces.http.dependencies import get_viewer_dependencies
from app.interfaces.http.schemas.viewer import (
    CheckoutRequest,
    CheckoutResponse,
    lock_to_response,
)

from .common import _require_idempotency_key, router


@router.post(
    "/{document_id}/checkout",
    response_model=CheckoutResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Check out a document for exclusive editing",
    responses={
        status.HTTP_200_OK: {"description": "Holder renewed their own checkout"},
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.checkout"},
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
        status.HTTP_409_CONFLICT: {"description": "Live foreign checkout (holder + expiry)"},
    },
)
async def checkout_document(
    document_id: UUID,
    body: CheckoutRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ViewerHttpDependencyProvider = Depends(get_viewer_dependencies),
    clock: Clock = Depends(get_clock),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CheckoutResponse:
    """Acquire the checkout, or renew it when the holder re-checks-out.

    A live foreign lock raises ``CheckoutConflict`` → 409 with
    ``details.holder`` + ``details.expires_at`` (T5 acceptance).
    """
    _require_idempotency_key(idempotency_key)
    repos = dependencies.for_session(db)
    try:
        result = await viewer_uc.checkout_document(
            viewer_uc.CheckoutCommand(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                document_id=document_id,
                lease_seconds=body.lease_seconds,
            ),
            document_repo=repos.document,
            checkout_repo=repos.checkout,
            audit=repos.audit,
            outbox=repos.outbox,
            clock=clock,
        )
    except CheckoutConflict as exc:
        # why: render the structured conflict inline (holder + expiry)
        # rather than relying on the generic Conflict mapping, so the
        # 409 body is stable even if the global handler changes.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "conflict",
                "message": str(exc),
                **dict(exc.to_details()),
            },
        ) from exc
    await db.commit()
    return lock_to_response(result.lock, renewed=result.renewed)


@router.post(
    "/{document_id}/checkin",
    response_model=CheckoutResponse,
    summary="Check in a document (release the holder's checkout)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.checkout"},
        status.HTTP_404_NOT_FOUND: {"description": "No active checkout in tenant"},
        status.HTTP_409_CONFLICT: {"description": "Checkout held by someone else"},
    },
)
async def checkin_document(
    document_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ViewerHttpDependencyProvider = Depends(get_viewer_dependencies),
    clock: Clock = Depends(get_clock),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CheckoutResponse:
    """Release the holder's checkout; the row stays for history."""
    _require_idempotency_key(idempotency_key)
    repos = dependencies.for_session(db)
    try:
        result = await viewer_uc.checkin_document(
            viewer_uc.CheckinCommand(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                document_id=document_id,
            ),
            document_repo=repos.document,
            checkout_repo=repos.checkout,
            audit=repos.audit,
            outbox=repos.outbox,
            clock=clock,
        )
    except CheckoutConflict as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "conflict",
                "message": str(exc),
                **dict(exc.to_details()),
            },
        ) from exc
    await db.commit()
    return lock_to_response(result.lock, renewed=False)


@router.get(
    "/{document_id}/checkout/history",
    response_model=list[CheckoutResponse],
    summary="Checkout history for a document (newest first)",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
    },
)
async def checkout_history(
    document_id: UUID,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ViewerHttpDependencyProvider = Depends(get_viewer_dependencies),
) -> list[CheckoutResponse]:
    """Return the checkout trail: who held the document when."""
    from app.application.authorization import (  # noqa: PLC0415
        Action,
        Resource,
        authorize,
    )

    await authorize(
        Action.DOCUMENT_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="document", id=document_id),
    )
    repos = dependencies.for_session(db)
    document = await repos.document.get(user.tenant_id, document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "not_found",
                "message": f"document {document_id} not found",
            },
        )
    trail = await repos.checkout.list_history(
        user.tenant_id, document_id, limit=limit, offset=offset
    )
    return [lock_to_response(lock, renewed=False) for lock in trail]


__all__ = ["checkin_document", "checkout_document", "checkout_history"]
