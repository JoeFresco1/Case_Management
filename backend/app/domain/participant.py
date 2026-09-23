"""Participants attached to a case.

Participants are retained after removal: ``removed_at`` is a tombstone so
audit, decisions, communications, and documents can continue to reference
the same stable identity.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation


@dataclass(frozen=True, slots=True)
class Participant:
    """A person or party associated with a case.

    ``removed_at`` is deliberately nullable rather than deleting the row;
    historical references must remain resolvable.
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    role: str
    display_name: str
    source: str
    is_primary: bool
    added_at: datetime
    removed_at: datetime | None = None
    external_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.role.strip() or not self.display_name.strip() or not self.source.strip():
            raise InvariantViolation("participant role, display_name, and source are required")
        if self.removed_at is not None and self.removed_at < self.added_at:
            raise InvariantViolation("participant removed_at cannot precede added_at")

    @classmethod
    def create(
        cls,
        *,
        tenant_id: UUID,
        case_id: UUID,
        role: str,
        display_name: str,
        source: str,
        is_primary: bool,
        added_at: datetime,
        external_ref: str | None = None,
    ) -> Participant:
        """Create a participant with a stable identity."""
        return cls(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=case_id,
            role=role,
            display_name=display_name,
            source=source,
            is_primary=is_primary,
            added_at=added_at,
            external_ref=external_ref,
        )

    def update(
        self,
        *,
        role: str | None = None,
        display_name: str | None = None,
        source: str | None = None,
        is_primary: bool | None = None,
        external_ref: str | None = None,
    ) -> Participant:
        """Return a validated participant with changed mutable metadata."""
        return replace(
            self,
            role=self.role if role is None else role,
            display_name=self.display_name if display_name is None else display_name,
            source=self.source if source is None else source,
            is_primary=self.is_primary if is_primary is None else is_primary,
            external_ref=self.external_ref if external_ref is None else external_ref,
        )

    def remove(self, *, removed_at: datetime) -> Participant:
        """Return a tombstoned participant while preserving its identity."""
        return replace(self, removed_at=removed_at)


__all__ = ["Participant"]
