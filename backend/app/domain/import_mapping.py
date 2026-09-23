"""Versioned import-mapping registry (P14-US3 / FM-026).

A mapping translates source rows to ``case.data``. Request-supplied
inline mappings stay supported for back-compat; the registry adds the
"durable, versioned, publish-governed" contract FM-026 requires:

- ``(tenant_id, key, version_number)`` is the identity; a job pins the
  exact version it ran with so re-running an old version reproduces.
- lifecycle ``draft`` → ``published`` → ``retired`` mirrors
  ``CaseTypeVersion``. Only ``published`` mappings may back new jobs;
  ``retired`` blocks new jobs but old jobs keep their snapshot.
- the ``mapping_json`` blob is the canonical ``MappingVersion``
  document (validated via ``MappingVersion`` shape checks at the
  application layer before persist).

# INTEGRATION_SEAM: enterprise imports

The registry is local SQL today; a future connector adapter reads the
same rows. Azure migration does not change the port.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation


class ImportMappingStatus(StrEnum):
    """Lifecycle of a registered import mapping."""

    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class ImportMapping:
    """A versioned, governed import mapping.

    Attributes:
        id: Stable row id.
        tenant_id: Owning tenant (every query carries it).
        key: Operator-facing stable key (``"hr_csv_v1"``).
        version_number: Monotonic per key, starts at 1.
        mapping_json: Canonical mapping document (``MappingVersion`` shape).
        status: Lifecycle status.
        published_at/published_by: Set once on publish.
        created_at: Row creation time.
    """

    id: UUID
    tenant_id: UUID
    key: str
    version_number: int
    mapping_json: dict[str, Any]
    status: ImportMappingStatus = ImportMappingStatus.DRAFT
    published_at: datetime | None = None
    published_by: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.key or not self.key.strip():
            raise InvariantViolation("import mapping requires a non-empty key")
        if self.version_number < 1:
            raise InvariantViolation("import mapping version_number must be >= 1")
        if not isinstance(self.mapping_json, dict) or not self.mapping_json:
            raise InvariantViolation("import mapping requires a non-empty mapping_json")

    @staticmethod
    def draft(
        *,
        tenant_id: UUID,
        key: str,
        version_number: int,
        mapping_json: dict[str, Any],
        now: datetime,
    ) -> ImportMapping:
        """Create a new draft mapping version."""
        return ImportMapping(
            id=uuid4(),
            tenant_id=tenant_id,
            key=key.strip(),
            version_number=version_number,
            mapping_json=dict(mapping_json),
            status=ImportMappingStatus.DRAFT,
            created_at=now,
        )

    def publish(self, *, actor_id: UUID, now: datetime) -> ImportMapping:
        """Publish a draft mapping. Published rows are immutable."""
        if self.status is not ImportMappingStatus.DRAFT:
            raise InvariantViolation(f"cannot publish a {self.status.value} import mapping")
        return replace(
            self,
            status=ImportMappingStatus.PUBLISHED,
            published_at=now,
            published_by=actor_id,
            updated_at=now,
        )

    def retire(self, *, actor_id: UUID, now: datetime) -> ImportMapping:
        """Retire a published mapping; retired mappings block new jobs."""
        if self.status is not ImportMappingStatus.PUBLISHED:
            raise InvariantViolation(f"cannot retire a {self.status.value} import mapping")
        return replace(
            self,
            status=ImportMappingStatus.RETIRED,
            updated_at=now,
        )


__all__ = ["ImportMapping", "ImportMappingStatus"]
