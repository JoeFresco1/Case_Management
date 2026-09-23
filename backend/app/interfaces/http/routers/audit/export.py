"""Structured audit export route — ``GET /api/v1/audit/export`` (P09-US5 / US7; FM-044).

Returns one page of the structured audit export. Every row carries its
``hash`` and ``prev_hash`` so a downstream auditor can verify the chain
off-system (P09-US3). The payload is redacted through the same
allowlist as the timeline; the chain anchors are integrity metadata and
are NOT redacted.

The export itself appends an ``AuditExported`` row to the chain (inside
the use case) so the action is visible on the timeline (P09-US5). The
router only parses/clamps the query parameters, builds the
application-layer ``ExportQuery``, and translates domain errors.

Registers onto the package :mod:`common` audit ``router``.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit import export_audit as export_audit_uc
from app.application.audit.http_dependencies import AuditHttpDependencyProvider
from app.deps import UserContext, get_current_user, get_db
from app.domain.errors import ValidationFailed
from app.interfaces.http.dependencies import get_audit_dependencies
from app.interfaces.http.schemas.audit import ExportBatchResponse, ExportEntryResponse

from .common import router


@router.get(
    "/audit/export",
    response_model=ExportBatchResponse,
    summary="Structured audit export with hash chain anchors",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks audit.export"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Malformed filter, cursor, or reason"
        },
    },
)
async def get_audit_export(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AuditHttpDependencyProvider = Depends(get_audit_dependencies),
    case_id: Annotated[
        UUID | None,
        Query(description=("Optional case scope. Omit for a tenant-wide date-range export.")),
    ] = None,
    since: Annotated[
        datetime | None,
        Query(description="Inclusive lower bound on occurred_at (ISO-8601)"),
    ] = None,
    until: Annotated[
        datetime | None,
        Query(description="Inclusive upper bound on occurred_at (ISO-8601)"),
    ] = None,
    reason: Annotated[
        str,
        Query(
            min_length=1,
            description=(
                "Free-text justification recorded on the AuditExported "
                "event that this call appends to the chain."
            ),
        ),
    ] = "unspecified",
    limit: Annotated[int, Query(ge=1, le=200)] = export_audit_uc.DEFAULT_PAGE_LIMIT,
    cursor: Annotated[
        str | None,
        Query(description="Opaque pagination cursor from a prior page"),
    ] = None,
) -> ExportBatchResponse:
    """Return one page of the structured audit export (P09-US5 / FM-044).

    Every row carries its ``hash`` and ``prev_hash`` so a downstream
    auditor can verify the chain off-system (P09-US3). The payload is
    redacted through the same allowlist as the timeline; the chain
    anchors are integrity metadata and are NOT redacted.

    The export itself appends an ``AuditExported`` row to the chain so
    the action is visible on the timeline (P09-US5).

    Args:
        user: Authenticated caller (identity + tenant + roles).
        db: Request-scoped async DB session.
        dependencies: Audit repository bundle for this session.
        case_id: Optional case scope; omit for tenant-wide export.
        since: Inclusive lower bound on ``occurred_at``.
        until: Inclusive upper bound on ``occurred_at``.
        reason: Justification recorded on the ``AuditExported`` row.
        limit: Page size (1..200; default from the use case).
        cursor: Opaque pagination cursor from a prior page.

    Returns:
        One export page with chain anchors per row.

    Raises:
        HTTPException: 403 when ``audit.export`` is missing, 422 on a
            malformed filter, cursor, or reason.
    """
    repositories = dependencies.for_session(db)
    # Validate the cursor early so a malformed token surfaces as 422
    # instead of 500. The SQL adapter re-decodes it on the way in.
    if cursor:
        try:
            repositories.cursor_decoder(cursor)
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "validation_failed",
                    "message": f"malformed cursor: {exc}",
                    "field": "cursor",
                },
            ) from exc

    try:
        batch = await export_audit_uc.handle(
            export_audit_uc.ExportQuery(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                case_id=case_id,
                since=since,
                until=until,
                reason=reason,
                limit=limit,
                cursor=cursor,
            ),
            audit=repositories.audit,
            cursor_encoder=repositories.cursor_encoder,
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

    return ExportBatchResponse(
        items=[ExportEntryResponse.model_validate(asdict(e)) for e in batch.items],
        next_cursor=batch.next_cursor,
        limit=batch.limit,
        rows_in_batch=batch.rows_in_batch,
    )
