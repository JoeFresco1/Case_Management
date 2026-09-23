"""Case aggregate.

The Case is the primary domain object (Constitution I). It carries:

- a stable id and a human-readable CaseReference (`C-{TENANT_SHORT}-{YYYY}-{SEQ:06d}`)
- the CaseTypeVersion that was active at creation
- structured data validated against that version's schema
- a workflow state pointer (set when the workflow engine lands in Phase 3)
- a version token for optimistic concurrency

# INTEGRATION_SEAM: reference generation
The repository that implements `CaseRepository.next_reference` issues a
new per-tenant-monotonic sequence. Domain just validates and stores the
result. The tenant's short code is looked up by the repository via a
`TenantRepository` port so the domain stays pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from app.domain.case_type import CaseTypeVersion
from app.domain.errors import (
    Conflict,
    InvariantViolation,
    ValidationFailed,
)


class CaseStatus(str, Enum):  # noqa: UP042 - intentional str+Enum for JSON serialization
    DRAFT = "draft"
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    CLOSED = "closed"
    CANCELLED = "cancelled"


# Terminal statuses cannot transition further.
_TERMINAL_STATUSES: frozenset[CaseStatus] = frozenset({CaseStatus.CLOSED, CaseStatus.CANCELLED})


# Reference validation (FM-009): the human-readable reference takes the
# shape of the tenant's configured template, so the value object cannot
# pin a single grammar. It enforces wire/DB safety instead — non-empty,
# bounded by ``MAX_REFERENCE_LENGTH`` (matches the ``cases.reference``
# column), and free of control characters. Shape strictness lives where
# the shape is known: ``ReferenceFormat.validate_template`` at
# configuration time and the ``next_reference`` renderer at issuance.
MAX_REFERENCE_LENGTH: int = 200


@dataclass(frozen=True, slots=True)
class CaseReference:
    """A human-readable, per-tenant-monotonic reference.

    Under the platform default template the shape is
    ``C-{tenant_short}-{yyyy}-{seq:06d}`` (e.g. ``C-ACME-2026-000042``);
    a tenant with a custom ``ReferenceFormat`` (FM-009) sees that
    template's shape instead (e.g. ``PR-000001``). The repository
    issues the sequence; the domain validates wire safety only.

    Validation is intentionally shape-agnostic: any non-empty string
    without control characters, bounded by ``MAX_REFERENCE_LENGTH``,
    is accepted. A malformed *legacy* string is still rejected
    downstream — it simply resolves to "not found" instead of failing
    construction, which is the correct semantic for a lookup key.
    """

    value: str

    def __post_init__(self) -> None:
        if not self.value or len(self.value) > MAX_REFERENCE_LENGTH:
            raise InvariantViolation("invalid case reference")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.value):
            raise InvariantViolation("case reference must not contain control characters")


# Sensitivity ladder shared with CaseTypeField/CaseType (case_type.py).
# P11-US6 + P05 §2: a value at or above ``RESTRICTED_THRESHOLD`` requires
# the actor to carry a sensitive-read action before the field's content
# is rendered. The flag is carried on the Case so the redactor never has
# to re-resolve the case type at the API boundary.
RESTRICTED_THRESHOLD: frozenset[str] = frozenset({"restricted", "phi"})


@dataclass(frozen=True, slots=True)
class FieldSnapshot:
    """A snapshot of the editable fields of a :class:`Case`.

    Used by :meth:`Case.edit` to record the before-state for the
    audit row. Only the four fields a PATCH may touch are captured;
    status, workflow state, sensitivity, and the immutable identity
    fields (id, reference, tenant_id, case_type_version_id, created_by,
    timestamps) are intentionally omitted because they are never part
    of a PATCH body (P01-US3).
    """

    title: str = ""
    description: str = ""
    priority: str = "normal"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FieldDiff:
    """Before/after record of a PATCH edit (P01-US3 / FR-003).

    The application layer threads the diff into the ``CaseUpdated``
    audit payload so every edit produces a row that names the changed
    fields and carries their before/after values. Only fields that
    actually changed appear in :attr:`changed_fields`; the
    :attr:`before` / :attr:`after` snapshots always carry the full set
    of editable fields so a reader can see the case's state at the
    audit moment without joining other audit rows.
    """

    before: FieldSnapshot = field(default_factory=FieldSnapshot)
    after: FieldSnapshot = field(default_factory=FieldSnapshot)
    changed_fields: frozenset[str] = field(default_factory=frozenset)

    def to_payload(self) -> dict[str, Any]:
        """Render the diff as a JSON-safe payload for the audit row."""
        return {
            "changed_fields": sorted(self.changed_fields),
            "before": {
                "title": self.before.title,
                "description": self.before.description,
                "priority": self.before.priority,
                "data": self.before.data,
            },
            "after": {
                "title": self.after.title,
                "description": self.after.description,
                "priority": self.after.priority,
                "data": self.after.data,
            },
        }


def is_restricted_sensitivity(level: str) -> bool:
    """Return True when ``level`` is at or above the restricted threshold.

    Levels: ``public`` < ``internal`` < ``confidential`` < ``restricted``
    < ``phi``. Anything at or above ``restricted`` requires explicit
    field-level authorization. Exposed as a module helper so the
    application layer, the redactor, and tests share one definition.
    """
    return level in RESTRICTED_THRESHOLD


@dataclass(frozen=True, slots=True)
class Case:
    id: UUID
    tenant_id: UUID
    reference: CaseReference
    case_type_version_id: UUID
    status: CaseStatus
    priority: str  # 'low' | 'normal' | 'high' | 'urgent'
    title: str
    description: str
    data: dict[str, Any]
    current_workflow_state: str | None
    version_token: int
    created_at: datetime
    updated_at: datetime
    created_by: UUID
    closed_at: datetime | None = None
    # P11-US6 / P05-US3: the case's effective sensitivity (inherited from
    # the case type's ``sensitivity_default`` at creation time). The flag
    # is carried on the aggregate so the redactor and the audit layer do
    # not have to re-resolve the type version at every read. Defaults to
    # ``"internal"`` to match the CaseType default.
    sensitivity: str = "internal"
    # P08-US1 / FM-039 H0 seam: a single binary flag that, when True,
    # refuses any attempt to close the case (terminal status flip).
    # H1 will add a per-case hold row with actor / reason / released_at
    # / released_by; for the H0 slice the flag is the only signal the
    # application layer needs to refuse the disposition. The flag does
    # not block edits — workers can keep recording notes / decisions
    # on a held case; they just cannot close it.
    legal_hold: bool = False

    # ----- factories ---------------------------------------------------------

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        reference: CaseReference,
        case_type_version: CaseTypeVersion,
        title: str,
        description: str,
        data: dict[str, Any],
        priority: str = "normal",
        created_by: UUID,
        now: datetime,
        sensitivity: str = "internal",
        legal_hold: bool = False,
    ) -> Case:
        if not title.strip():
            raise ValidationFailed("title is required", field="title")
        if priority not in ("low", "normal", "high", "urgent"):
            raise ValidationFailed("invalid priority", field="priority")
        if case_type_version.tenant_id != tenant_id:
            raise InvariantViolation("case type version tenant mismatch")
        _validate_against_schema(case_type_version, data)
        return Case(
            id=uuid4(),
            tenant_id=tenant_id,
            reference=reference,
            case_type_version_id=case_type_version.id,
            status=CaseStatus.OPEN,
            priority=priority,
            title=title.strip(),
            description=description,
            data=data,
            current_workflow_state=None,
            version_token=1,
            created_at=now,
            updated_at=now,
            created_by=created_by,
            closed_at=None,
            sensitivity=sensitivity,
            legal_hold=legal_hold,
        )

    # ----- transitions -------------------------------------------------------

    def edit(  # noqa: PLR0912 - structured PATCH branches by design
        self,
        *,
        expected_version_token: int,
        title: str | None = None,
        description: str | None = None,
        priority: str | None = None,
        data: dict[str, Any] | None = None,
        case_type_version: CaseTypeVersion | None = None,
        now: datetime,
    ) -> Case:
        """Apply a PATCH-style edit with optimistic concurrency.

        Implements P01-US3 / FR-003. The caller passes the
        ``expected_version_token`` it read from the prior ``GET`` (the
        ``If-Match`` header at the HTTP boundary). The aggregate refuses
        the edit and raises :class:`Conflict` when its current token
        differs — never silently overwriting a peer's write.

        Only the supplied fields are touched; ``None`` means "leave
        alone". Field-level history preservation is achieved by *not*
        mutating the original ``Case``: ``dataclasses.replace`` produces
        a new instance whose ``data`` snapshot can be diffed by the
        caller (the application layer emits the diff to audit).

        Why the optimistic check happens in the domain (not in the
        repository): the token is the aggregate's invariant. The
        repository is the storage adapter; it cannot know what the
        "correct" token is until the aggregate hands it one. Putting
        the check here also makes the in-memory test fake authoritative
        — the same ``assert_concurrency_token`` path runs in production
        and tests.

        Returns the new :class:`Case` instance. The application layer
        diffs the returned case against the prior one to build the
        ``CaseUpdated`` audit payload (P01-US3 "field-level history
        preserved"). A no-op edit returns ``self`` unchanged.
        """
        self.assert_concurrency_token(expected_version_token)
        if self.status in _TERMINAL_STATUSES:
            raise InvariantViolation(f"cannot edit a {self.status.value} case")
        if case_type_version is not None and data is not None:
            if case_type_version.id != self.case_type_version_id:
                raise InvariantViolation(
                    "data updates must validate against the case's existing type version"
                )
            _validate_against_schema(case_type_version, data)

        kwargs: dict[str, Any] = {}
        changed: set[str] = set()
        if title is not None:
            if not title.strip():
                raise ValidationFailed("title is required", field="title")
            stripped = title.strip()
            if stripped != self.title:
                kwargs["title"] = stripped
                changed.add("title")
        if description is not None and description != self.description:
            kwargs["description"] = description
            changed.add("description")
        if priority is not None:
            if priority not in ("low", "normal", "high", "urgent"):
                raise ValidationFailed("invalid priority", field="priority")
            if priority != self.priority:
                kwargs["priority"] = priority
                changed.add("priority")
        if data is not None:
            if case_type_version is None:
                raise InvariantViolation(
                    "data edits require the case's case_type_version to validate"
                )
            if data != self.data:
                kwargs["data"] = data
                changed.add("data")

        if not changed:
            # No-op edit: return self unchanged so the caller does not
            # bump the version token or emit an audit row for a write
            # that did not change anything. The aggregate is frozen
            # (``slots=True``); returning ``self`` is safe.
            return self

        return replace(
            self,
            **kwargs,
            updated_at=now,
            version_token=self.version_token + 1,
        )

    def update_data(
        self,
        *,
        data: dict[str, Any],
        case_type_version: CaseTypeVersion,
        now: datetime,
    ) -> Case:
        """Backward-compatible wrapper around :meth:`edit`.

        Kept so older callers (e.g. workflow transitions that mutate
        only ``data``) keep working. The new ``edit`` method supersedes
        this for PATCH endpoints (P01-US3).
        """
        return self.edit(
            expected_version_token=self.version_token,
            data=data,
            case_type_version=case_type_version,
            now=now,
        )

    def change_status(self, *, new_status: CaseStatus, now: datetime) -> Case:
        _ensure_not_terminal(self.status)
        if new_status is self.status:
            return self
        if new_status in _TERMINAL_STATUSES and self.status in _TERMINAL_STATUSES:
            raise InvariantViolation("case is already terminal")
        # P08-US1 / FM-039 H0 seam: a held case cannot be closed. The
        # refusal is at the aggregate (not the application layer) so
        # every code path that flips the case to a terminal status
        # inherits the rule — the workflow engine, the admin tool, and
        # a future bulk importer all hit the same guard. The error
        # maps to HTTP 409 conflict at the boundary so a client
        # trying to close a held case gets a structured refusal, not
        # a silent no-op. H1 will replace the flag with a full
        # hold-record lifecycle; the guard here stays.
        if self.legal_hold and new_status in _TERMINAL_STATUSES:
            raise Conflict(
                f"case {self.reference.value} is under legal hold; "
                f"cannot transition to {new_status.value}"
            )
        closed_at = self.closed_at
        if new_status in _TERMINAL_STATUSES:
            closed_at = now
        return replace(
            self,
            status=new_status,
            updated_at=now,
            version_token=self.version_token + 1,
            closed_at=closed_at,
        )

    def set_workflow_state(self, *, state: str | None, now: datetime) -> Case:
        """Update the workflow state pointer without changing case status.

        Used by the workflow engine (T070+). Validation that the state
        exists in the active workflow version lives in the application
        layer; the domain just stores the pointer.

        Why the version_token is only bumped on a *change* of state,
        and never on the very first assignment: ``create_case`` calls
        ``set_workflow_state`` to attach the initial workflow state
        right after ``Case.create``. Bumping the token there would
        mean a freshly-created case ships with ``version_token=2`` and
        stale-concurrency checks misbehave from the first request. The
        token represents the count of *mutations applied after
        creation*; an initial-state assignment is not one. A no-op
        (current equals new) also does not bump.
        """
        if state == self.current_workflow_state:
            return self
        # First-ever assignment: ``Case.create`` leaves the state at
        # ``None``; setting it is part of creation, not a mutation.
        is_initial = self.current_workflow_state is None
        next_token = self.version_token if is_initial else self.version_token + 1
        return replace(
            self,
            current_workflow_state=state,
            updated_at=now,
            version_token=next_token,
        )

    def assert_concurrency_token(self, token: int) -> None:
        if token != self.version_token:
            raise Conflict(f"stale concurrency token (expected {self.version_token}, got {token})")


# ----- helpers --------------------------------------------------------------


def _ensure_not_terminal(status: CaseStatus) -> None:
    if status in _TERMINAL_STATUSES:
        raise InvariantViolation(f"case is {status.value} and cannot change")


def _validate_against_schema(version: CaseTypeVersion, data: dict[str, Any]) -> None:
    for key in version.schema.required_field_keys():
        if key not in data or data[key] in (None, ""):
            raise ValidationFailed(f"required field missing: {key}", field=key)
    extra = set(data) - set(version.schema.field_keys())
    if extra:
        raise ValidationFailed(f"unknown field(s) for case type: {sorted(extra)}")


__all__ = [
    "Case",
    "CaseReference",
    "CaseStatus",
    "FieldDiff",
    "FieldSnapshot",
    "MAX_REFERENCE_LENGTH",
    "RESTRICTED_THRESHOLD",
    "is_restricted_sensitivity",
]
