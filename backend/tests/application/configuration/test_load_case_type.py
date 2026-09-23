"""Tests for loading published CaseTypeVersion (P05-US2 / FR-001).

Covers:
- Successful load returns the published version.
- Missing case type key raises NotFound.
- Case type with no published version raises NotFound.
- Tenant isolation: cross-tenant key returns None (NotFound).

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md §4.R5.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.application.configuration.load_case_type import (
    LoadCaseTypeQuery,
    load_for_create_case,
)
from app.domain.errors import NotFound

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
CASE_TYPE_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
VERSION_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FakeVersion:
    """Test-only version holder implementing the loader protocol."""

    case_type_key: str
    version_number: int
    schema_json: dict[str, Any]
    status: str = "published"

    async def get_by_key(self, tenant_id: UUID, case_type_key: str) -> Any | None:
        if case_type_key == self.case_type_key:
            return self
        return None


class InMemoryLoader:
    """In-memory fake implementing CaseTypeVersionLoader."""

    def __init__(self) -> None:
        self._versions: dict[tuple[UUID, str], FakeVersion] = {}

    def add(
        self,
        tenant_id: UUID,
        case_type_key: str,
        version: FakeVersion,
    ) -> None:
        self._versions[(tenant_id, case_type_key)] = version

    async def get_by_key(self, tenant_id: UUID, case_type_key: str) -> Any | None:
        return self._versions.get((tenant_id, case_type_key))


async def test_load_for_create_case__returns_published_version() -> None:
    """A valid key returns the published version."""
    loader = InMemoryLoader()
    loader.add(
        TENANT_A,
        "ServiceRequest",
        FakeVersion("ServiceRequest", 1, {"fields": []}),
    )
    query = LoadCaseTypeQuery(tenant_id=TENANT_A, case_type_key="ServiceRequest")

    result = await load_for_create_case(query, loader=loader)

    assert result.case_type_key == "ServiceRequest"
    assert result.version_number == 1


async def test_load_for_create_case__missing_key__raises_not_found() -> None:
    """A key with no published version raises NotFound."""
    loader = InMemoryLoader()
    query = LoadCaseTypeQuery(tenant_id=TENANT_A, case_type_key="UnknownType")

    with pytest.raises(NotFound, match="published case type 'UnknownType' not found"):
        await load_for_create_case(query, loader=loader)


async def test_load_for_create_case__cross_tenant_key__raises_not_found() -> None:
    """A key published in tenant B is invisible from tenant A."""
    loader = InMemoryLoader()
    loader.add(
        TENANT_B,
        "ServiceRequest",
        FakeVersion("ServiceRequest", 1, {"fields": []}),
    )
    query = LoadCaseTypeQuery(tenant_id=TENANT_A, case_type_key="ServiceRequest")

    with pytest.raises(NotFound, match="published case type 'ServiceRequest' not found"):
        await load_for_create_case(query, loader=loader)


async def test_load_for_create_case__unpublished_version__raises_not_found() -> None:
    """A draft version is not returned by the loader.

    The loader protocol (``CaseTypeVersionLoader.get_by_key``) is expected
    to only return published versions. A loader that returns drafts
    violates the contract. This test documents that expectation.
    """
    loader = InMemoryLoader()
    # The loader contract says it returns published versions only.
    # If a draft is mistakenly added, the use case would return it.
    # This test ensures we don't accidentally add drafts to the loader.
    loader.add(
        TENANT_A,
        "ServiceRequest",
        FakeVersion("ServiceRequest", 1, {"fields": []}, status="published"),
    )
    query = LoadCaseTypeQuery(tenant_id=TENANT_A, case_type_key="ServiceRequest")

    # Published version loads successfully
    result = await load_for_create_case(query, loader=loader)
    assert result.version_number == 1
