"""List publication history for a CaseType (FR-033 / P10-US4).

The history endpoint returns every version of a CaseType, newest first,
so an admin can answer:

- "What is the currently effective schema?" (``status == published``)
- "Who published what, when?"
- "Which version is this case bound to?" (joined from the case row)

The result includes the schema's publication lineage (status,
published_at, published_by) and a high-level fingerprint
(``field_keys``, ``document_classifications``) so the admin UI can
render the timeline without dragging the full schema payload over the
wire. The full schema is fetchable by id from a sibling endpoint.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.case_type import CaseType, CaseTypeVersion, CaseTypeVersionStatus
from app.domain.errors import NotFound


class _CaseTypeRepo(Protocol):
    async def get(self, tenant_id: UUID, case_type_id: UUID) -> CaseType | None: ...


class _CaseTypeVersionRepo(Protocol):
    async def list_for_case_type(
        self,
        tenant_id: UUID,
        case_type_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[CaseTypeVersion]: ...


@dataclass(frozen=True, slots=True)
class PublicationHistoryQuery:
    tenant_id: UUID
    case_type_id: UUID
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True, slots=True)
class PublicationHistoryEntry:
    id: UUID
    version_number: int
    status: CaseTypeVersionStatus
    published_at: datetime | None
    published_by: UUID | None
    created_at: datetime
    field_keys: tuple[str, ...]
    document_classifications: tuple[str, ...]

    @staticmethod
    def from_domain(v: CaseTypeVersion) -> PublicationHistoryEntry:
        return PublicationHistoryEntry(
            id=v.id,
            version_number=v.version_number,
            status=v.status,
            published_at=v.published_at,
            published_by=v.published_by,
            created_at=v.created_at,  # type: ignore[arg-type]
            field_keys=v.schema.field_keys(),
            document_classifications=v.schema.document_classifications,
        )


async def handle(
    query: PublicationHistoryQuery,
    *,
    case_type_repo: _CaseTypeRepo,
    case_type_version_repo: _CaseTypeVersionRepo,
) -> list[PublicationHistoryEntry]:
    """Return the publication history for a CaseType, newest first.

    Raises:
        NotFound: the CaseType does not exist for this tenant.
    """
    case_type = await case_type_repo.get(query.tenant_id, query.case_type_id)
    if case_type is None:
        raise NotFound(f"case type {query.case_type_id} not found")

    versions = await case_type_version_repo.list_for_case_type(
        query.tenant_id,
        query.case_type_id,
        limit=query.limit,
        offset=query.offset,
    )
    return [PublicationHistoryEntry.from_domain(v) for v in versions]


__all__ = [
    "PublicationHistoryQuery",
    "PublicationHistoryEntry",
    "handle",
]
