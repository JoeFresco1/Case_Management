"""Tests for the authorized published case-type intake catalog."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.application.cases import list_published_case_types
from app.domain.case_type import CaseType, CaseTypeSchema, CaseTypeVersion
from app.domain.context import UserContext

TENANT = UUID("11111111-1111-1111-1111-111111111111")
ACTOR = UUID("22222222-2222-2222-2222-222222222222")
NOW = datetime(2026, 9, 8, tzinfo=UTC)


class CaseTypeRepo:
    """In-memory tenant-scoped case-type header repository."""

    def __init__(self, case_types: list[CaseType]) -> None:
        self.case_types = case_types

    async def list_for_tenant(self, tenant_id: UUID) -> list[CaseType]:
        return [item for item in self.case_types if item.tenant_id == tenant_id]


class VersionRepo:
    """In-memory published-version repository."""

    def __init__(self, versions: list[CaseTypeVersion]) -> None:
        self.versions = versions

    async def get_published_for_case_type(
        self, tenant_id: UUID, case_type_id: UUID
    ) -> CaseTypeVersion | None:
        return next(
            (
                item
                for item in self.versions
                if item.tenant_id == tenant_id
                and item.case_type_id == case_type_id
                and item.status.value == "published"
            ),
            None,
        )


def make_case_type(key: str, *, tenant_id: UUID = TENANT) -> tuple[CaseType, CaseTypeVersion]:
    case_type = CaseType(
        id=UUID(int=len(key)),
        tenant_id=tenant_id,
        key=key,
        name=key,
        description=f"{key} requests",
    )
    draft = CaseTypeVersion.draft(
        tenant_id=tenant_id,
        case_type_id=case_type.id,
        version_number=1,
        schema=CaseTypeSchema(),
        now=NOW,
    )
    return case_type, draft.publish(actor_id=ACTOR, at=NOW)


@pytest.mark.asyncio
async def test_list_published_case_types__intake_role__returns_published_schema() -> None:
    case_type, version = make_case_type("ServiceRequest")
    result = await list_published_case_types.handle(
        tenant_id=TENANT,
        actor=UserContext(ACTOR, TENANT, "Intake", ("intake",)),
        case_type_repo=CaseTypeRepo([case_type]),
        version_repo=VersionRepo([version]),
    )
    assert [item.key for item in result] == ["ServiceRequest"]
    assert result[0].version_id == version.id


@pytest.mark.asyncio
async def test_list_published_case_types__reviewer_role__omits_unauthorized_type() -> None:
    case_type, version = make_case_type("ServiceRequest")
    result = await list_published_case_types.handle(
        tenant_id=TENANT,
        actor=UserContext(ACTOR, TENANT, "Reviewer", ("reviewer",)),
        case_type_repo=CaseTypeRepo([case_type]),
        version_repo=VersionRepo([version]),
    )
    assert result == []
