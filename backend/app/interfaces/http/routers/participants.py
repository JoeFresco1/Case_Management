"""Tenant-scoped participant endpoints for case workspaces."""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.participants.http_dependencies import ParticipantHttpDependencyProvider
from app.application.participants.manage_participants import (
    AddParticipantCommand,
    RemoveParticipantCommand,
    UpdateParticipantCommand,
    add_participant,
    remove_participant,
    update_participant,
)
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.domain.participant import Participant
from app.interfaces.http.dependencies import get_participant_dependencies
from app.interfaces.http.schemas.participants import (
    ParticipantPatch,
    ParticipantRequest,
    ParticipantResponse,
)

router = APIRouter(prefix="/cases/{case_id}/participants", tags=["participants"])


def _response(participant: Participant) -> ParticipantResponse:
    """Project a domain participant to its wire contract."""
    return ParticipantResponse(
        id=participant.id,
        tenant_id=participant.tenant_id,
        case_id=participant.case_id,
        role=participant.role,
        display_name=participant.display_name,
        source=participant.source,
        external_ref=participant.external_ref,
        is_primary=participant.is_primary,
        added_at=participant.added_at,
        removed_at=participant.removed_at,
    )


@router.post(
    "",
    response_model=ParticipantResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a participant to a case",
)
async def add(
    body: ParticipantRequest,
    case_id: UUID = Path(...),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    correlation_id: str = Header(default="", alias="X-Correlation-Id"),
    dependencies: ParticipantHttpDependencyProvider = Depends(get_participant_dependencies),
) -> ParticipantResponse:
    """Add a tenant-scoped participant and record its audit event."""
    result = await add_participant(
        AddParticipantCommand(
            tenant_id=user.tenant_id,
            case_id=case_id,
            actor=user,
            role=body.role,
            display_name=body.display_name,
            source=body.source,
            external_ref=body.external_ref,
            is_primary=body.is_primary,
            correlation_id=correlation_id,
        ),
        case_repo=dependencies.for_session(db).case,
        participant_repo=dependencies.for_session(db).participant,
        audit=dependencies.for_session(db).audit,
        outbox=dependencies.for_session(db).outbox,
        clock=clock,
    )
    await db.commit()
    return _response(result)


@router.patch(
    "/{participant_id}", response_model=ParticipantResponse, summary="Update a participant"
)
async def update(
    case_id: UUID,
    participant_id: UUID,
    body: ParticipantPatch,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    correlation_id: str = Header(default="", alias="X-Correlation-Id"),
    dependencies: ParticipantHttpDependencyProvider = Depends(get_participant_dependencies),
) -> ParticipantResponse:
    """Update participant metadata; the case id is checked against stored ownership."""
    result = await update_participant(
        UpdateParticipantCommand(
            tenant_id=user.tenant_id,
            participant_id=participant_id,
            case_id=case_id,
            actor=user,
            role=body.role,
            display_name=body.display_name,
            source=body.source,
            external_ref=body.external_ref,
            is_primary=body.is_primary,
            correlation_id=correlation_id,
        ),
        participant_repo=dependencies.for_session(db).participant,
        audit=dependencies.for_session(db).audit,
        outbox=dependencies.for_session(db).outbox,
        clock=clock,
    )
    await db.commit()
    return _response(result)


@router.delete(
    "/{participant_id}", response_model=ParticipantResponse, summary="Remove a participant"
)
async def remove(
    case_id: UUID,
    participant_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    correlation_id: str = Header(default="", alias="X-Correlation-Id"),
    dependencies: ParticipantHttpDependencyProvider = Depends(get_participant_dependencies),
) -> ParticipantResponse:
    """Logically remove a participant while retaining historical references."""
    result = await remove_participant(
        RemoveParticipantCommand(
            tenant_id=user.tenant_id,
            participant_id=participant_id,
            case_id=case_id,
            actor=user,
            correlation_id=correlation_id,
        ),
        participant_repo=dependencies.for_session(db).participant,
        audit=dependencies.for_session(db).audit,
        outbox=dependencies.for_session(db).outbox,
        clock=clock,
    )
    await db.commit()
    return _response(result)


__all__ = ["router"]
