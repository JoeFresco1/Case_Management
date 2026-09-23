"""List published case types an actor may use for intake (P01-US1 / T051)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.case_type import CaseType, CaseTypeSchema, CaseTypeVersion
from app.domain.context import UserContext
from app.domain.errors import Forbidden


class _CaseTypeLister(Protocol):
    async def list_for_tenant(self, tenant_id: UUID) -> list[CaseType]: ...


class _PublishedVersionReader(Protocol):
    async def get_published_for_case_type(
        self, tenant_id: UUID, case_type_id: UUID
    ) -> CaseTypeVersion | None: ...


@dataclass(frozen=True, slots=True)
class PublishedCaseType:
    """A published, intake-authorized schema projection."""

    id: UUID
    key: str
    name: str
    description: str
    sensitivity_default: str
    version_id: UUID
    version_number: int
    schema: CaseTypeSchema


async def handle(
    *,
    tenant_id: UUID,
    actor: UserContext,
    case_type_repo: _CaseTypeLister,
    version_repo: _PublishedVersionReader,
) -> Sequence[PublishedCaseType]:
    """Return only published case types the actor can create.

    Authorization is evaluated independently for each type so a restricted
    type is omitted without leaking its existence or schema details.
    """
    results: list[PublishedCaseType] = []
    for case_type in await case_type_repo.list_for_tenant(tenant_id):
        version = await version_repo.get_published_for_case_type(tenant_id, case_type.id)
        if version is None:
            continue
        try:
            await authorize(
                Action.CASE_CREATE,
                actor,
                Resource(tenant_id=tenant_id, kind="case_type", id=case_type.id),
            )
        except Forbidden:
            continue
        results.append(
            PublishedCaseType(
                id=case_type.id,
                key=case_type.key,
                name=case_type.name,
                description=case_type.description,
                sensitivity_default=case_type.sensitivity_default,
                version_id=version.id,
                version_number=version.version_number,
                schema=version.schema,
            )
        )
    return results


__all__ = ["PublishedCaseType", "handle"]
