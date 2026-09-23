"""Audit correction route — ``POST /api/v1/audit/{event_id}/correction`` (P09-US7; FM-044).

Appends a compensating ``AuditCorrectionRecorded`` row. The original
event is **never** modified: the new row references the original by id
and carries the reason and the corrected summary. The append happens in
the same DB transaction as the read (defense-in-depth: the audit table
is append-only at four layers, so this is the only legal way to
"correct" history).

The router only parses the body, builds the application-layer
``CorrectAuditCommand``, translates domain errors, and re-reads the
freshly appended row so the response carries the committed row id.

Registers onto the package :mod:`common` audit ``router``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Body, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit import correct_audit as correct_audit_uc
from app.application.audit.http_dependencies import AuditHttpDependencyProvider
from app.deps import UserContext, get_current_user, get_db
from app.domain.errors import NotFound, ValidationFailed
from app.interfaces.http.dependencies import get_audit_dependencies
from app.interfaces.http.schemas.audit import CorrectionRequest, CorrectionResponse

from .common import router


@router.post(
    "/audit/{event_id}/correction",
    response_model=CorrectionResponse,
    summary="Append a compensating AuditCorrectionRecorded row",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks audit.correct"},
        status.HTTP_404_NOT_FOUND: {"description": "Original event not found in tenant"},
        status.HTTP_409_CONFLICT: {"description": "Original event is itself a correction"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Malformed correction request"},
    },
)
async def post_audit_correction(
    event_id: UUID,
    body: Annotated[
        CorrectionRequest,
        Body(
            description=(
                "Reason, corrected_summary, and correlation_id for the "
                "compensating row. The original event is never modified."
            )
        ),
    ],
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AuditHttpDependencyProvider = Depends(get_audit_dependencies),
) -> CorrectionResponse:
    """Append a compensating ``AuditCorrectionRecorded`` row (P09-US7).

    The original event is **never** modified. The new row references
    the original by id and carries the reason and the corrected
    summary. The append happens in the same DB transaction as the
    read (defense-in-depth: the audit table is append-only at four
    layers, so this is the only legal way to "correct" history).

    Args:
        event_id: Id of the original (non-correction) audit event.
        body: Reason, corrected summary, and correlation id.
        user: Authenticated caller (identity + tenant + roles).
        db: Request-scoped async DB session.
        dependencies: Audit repository bundle for this session.

    Returns:
        The original event id, the newly appended correction event id,
        and the correction payload written to the chain.

    Raises:
        HTTPException: 403 when ``audit.correct`` is missing, 404 when
            the original event is absent or cross-tenant, 409 when the
            target is itself a correction, 422 on a malformed body.
    """
    repositories = dependencies.for_session(db)

    try:
        result = await correct_audit_uc.handle(
            correct_audit_uc.CorrectAuditCommand(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                original_event_id=event_id,
                reason=body.reason,
                corrected_summary=body.corrected_summary,
                correlation_id=body.correlation_id,
                occurred_at=datetime.now(UTC),
            ),
            audit=repositories.audit,
            dedupe=repositories.dedupe,
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
    except NotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": str(exc)},
        ) from exc

    # Re-read the freshly appended row so the response carries the
    # committed row id. Same transaction so the read sees the staged
    # append. The use case leaves ``new_event_id`` as ``None`` so the
    # application layer never invents a row id.
    new_event_id = await repositories.correction_lookup.latest_correction_id(
        tenant_id=user.tenant_id,
        correlation_id=body.correlation_id,
    )

    return CorrectionResponse(
        original_event_id=result.original_event_id,
        new_event_id=new_event_id,
        correction_payload=result.correction_payload,
    )
