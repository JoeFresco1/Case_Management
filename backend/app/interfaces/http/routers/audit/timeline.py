"""Case audit timeline route — ``GET /api/v1/cases/{case_id}/timeline`` (P09-US1 / US4 / FM-043).

This is the only HTTP entry to the per-case audit timeline. The
endpoint is authz-checked (``Action.AUDIT_READ``), tenant-scoped, and
returns a paginated, redacted, chronologically-ordered page of audit
events (AGENTS.md §17.1, plan §4.R9).

The handler delegates the entire business decision to
``app.application.audit.view_timeline.handle``. The router's job is
limited to:

- parsing and clamping the query parameters;
- building the application-layer ``ViewTimelineQuery``;
- mapping the use case's ``TimelinePage`` to the wire ``TimelinePage``;
- translating domain errors via the existing handler
  (``interfaces/http/errors.py``).

Registers onto the package :mod:`common` audit ``router``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit import view_timeline as view_timeline_uc
from app.application.audit.http_dependencies import AuditHttpDependencyProvider
from app.deps import UserContext, get_current_user, get_db
from app.domain.audit import ALL_EVENT_TYPES
from app.domain.errors import NotFound, ValidationFailed
from app.interfaces.http.dependencies import get_audit_dependencies
from app.interfaces.http.schemas.audit import TimelinePage

from .common import _entry_to_response, router


@router.get(
    "/cases/{case_id}/timeline",
    response_model=TimelinePage,
    summary="Case audit timeline (chronological, paginated, redacted)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks audit.read"},
        status.HTTP_404_NOT_FOUND: {"description": "Case not found in tenant"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Malformed filter or cursor"},
    },
)
async def get_case_timeline(
    case_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AuditHttpDependencyProvider = Depends(get_audit_dependencies),
    # ``limit`` mirrors AGENTS.md §17.1 — default 50, max 200.
    limit: Annotated[int, Query(ge=1, le=200)] = (view_timeline_uc.DEFAULT_PAGE_LIMIT),
    # Multi-select event-type filter (P09-US4). Repeating the query
    # parameter yields a list — FastAPI handles the parsing.
    event_type: Annotated[
        list[str] | None,
        Query(alias="event_type", description="Filter by audit event type"),
    ] = None,
    actor_id: Annotated[
        UUID | None,
        Query(description="Filter by actor id (UUID)"),
    ] = None,
    since: Annotated[
        datetime | None,
        Query(description="Inclusive lower bound on occurred_at (ISO-8601)"),
    ] = None,
    until: Annotated[
        datetime | None,
        Query(description="Inclusive upper bound on occurred_at (ISO-8601)"),
    ] = None,
    cursor: Annotated[
        str | None,
        Query(description="Opaque pagination cursor from a prior page"),
    ] = None,
) -> TimelinePage:
    """Return the case's audit timeline.

    Events are returned in chronological order (oldest first). Each
    event carries actor, timestamp, event type, payload (with sensitive
    values redacted to ``"***"``), the chain hash anchors, and the
    case-type / workflow version that was in effect at write time.

    Authorization: ``AUDIT_READ`` is required. Cross-tenant ``case_id``
    values surface as 404 — the actor cannot probe for the existence
    of a case they cannot read (Constitution V).

    Args:
        case_id: Case whose timeline is requested (tenant-scoped).
        user: Authenticated caller (identity + tenant + roles).
        db: Request-scoped async DB session.
        dependencies: Audit repository bundle for this session.
        limit: Page size (1..200; default 50).
        event_type: Optional multi-select event-type filter.
        actor_id: Optional filter on the event's actor id.
        since: Inclusive lower bound on ``occurred_at``.
        until: Inclusive upper bound on ``occurred_at``.
        cursor: Opaque pagination cursor from a prior page.

    Returns:
        One page of timeline entries, oldest first.

    Raises:
        HTTPException: 403 when ``AUDIT_READ`` is missing, 404 when the
            case is absent or in another tenant, 422 on unknown event
            types / malformed cursor / invalid filter.
    """
    repositories = dependencies.for_session(db)
    # Defensive validation: catch unknown event types before the use
    # case so the OpenAPI document's error model stays accurate. The
    # use case also validates; this is a UX-friendly 422 instead of a
    # 500 if the validator's exception type ever drifts.
    if event_type:
        unknown = [t for t in event_type if t not in ALL_EVENT_TYPES]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "validation_failed",
                    "message": f"unknown event types: {unknown!r}",
                    "field": "event_type",
                },
            )
    # Cursor decoding happens inside the SQL adapter; a malformed
    # cursor raises ValueError there. We probe it here so the error
    # surfaces as a structured 422 instead of a 500.
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
        page = await view_timeline_uc.handle(
            view_timeline_uc.ViewTimelineQuery(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                case_id=case_id,
                event_types=tuple(event_type) if event_type else None,
                actor_filter=actor_id,
                since=since,
                until=until,
                limit=limit,
                cursor=cursor,
            ),
            audit=repositories.audit,
            case_repo=repositories.case,
            cursor_encoder=repositories.cursor_encoder,
        )
    except ValidationFailed as exc:
        # Belt-and-suspenders: the use case also validates; the router
        # translation ensures the structured error model is correct
        # even if the validator is later moved.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "validation_failed",
                "message": str(exc),
                "field": exc.field,
            },
        ) from exc
    except NotFound as exc:
        # Use case raised NotFound. We surface the canonical 404 — the
        # actor cannot distinguish "case in another tenant" from
        # "case does not exist".
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "not_found",
                "message": f"case {case_id} not found",
            },
        ) from exc

    items = [_entry_to_response(e) for e in page.items]
    return TimelinePage(items=items, next_cursor=page.next_cursor, limit=page.limit)
