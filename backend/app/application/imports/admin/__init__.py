"""Import mapping admin use cases (FM-026 / P14-US3).

Three operations govern the mapping lifecycle:

- :func:`draft_import_mapping` — create a new draft version under a
  stable key. The version number is monotonic per key; the repository
  reads the latest existing version and increments.
- :func:`publish_import_mapping` — transition a draft to ``published``.
  Only published mappings may back new import jobs. Re-publishing a
  published mapping raises :class:`InvariantViolation` (immutability).
- :func:`retire_import_mapping` — transition a published mapping to
  ``retired``. Retired mappings block new jobs but old jobs keep their
  inline snapshot.

Each operation appends a ``ConfigurationDrafted`` /
``ConfigurationPublished`` / ``ConfigurationRetired`` audit event in
the same transaction as the status flip (Constitution IV / AGENTS.md
§4.R12).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.application.imports.admin.http_dependencies import (
    ImportMappingRepository,
)
from app.domain.context import UserContext
from app.domain.errors import NotFound
from app.domain.import_mapping import ImportMapping
from app.domain.ports import AuditEventStore


class _Clock(Protocol):
    def now(self) -> datetime: ...


# ---------------------------------------------------------------------------
# Commands + results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DraftImportMappingCommand:
    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    key: str
    mapping_json: dict[str, Any]
    correlation_id: str


@dataclass(frozen=True, slots=True)
class DraftImportMappingResult:
    mapping_id: UUID
    version_number: int
    status: str


@dataclass(frozen=True, slots=True)
class PublishImportMappingCommand:
    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    mapping_id: UUID
    correlation_id: str


@dataclass(frozen=True, slots=True)
class PublishImportMappingResult:
    mapping_id: UUID
    version_number: int
    status: str


@dataclass(frozen=True, slots=True)
class RetireImportMappingCommand:
    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    mapping_id: UUID
    correlation_id: str


@dataclass(frozen=True, slots=True)
class RetireImportMappingResult:
    mapping_id: UUID
    version_number: int
    status: str


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


async def draft_import_mapping(
    cmd: DraftImportMappingCommand,
    *,
    repo: ImportMappingRepository,
    audit_store: AuditEventStore,
    clock: _Clock,
) -> DraftImportMappingResult:
    """Create a new draft import-mapping version under ``key``.

    The version number is one greater than the latest existing version
    for the same key (or 1 if none exists). Only draft versions can be
    published; publishing a draft copies the registry snapshot into the
    job so a later retire does not silently change historical jobs.
    """
    actor_ctx = UserContext(
        user_id=cmd.actor_id,
        tenant_id=cmd.tenant_id,
        display_name="",
        roles=cmd.actor_roles,
        is_system=False,
    )
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        actor_ctx,
        Resource(tenant_id=cmd.tenant_id, kind="import_mapping"),
    )

    # Why fetch-then-add: the version number is monotonic per key; we
    # cannot use a database sequence because the identity is
    # (tenant, key, version_number).
    existing = await repo.list_for_key(cmd.tenant_id, cmd.key)
    next_version = max((m.version_number for m in existing), default=0) + 1

    now = clock.now()
    draft = ImportMapping.draft(
        tenant_id=cmd.tenant_id,
        key=cmd.key,
        version_number=next_version,
        mapping_json=dict(cmd.mapping_json),
        now=now,
    )
    await repo.add(draft)
    await audit_store.append(
        tenant_id=cmd.tenant_id,
        event_type="ConfigurationDrafted",
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type="ImportMapping",
        subject_id=draft.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "key": draft.key,
            "version_number": draft.version_number,
        },
        occurred_at=now,
    )
    return DraftImportMappingResult(
        mapping_id=draft.id,
        version_number=draft.version_number,
        status=draft.status.value,
    )


async def publish_import_mapping(
    cmd: PublishImportMappingCommand,
    *,
    repo: ImportMappingRepository,
    audit_store: AuditEventStore,
    clock: _Clock,
) -> PublishImportMappingResult:
    """Publish a draft import mapping. Published mappings are immutable."""
    actor_ctx = UserContext(
        user_id=cmd.actor_id,
        tenant_id=cmd.tenant_id,
        display_name="",
        roles=cmd.actor_roles,
        is_system=False,
    )
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        actor_ctx,
        Resource(tenant_id=cmd.tenant_id, kind="import_mapping"),
    )

    mapping = await repo.get(cmd.tenant_id, cmd.mapping_id)
    if mapping is None:
        raise NotFound(f"import mapping {cmd.mapping_id} not found")

    now = clock.now()
    published = mapping.publish(actor_id=cmd.actor_id, now=now)
    await repo.mark_published(
        cmd.tenant_id,
        published.id,
        published_at=now,
        published_by=cmd.actor_id,
    )
    await audit_store.append(
        tenant_id=cmd.tenant_id,
        event_type="ConfigurationPublished",
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type="ImportMapping",
        subject_id=published.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "key": published.key,
            "version_number": published.version_number,
        },
        occurred_at=now,
    )
    return PublishImportMappingResult(
        mapping_id=published.id,
        version_number=published.version_number,
        status=published.status.value,
    )


async def retire_import_mapping(
    cmd: RetireImportMappingCommand,
    *,
    repo: ImportMappingRepository,
    audit_store: AuditEventStore,
    clock: _Clock,
) -> RetireImportMappingResult:
    """Retire a published import mapping. Retired mappings block new jobs."""
    actor_ctx = UserContext(
        user_id=cmd.actor_id,
        tenant_id=cmd.tenant_id,
        display_name="",
        roles=cmd.actor_roles,
        is_system=False,
    )
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        actor_ctx,
        Resource(tenant_id=cmd.tenant_id, kind="import_mapping"),
    )

    mapping = await repo.get(cmd.tenant_id, cmd.mapping_id)
    if mapping is None:
        raise NotFound(f"import mapping {cmd.mapping_id} not found")

    now = clock.now()
    retired = mapping.retire(actor_id=cmd.actor_id, now=now)
    await repo.mark_retired(
        cmd.tenant_id,
        retired.id,
        retired_at=now,
        retired_by=cmd.actor_id,
    )
    await audit_store.append(
        tenant_id=cmd.tenant_id,
        event_type="ConfigurationRetired",
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type="ImportMapping",
        subject_id=retired.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "key": retired.key,
            "version_number": retired.version_number,
        },
        occurred_at=now,
    )
    return RetireImportMappingResult(
        mapping_id=retired.id,
        version_number=retired.version_number,
        status=retired.status.value,
    )


__all__ = [
    "DraftImportMappingCommand",
    "DraftImportMappingResult",
    "ImportMappingRepository",
    "PublishImportMappingCommand",
    "PublishImportMappingResult",
    "RetireImportMappingCommand",
    "RetireImportMappingResult",
    "draft_import_mapping",
    "publish_import_mapping",
    "retire_import_mapping",
]
