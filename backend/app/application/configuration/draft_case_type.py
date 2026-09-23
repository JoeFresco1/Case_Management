"""Draft a new ``CaseTypeVersion`` (FM-008).

Drafting creates a new version row in ``draft`` status. The version
number is monotonic per CaseType (1, 2, 3, …); the repository reads the
latest existing version and increments. The same CaseType key must
exist; the draft is created against the existing header.

# Validation

The schema is validated at draft time too — not only at publish. This
catches typos and missing fields early, and makes the publish path a
short confirmation that the schema is still valid. A schema that
failed validation at draft time is not persisted.

# Why a separate draft endpoint

Pillar P10 §3 calls out the "draft → validate → publish → retire"
lifecycle; the draft step has its own authorization and audit semantics
(``ConfigurationDrafted``). Splitting it from publish keeps the
``POST /admin/case-types`` and ``POST /admin/case-types/{id}/versions``
endpoints composable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.application.configuration.validate_case_type_definition import (
    validate_definition,
)
from app.domain.case_type import (
    CaseType,
    CaseTypeSchema,
    CaseTypeVersion,
)
from app.domain.errors import NotFound


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _CaseTypeRepo(Protocol):
    async def get_by_key(self, tenant_id: UUID, key: str) -> CaseType | None: ...


class _CaseTypeVersionRepo(Protocol):
    async def get_latest_for_case_type(
        self, tenant_id: UUID, case_type_id: UUID
    ) -> CaseTypeVersion | None: ...

    async def add(self, version: CaseTypeVersion) -> None: ...


class _AuditStore(Protocol):
    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID,
        actor_type: str,
        subject_type: str,
        subject_id: UUID,
        case_id: UUID | None,
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class DraftCaseTypeVersionCommand:
    tenant_id: UUID
    actor_id: UUID
    case_type_key: str
    schema: CaseTypeSchema
    correlation_id: str


@dataclass(frozen=True, slots=True)
class DraftCaseTypeVersionResult:
    case_type_id: UUID
    case_type_version_id: UUID
    version_number: int


async def handle(
    cmd: DraftCaseTypeVersionCommand,
    *,
    case_type_repo: _CaseTypeRepo,
    case_type_version_repo: _CaseTypeVersionRepo,
    audit_store: _AuditStore,
    clock: _Clock,
) -> DraftCaseTypeVersionResult:
    """Create a new draft ``CaseTypeVersion``.

    Raises:
        NotFound: the CaseType key does not exist for this tenant.
        ValidationFailed: the schema is malformed.
    """
    case_type = await case_type_repo.get_by_key(cmd.tenant_id, cmd.case_type_key)
    if case_type is None:
        raise NotFound(f"case type {cmd.case_type_key!r} not found")

    validate_definition(cmd.schema)

    latest = await case_type_version_repo.get_latest_for_case_type(cmd.tenant_id, case_type.id)
    next_version = (latest.version_number + 1) if latest is not None else 1

    now = clock.now()
    draft = CaseTypeVersion.draft(
        tenant_id=cmd.tenant_id,
        case_type_id=case_type.id,
        version_number=next_version,
        schema=cmd.schema,
        now=now,
    )
    await case_type_version_repo.add(draft)

    await audit_store.append(
        tenant_id=cmd.tenant_id,
        event_type="ConfigurationDrafted",
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type="CaseTypeVersion",
        subject_id=draft.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "case_type_id": str(case_type.id),
            "case_type_key": case_type.key,
            "version_number": draft.version_number,
            "field_keys": list(draft.schema.field_keys()),
        },
        occurred_at=now,
    )

    return DraftCaseTypeVersionResult(
        case_type_id=case_type.id,
        case_type_version_id=draft.id,
        version_number=draft.version_number,
    )


__all__ = [
    "DraftCaseTypeVersionCommand",
    "DraftCaseTypeVersionResult",
    "handle",
]
