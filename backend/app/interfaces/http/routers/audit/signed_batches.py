"""Signed audit batch routes — ``GET/POST /api/v1/audit/signed-batches`` (P09-US9; ADR-009).

Externally-verifiable signed batch surface for SIEM-style auditors:

- ``GET /api/v1/audit/signed-batches`` — list the per-tenant signed
  batches.
- ``POST /api/v1/audit/signed-batches`` — build and persist a signed
  batch over an inclusive event span; the chain is verified against the
  durable ``audit_events`` rows before signing.

Both routes authorize ``Action.AUDIT_STREAM``, which is **Gated** per
ADR-009 (no role grants it today) — the wire contract exists so the
auditor's tooling is stable while the operator flips the grant when the
Azure SIEM target is selected.

``get_signed_batch_repository`` resolves the per-request
``SignedBatchRepository`` from ``app.state`` so a production
deployment can swap in the Azure tamper-evident blob store without
code changes (the application port stays the same).

Registers onto the package :mod:`common` audit ``router``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Body, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit import siem_stream as siem_stream_uc
from app.application.audit.http_dependencies import AuditHttpDependencyProvider
from app.application.authorization import Action, Resource, authorize
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.domain.errors import ValidationFailed
from app.interfaces.http.dependencies import get_audit_dependencies
from app.interfaces.http.schemas.audit import (
    SignedBatchPage,
    SignedBatchRequest,
    SignedBatchResponse,
)

from .common import router


def _batch_to_response(batch: siem_stream_uc.SignedBatch) -> SignedBatchResponse:
    """Translate the application dataclass into the wire schema.

    Same rationale as ``_entry_to_response``: Pydantic is the
    OpenAPI contract; hand-editing the fields at the router is
    forbidden.

    Args:
        batch: Application-layer signed batch dataclass.

    Returns:
        The wire ``SignedBatchResponse``.
    """
    return SignedBatchResponse.model_validate(
        {
            "id": batch.id,
            "tenant_id": batch.tenant_id,
            "first_event_id": batch.first_event_id,
            "last_event_id": batch.last_event_id,
            "first_event_hash": batch.first_event_hash,
            "last_event_hash": batch.last_event_hash,
            "row_count": batch.row_count,
            "algorithm": batch.algorithm,
            "key_id": batch.key_id,
            "signature": batch.signature,
            "payload": batch.payload,
            "created_at": batch.created_at.isoformat(),
            "created_by": batch.created_by,
        }
    )


def get_signed_batch_repository(request: Request) -> siem_stream_uc.SignedBatchRepository:
    """Resolve the per-request ``SignedBatchRepository``.

    The local adapter (``InMemorySignedBatchRepository``) is wired
    on ``app.state`` during startup when the production target is
    not yet selected. The Azure tamper-evident blob store replaces
    this seam once ADR-009 is approved; the application port
    (``SignedBatchRepository``) does not change.

    Args:
        request: Current HTTP request (used to reach ``app.state``).

    Returns:
        The configured signed-batch repository.

    Raises:
        HTTPException: 503 when no repository was wired at composition
            time (startup invariant — fail closed, never guess).
    """
    repo = getattr(request.app.state, "signed_batch_repository", None)
    if repo is None:  # pragma: no cover — startup invariant
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "dependency_unavailable",
                "message": "signed batch repository is not configured",
            },
        )
    return repo  # type: ignore[return-value]


@router.get(
    "/audit/signed-batches",
    response_model=SignedBatchPage,
    summary="List tamper-evident signed audit batches (P09-US9, Gated)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks audit.stream (Gated per ADR-009)"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Signed-batch repository is not configured"
        },
    },
)
async def get_signed_batches(
    user: UserContext = Depends(get_current_user),
    repository: siem_stream_uc.SignedBatchRepository = Depends(get_signed_batch_repository),
    limit: Annotated[
        int,
        Query(ge=1, le=200, description="Maximum number of batches to return"),
    ] = siem_stream_uc.DEFAULT_PAGE_LIMIT,
) -> SignedBatchPage:
    """Return the per-tenant list of signed audit batches (P09-US9 / FM-046).

    Authorization: ``audit.stream`` is required. The action is
    **Gated** per ADR-009: no role grants it today. The endpoint
    exists so the auditor's tooling has a stable wire contract; the
    operator flips the role grant when the Azure SIEM target is
    selected.

    Args:
        user: Authenticated caller (identity + tenant + roles).
        repository: Per-request signed-batch repository seam.
        limit: Maximum number of batches (1..200).

    Returns:
        The tenant's signed batches (``next_cursor`` is ``None`` — the
        list endpoint is not cursor-paginated).

    Raises:
        HTTPException: 403 when ``audit.stream`` is missing, 503 when
            the repository is not configured.
    """

    # Authorization is evaluated up-front so a forbidden actor does
    # not see the count of batches. The use case would also
    # authorize, but the router is the boundary that hides 404 vs
    # 403 from a probing client (Constitution V).
    await authorize(
        Action.AUDIT_STREAM,
        user,
        Resource(tenant_id=user.tenant_id, kind="audit_siem"),
    )

    batches = await repository.list(tenant_id=user.tenant_id, limit=limit)
    return SignedBatchPage(
        items=[_batch_to_response(b) for b in batches],
        next_cursor=None,
        limit=limit,
    )


@router.post(
    "/audit/signed-batches",
    response_model=SignedBatchResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a signed audit batch over an inclusive span (P09-US9, Gated)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks audit.stream (Gated per ADR-009)"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Span empty, out of order, or chain mismatch"
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Signed-batch repository is not configured"
        },
    },
)
async def post_signed_batch(
    body: Annotated[
        SignedBatchRequest,
        Body(
            description=(
                "Inclusive event range and the reason for the batch. "
                "The chain is verified against the durable audit_events "
                "rows before signing."
            )
        ),
    ],
    clock: Annotated[Clock, Depends(get_clock)],
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AuditHttpDependencyProvider = Depends(get_audit_dependencies),
    repository: siem_stream_uc.SignedBatchRepository = Depends(get_signed_batch_repository),
) -> SignedBatchResponse:
    """Build and persist a signed batch over ``[first, last]`` (P09-US9).

    The chain is verified against the durable ``audit_events`` rows
    before signing so a misconfigured caller cannot attest a chain
    that does not exist on-disk. The signed batch is persisted via
    the per-request ``SignedBatchRepository``; the auditor's tooling
    re-reads the rows through the standard audit read API to
    recompute the chain and verify the signature.

    Args:
        body: Inclusive event range and the reason for the batch.
        clock: Wall clock stamped onto the signed batch.
        user: Authenticated caller (identity + tenant + roles).
        db: Request-scoped async DB session.
        dependencies: Audit repository bundle for this session.
        repository: Per-request signed-batch repository seam.

    Returns:
        The persisted signed batch.

    Raises:
        HTTPException: 403 when ``audit.stream`` is missing, 422 on an
            empty/out-of-order span or chain mismatch, 503 when the
            repository is not configured.
    """

    repositories = dependencies.for_session(db)
    try:
        batch = await siem_stream_uc.build_signed_batch(
            siem_stream_uc.BuildBatchCommand(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                first_event_id=body.first_event_id,
                last_event_id=body.last_event_id,
                reason=body.reason,
                correlation_id=body.correlation_id,
            ),
            audit=repositories.audit,
            repository=repository,
            clock=clock,
        )
    except ValidationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "validation_failed",
                "message": str(exc),
                "field": exc.field,
            },
        ) from exc
    return _batch_to_response(batch)
