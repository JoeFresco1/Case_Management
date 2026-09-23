"""Case relationships router (P01-US5, US7, US8 / FM-010).

Mounts the three relationship use cases under
``/api/v1/cases/{case_id}/...``:

- ``POST /cases/{id}/relations`` — relate this case to another
  (bidirectional ``related`` pair, P01-US5).
- ``POST /cases/{id}/splits`` — split this case into a new case
  (P01-US7). The new case carries a copy of the documents.
- ``POST /cases/{id}/merges`` — merge another case into this case
  (P01-US8). The other case is marked CANCELLED; both audit chains
  are preserved.
- ``GET /cases/{id}/relations`` — list the case's outgoing edges
  (any kind) so the workspace can render the related-cases panel.

The router is deliberately thin: it maps HTTP shape to the
application use case, commits the transaction, and renders the
canonical response. All authorization, validation, audit, and outbox
writes live in the use cases (FM-002 / P11-US2).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cases import (
    merge_cases as merge_cases_uc,
)
from app.application.cases import (
    relate_cases as relate_cases_uc,
)
from app.application.cases import (
    split_case as split_case_uc,
)
from app.application.cases.relationship_http_dependencies import (
    CaseRelationshipHttpDependencyProvider,
)
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.domain.case_relationship import (
    CaseRelationship,
    RelationshipKind,
)
from app.interfaces.http.dependencies import get_case_relationship_dependencies

router = APIRouter(prefix="/cases/{case_id}/relations", tags=["case-relations"])


# ---------------------------------------------------------------------------
# Pydantic request / response shapes
# ---------------------------------------------------------------------------


class RelateCasesRequest(BaseModel):
    target_case_id: UUID = Field(description="The case to relate to.")
    reason: str = Field(default="", max_length=2000)
    note: str = Field(default="", max_length=10_000)


class SplitCaseRequest(BaseModel):
    new_title: str = Field(min_length=1, max_length=500)
    new_description: str | None = Field(default=None, max_length=10_000)
    new_data: dict | None = None


class MergeCasesRequest(BaseModel):
    retired_case_id: UUID = Field(description="The case that will be folded in.")
    reason: str = Field(default="", max_length=2000)
    note: str = Field(default="", max_length=10_000)


class CaseRelationEdge(BaseModel):
    id: UUID
    source_case_id: UUID
    target_case_id: UUID
    kind: RelationshipKind
    relationship_id: UUID
    reason: str = ""
    note: str = ""


class RelateCasesResponse(BaseModel):
    relationship_id: UUID
    source_case_id: UUID
    target_case_id: UUID


class SplitCaseResponse(BaseModel):
    new_case_id: UUID
    new_reference: str
    carried_document_ids: list[UUID]


class MergeCasesResponse(BaseModel):
    survivor_case_id: UUID
    retired_case_id: UUID
    relationship_id: UUID


# ---------------------------------------------------------------------------
# POST /cases/{id}/relations — relate two cases
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=RelateCasesResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Relate this case to another case (bidirectional peer link)",
)
async def relate(
    case_id: UUID,
    body: RelateCasesRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: CaseRelationshipHttpDependencyProvider = Depends(
        get_case_relationship_dependencies
    ),
    correlation_id: str = Header(default="", alias="X-Correlation-Id"),
) -> RelateCasesResponse:
    repositories = dependencies.for_session(db)
    result = await relate_cases_uc.handle(
        relate_cases_uc.RelateCasesCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            source_case_id=case_id,
            target_case_id=body.target_case_id,
            reason=body.reason,
            note=body.note,
            correlation_id=correlation_id,
        ),
        case_repo=repositories.case,
        relationship_repo=repositories.relationship,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return RelateCasesResponse(
        relationship_id=result.source_edge.relationship_id,
        source_case_id=result.source_edge.source_case_id,
        target_case_id=result.source_edge.target_case_id,
    )


# ---------------------------------------------------------------------------
# GET /cases/{id}/relations — list outgoing edges
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=list[CaseRelationEdge],
    summary="List outgoing relationships from this case",
)
async def list_relations(
    case_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseRelationshipHttpDependencyProvider = Depends(
        get_case_relationship_dependencies
    ),
    kinds: Annotated[list[RelationshipKind] | None, Query()] = None,
) -> list[CaseRelationEdge]:
    repo = dependencies.for_session(db).relationship
    edges = await repo.list_for_case(user.tenant_id, case_id, kinds=kinds)
    return [
        CaseRelationEdge(
            id=e.id,
            source_case_id=e.source_case_id,
            target_case_id=e.target_case_id,
            kind=e.kind,
            relationship_id=e.relationship_id,
            reason=e.reason,
            note=e.note,
        )
        for e in edges
    ]


# ---------------------------------------------------------------------------
# Splits + merges — separate sub-routers so each has its own POST path
# ---------------------------------------------------------------------------


splits_router = APIRouter(prefix="/cases/{case_id}/splits", tags=["case-relations"])


@splits_router.post(
    "",
    response_model=SplitCaseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Split this case into a new case (carrying its documents)",
)
async def split(
    case_id: UUID,
    body: SplitCaseRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: CaseRelationshipHttpDependencyProvider = Depends(
        get_case_relationship_dependencies
    ),
    correlation_id: str = Header(default="", alias="X-Correlation-Id"),
) -> SplitCaseResponse:
    repositories = dependencies.for_session(db)
    result = await split_case_uc.handle(
        split_case_uc.SplitCaseCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            source_case_id=case_id,
            new_title=body.new_title,
            new_description=body.new_description,
            new_data=body.new_data,
            correlation_id=correlation_id,
        ),
        case_repo=repositories.case,
        case_type_repo=repositories.case_type,
        case_type_version_repo=repositories.case_type_version,
        document_repo=repositories.document,
        relationship_repo=repositories.relationship,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return SplitCaseResponse(
        new_case_id=result.new_case.id,
        new_reference=result.new_case.reference.value,
        carried_document_ids=list(result.carried_document_ids),
    )


merges_router = APIRouter(prefix="/cases/{case_id}/merges", tags=["case-relations"])


@merges_router.post(
    "",
    response_model=MergeCasesResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Merge another case into this case (P01-US8)",
)
async def merge(
    case_id: UUID,
    body: MergeCasesRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: CaseRelationshipHttpDependencyProvider = Depends(
        get_case_relationship_dependencies
    ),
    correlation_id: str = Header(default="", alias="X-Correlation-Id"),
) -> MergeCasesResponse:
    repositories = dependencies.for_session(db)
    result = await merge_cases_uc.handle(
        merge_cases_uc.MergeCasesCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            survivor_case_id=case_id,
            retired_case_id=body.retired_case_id,
            reason=body.reason,
            note=body.note,
            correlation_id=correlation_id,
        ),
        case_repo=repositories.case,
        relationship_repo=repositories.relationship,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return MergeCasesResponse(
        survivor_case_id=result.survivor_case_id,
        retired_case_id=result.retired_case_id,
        relationship_id=result.merge_edge.relationship_id,
    )


# Wire the split + merge routers into the main file under ``/api/v1``.
# The module-level ``router`` above is intentionally limited to relations;
# the splits + merges are sibling routers that share the same auth and
# DB dependency style.
__all__ = [
    "CaseRelationEdge",
    "MergeCasesRequest",
    "MergeCasesResponse",
    "RelateCasesRequest",
    "RelateCasesResponse",
    "SplitCaseRequest",
    "SplitCaseResponse",
    "CaseRelationship",
    "merges_router",
    "router",
    "splits_router",
]
