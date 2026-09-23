"""Publish use case for a CaseTypeVersion (FM-008 / FR-015).

This module orchestrates the full ``draft → published`` transition:

1. Run the validator over the requested schema. A defect raises
   ``ValidationFailed`` and the transaction aborts.
2. Look up the draft version by id (tenant-scoped). If the row's status
   is not ``draft`` the use case raises ``InvariantViolation`` (this is
   the immutability guard from FR-015).
3. Re-issue the domain entity via ``CaseTypeVersion.publish`` so the
   ``published_at`` / ``published_by`` fields are stamped by the Clock.
4. Persist the new status, set the CaseType's ``current_version_id`` to
   the published version, and emit a ``ConfigurationPublished`` audit
   event.

The whole flow runs inside one DB transaction so the status flip, the
current-version pointer, and the audit row are atomic (Constitution
III / plan §4.R12). A failure anywhere rolls back every side effect.

# Why not in the domain

The publish lifecycle crosses multiple aggregates (CaseType +
CaseTypeVersion + AuditEvent) and writes through ports the domain does
not own (audit, time). The orchestration belongs in the application
layer; the domain owns the invariants on ``publish()`` itself.
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
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.errors import NotFound


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _CaseTypeRepo(Protocol):
    async def get(self, tenant_id: UUID, case_type_id: UUID) -> CaseType | None: ...
    async def set_current_version(
        self, tenant_id: UUID, case_type_id: UUID, version_id: UUID
    ) -> None: ...


class _CaseTypeVersionRepo(Protocol):
    async def get(self, tenant_id: UUID, version_id: UUID) -> CaseTypeVersion | None: ...

    async def mark_status(
        self,
        tenant_id: UUID,
        version_id: UUID,
        *,
        new_status: CaseTypeVersionStatus,
        published_at: datetime | None,
        published_by: UUID | None,
    ) -> None: ...

    async def list_for_case_type(
        self,
        tenant_id: UUID,
        case_type_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CaseTypeVersion]: ...


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
class PublishCaseTypeVersionCommand:
    tenant_id: UUID
    actor_id: UUID
    case_type_version_id: UUID
    correlation_id: str


@dataclass(frozen=True, slots=True)
class PublishCaseTypeVersionResult:
    case_type_id: UUID
    case_type_version_id: UUID
    version_number: int
    status: CaseTypeVersionStatus


async def handle(
    cmd: PublishCaseTypeVersionCommand,
    *,
    case_type_repo: _CaseTypeRepo,
    case_type_version_repo: _CaseTypeVersionRepo,
    audit_store: _AuditStore,
    clock: _Clock,
) -> PublishCaseTypeVersionResult:
    """Publish a draft ``CaseTypeVersion``.

    Raises:
        NotFound: the version (or its owning CaseType) does not exist.
        InvariantViolation: the version is not in ``draft`` status.
        ValidationFailed: the schema has unresolved defects.
    """
    version = await case_type_version_repo.get(cmd.tenant_id, cmd.case_type_version_id)
    if version is None:
        raise NotFound(f"case type version {cmd.case_type_version_id} not found")

    # Validator runs first; if the schema is broken we never touch the
    # persistence layer (the validator is a pure function over the
    # domain entity's schema).
    validate_definition(version.schema)

    case_type = await case_type_repo.get(cmd.tenant_id, version.case_type_id)
    if case_type is None:
        raise NotFound(f"case type {version.case_type_id} not found")

    now = clock.now()
    published = version.publish(actor_id=cmd.actor_id, at=now)

    # Persist the status flip. ``mark_status`` raises
    # ``InvariantViolation`` if the row is not actually a draft in the
    # DB (the second-line guard).
    await case_type_version_repo.mark_status(
        cmd.tenant_id,
        published.id,
        new_status=CaseTypeVersionStatus.PUBLISHED,
        published_at=published.published_at,
        published_by=published.published_by,
    )
    # Promote to the CaseType's current version.
    await case_type_repo.set_current_version(cmd.tenant_id, case_type.id, published.id)

    # Audit. Same transaction so the audit row is committed atomically
    # with the status flip (Constitution IV / plan §4.R12).
    await audit_store.append(
        tenant_id=cmd.tenant_id,
        event_type="ConfigurationPublished",
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type="CaseTypeVersion",
        subject_id=published.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "case_type_id": str(case_type.id),
            "case_type_key": case_type.key,
            "version_number": published.version_number,
            "schema_field_keys": list(published.schema.field_keys()),
            "document_classifications": list(published.schema.document_classifications),
        },
        occurred_at=now,
    )

    return PublishCaseTypeVersionResult(
        case_type_id=case_type.id,
        case_type_version_id=published.id,
        version_number=published.version_number,
        status=CaseTypeVersionStatus.PUBLISHED,
    )


__all__ = [
    "PublishCaseTypeVersionCommand",
    "PublishCaseTypeVersionResult",
    "handle",
]
