"""FormVersion and FormBinding lifecycle aggregates (P07, FR-015 G3/G4).

:class:`FormVersion` is the immutable published snapshot of a
:class:`~.specs.FormDefinition` (draft -> published -> retired; publish/retire
return new instances). :class:`FormBinding` pins a form version to a case type
version so cases keep the layout they were created under. Both validate
through the shared definition/condition machinery in :mod:`validation` and
:mod:`conditions`.

Symbols: ``FormVersionStatus``, ``FormVersion``, ``FormBinding``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation

from .conditions import _evaluate_visibility, _section_visibility_referenced_keys
from .specs import FormDefinition
from .validation import _validate_definition


class FormVersionStatus(StrEnum):
    """Lifecycle of a ``FormVersion`` (draft/published/retired, persisted as string)."""

    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class FormVersion:
    """Immutable form snapshot; draft->published->retired. Publish/retire return new instances."""

    id: UUID
    tenant_id: UUID
    key: str
    version_number: int
    definition: FormDefinition
    status: FormVersionStatus
    published_at: datetime | None = None
    published_by: UUID | None = None
    created_at: datetime | None = None

    @staticmethod
    def draft(
        *,
        tenant_id: UUID,
        key: str,
        version_number: int,
        definition: FormDefinition,
        now: datetime | None = None,
    ) -> FormVersion:
        """Build a draft; validates definition. Raises InvariantViolation on bad version/shape."""
        if version_number < 1:
            raise InvariantViolation("version_number must be >= 1")
        _validate_definition(definition)
        return FormVersion(
            id=uuid4(),
            tenant_id=tenant_id,
            key=key,
            version_number=version_number,
            definition=definition,
            status=FormVersionStatus.DRAFT,
            created_at=now,
        )

    def publish(self, *, actor_id: UUID, at: datetime) -> FormVersion:
        """Draft->published; raises InvariantViolation unless draft."""
        if self.status is not FormVersionStatus.DRAFT:
            raise InvariantViolation(f"cannot publish a {self.status.value} form version")
        return FormVersion(
            id=self.id,
            tenant_id=self.tenant_id,
            key=self.key,
            version_number=self.version_number,
            definition=self.definition,
            status=FormVersionStatus.PUBLISHED,
            published_at=at,
            published_by=actor_id,
            created_at=self.created_at,
        )

    def retire(self) -> FormVersion:
        """Published->retired; raises InvariantViolation unless published."""
        if self.status is not FormVersionStatus.PUBLISHED:
            raise InvariantViolation(
                f"only published form versions can be retired (got {self.status.value})"
            )
        return FormVersion(
            id=self.id,
            tenant_id=self.tenant_id,
            key=self.key,
            version_number=self.version_number,
            definition=self.definition,
            status=FormVersionStatus.RETIRED,
            published_at=self.published_at,
            published_by=self.published_by,
            created_at=self.created_at,
        )

    def assert_mutable(self) -> None:
        """Raise InvariantViolation unless draft (FR-015 repository guard)."""
        if self.status is not FormVersionStatus.DRAFT:
            raise InvariantViolation(f"form version is {self.status.value} and cannot be modified")

    def visible_section_keys(
        self, *, visible_data: Mapping[str, Any], submission_data: Mapping[str, Any] | None = None
    ) -> frozenset[str]:
        """Visible section keys against merged context; hidden values excluded from predicates."""
        merged: dict[str, Any] = dict(visible_data)
        if submission_data is not None:
            for key, value in submission_data.items():
                if key in _section_visibility_referenced_keys(self.definition):
                    merged[key] = value
        visible: set[str] = set()
        for section in self.definition.sections:
            if section.visible_when is None or _evaluate_visibility(section.visible_when, merged):
                visible.add(section.key)
        return frozenset(visible)


@dataclass(frozen=True, slots=True)
class FormBinding:
    """Immutable link FormVersion<->CaseTypeVersion; cases pin via ``case_type_version_id`` (G4)."""

    id: UUID
    tenant_id: UUID
    case_type_version_id: UUID
    form_version_id: UUID
    published_at: datetime | None = None
    published_by: UUID | None = None
    created_at: datetime | None = None
    state_forms: tuple[Mapping[str, Any], ...] = ()
    retired_at: datetime | None = None
    retired_by: UUID | None = None

    @staticmethod
    def draft(
        *,
        tenant_id: UUID,
        case_type_version_id: UUID,
        form_version_id: UUID,
        now: datetime | None = None,
        state_forms: tuple[Mapping[str, Any], ...] = (),
    ) -> FormBinding:
        """Build a draft binding (unpublished until ``publish``)."""
        return FormBinding(
            id=uuid4(),
            tenant_id=tenant_id,
            case_type_version_id=case_type_version_id,
            form_version_id=form_version_id,
            created_at=now,
            state_forms=state_forms,
        )

    def publish(self, *, actor_id: UUID, at: datetime) -> FormBinding:
        """Mark published; raises InvariantViolation if already published."""
        if self.published_at is not None:
            raise InvariantViolation("binding already published")
        return FormBinding(
            id=self.id,
            tenant_id=self.tenant_id,
            case_type_version_id=self.case_type_version_id,
            form_version_id=self.form_version_id,
            published_at=at,
            published_by=actor_id,
            created_at=self.created_at,
            state_forms=self.state_forms,
            retired_at=self.retired_at,
            retired_by=self.retired_by,
        )

    def retire(self, *, actor_id: UUID, at: datetime) -> FormBinding:
        """Retire a published binding; raises InvariantViolation unless published and live."""
        if self.published_at is None:
            raise InvariantViolation("cannot retire an unpublished binding")
        if self.retired_at is not None:
            raise InvariantViolation("binding already retired")
        return FormBinding(
            id=self.id,
            tenant_id=self.tenant_id,
            case_type_version_id=self.case_type_version_id,
            form_version_id=self.form_version_id,
            published_at=self.published_at,
            published_by=self.published_by,
            created_at=self.created_at,
            state_forms=self.state_forms,
            retired_at=at,
            retired_by=actor_id,
        )

    @property
    def is_published(self) -> bool:
        """True when published and not retired (eligible for new submissions)."""
        return self.published_at is not None and self.retired_at is None

    def assert_applies_to_case(self, *, tenant_id: UUID, case_type_version_id: UUID) -> None:
        """Assert binding pins the case's type version; raises InvariantViolation otherwise."""
        if self.tenant_id != tenant_id:
            raise InvariantViolation("form binding tenant mismatch")
        if self.published_at is None:
            raise InvariantViolation("form binding is not published")
        if self.retired_at is not None:
            raise InvariantViolation("form binding is retired")
        if self.case_type_version_id != case_type_version_id:
            raise InvariantViolation("form binding does not match case's pinned version")


__all__ = [
    "StrEnum",
    "UUID",
    "FormBinding",
    "FormVersion",
    "FormVersionStatus",
    "datetime",
    "uuid4",
]
