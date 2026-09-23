"""CaseType and CaseTypeVersion aggregates.

A CaseType is the business category of a case. A CaseTypeVersion is an
immutable, published snapshot of its schema. New versions are created by
drafting a new `CaseTypeVersion` against the same CaseType; existing
cases retain the version that was active at their creation (FM-008).

# Lifecycle (FM-008 / P05-US1 / P10-US2)

``CaseTypeVersion.status`` is one of:

- ``draft`` — being authored. Mutable.
- ``published`` — frozen. The application layer rejects any attempt to
  mutate schema, status, or metadata on a published version. The
  repository treats UPDATE attempts on a published version as a bug.
- ``retired`` — historical. No new cases may be created against a retired
  version (FR-015, FR-033).

# Schema shape (AGENTS.md §16.2)

The version's ``schema_json`` is validated at publish time:

- ``fields``: list of field definitions with stable machine ``key``,
  human ``label``, ``type``, optional ``required``, ``enum_values``,
  ``sensitivity``, ``searchable``.
- ``document_classifications``: list of allowed document classifications.
- ``rules``: list of declarative rule definitions (declarative JSON
  conditions, no code). Evaluator lives in ``domain/rules.py``.

The schema is stored in the repository as portable JSON via
``sqlalchemy.JSON`` (no PostgreSQL-specific operators).

INTEGRATION_SEAM: CaseTypeRepository is a domain port
(``app/domain/ports.py``). The local adapter lives in
``app/infrastructure/persistence/repositories/case_type_repository.py``
and is wired through ``app/deps.py``. The Azure production adapter
(``case_type_repository_azure_sql.py``) is a future module behind the
same Protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation


class CaseTypeVersionStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"


# Field types that the v1 schema supports. Pillar P05 §2 enumerates the
# long-term list; v1 ships the ones that are deterministic, portable, and
# validated without code execution.
_FIELD_TYPES: frozenset[str] = frozenset(
    {
        "text",
        "longtext",
        "integer",
        "decimal",
        "boolean",
        "date",
        "datetime",
        "enum",
        "multichoice",
        "reference",
        "json",
    }
)

# Sensitivity ladder (P05 §2 / Pillar P11). v1 only needs the labels; the
# redaction logic lives in P11 future slices.
_SENSITIVITY_LEVELS: frozenset[str] = frozenset(
    {"public", "internal", "confidential", "restricted", "phi"}
)

# Derived-field operators supported by the v1 declarative evaluator
# (P05-US6 / FM-012). The evaluator is a pure function over the case's
# ``data`` dict; arbitrary expressions are not permitted (Constitution XIV).
_DERIVED_OPERATORS: frozenset[str] = frozenset(
    {"add", "sub", "mul", "div", "concat", "coalesce", "if_present"}
)

# Numeric types that participate in arithmetic derivations.
_DERIVED_ARITHMETIC_TYPES: frozenset[str] = frozenset({"integer", "decimal"})


@dataclass(frozen=True, slots=True)
class DerivedFieldSpec:
    """Declarative specification for a derived field (P05-US6).

    A derived field's value is **not** supplied by the caller at write
    time; it is computed deterministically from sibling fields by the
    evaluator in ``application/cases/compute_derived.py``. The shape
    mirrors the rule operator vocabulary so the same JSON portability
    story applies (no code execution, no I/O).

    Operators:

    - ``add`` / ``sub`` / ``mul`` / ``div`` over two ``operands`` that
      resolve to integer or decimal values. Division by zero raises.
    - ``concat`` over a list of ``operands`` (text, longtext, enum, or
      multichoice).
    - ``coalesce`` over a list of ``operands``; returns the first
      non-null / non-empty value.
    - ``if_present`` with ``operand`` and ``fallback``; returns
      ``operand`` when present, else ``fallback``.
    """

    operator: str
    operands: tuple[str, ...] = ()
    fallback: str | None = None

    def __post_init__(self) -> None:
        if self.operator not in _DERIVED_OPERATORS:
            raise InvariantViolation(f"unsupported derived operator {self.operator!r}")
        if (
            self.operator in {"add", "sub", "mul", "div", "concat", "coalesce"}
            and len(self.operands) < 2
        ):
            raise InvariantViolation(
                f"derived operator {self.operator!r} requires at least 2 operands"
            )
        if self.operator == "if_present" and (len(self.operands) != 1 or not self.fallback):
            raise InvariantViolation(
                "derived operator 'if_present' requires exactly 1 operand and a fallback key"
            )


@dataclass(frozen=True, slots=True)
class LabelRename:
    """A single rename entry for a field's label (P05-US8 / FM-012).

    The platform records every rename so historical meaning is preserved
    even when the visible label has changed across versions. The
    ``previous`` label is empty for the very first version of a field.
    """

    previous: str
    current: str
    at_version: int
    at: datetime | None = None

    def __post_init__(self) -> None:
        if self.at_version < 1:
            raise InvariantViolation(f"rename version must be >= 1 (got {self.at_version})")
        if not self.current.strip():
            raise InvariantViolation("rename current label is required")
        if self.previous == self.current:
            raise InvariantViolation(f"rename is a no-op (previous==current={self.current!r})")


@dataclass(frozen=True, slots=True)
class CaseTypeField:
    """A single field in a case type schema.

    A field has a stable machine ``key``, a human ``label`` that may be
    renamed across versions (P05-US8), a ``type``, optional
    ``enum_values`` for enum/multichoice, and metadata used by P05/P11
    (``sensitivity``, ``searchable``).

    Optional ``derived`` turns this field into a computed value (P05-US6).
    The case writer never supplies the field; the evaluator in
    ``application/cases/compute_derived.py`` derives it from sibling
    field keys declared in ``derived.operands``.

    ``label_history`` records every rename in version order so that the
    UI can present the field's full label history without losing the
    stable key identity (P05-US8 / FM-012).
    """

    key: str
    label: str
    type: str
    required: bool = False
    enum_values: tuple[str, ...] = ()
    sensitivity: str = "internal"
    searchable: bool = False
    derived: DerivedFieldSpec | None = None
    label_history: tuple[LabelRename, ...] = ()

    def __post_init__(self) -> None:
        if not self.key or not self.key.replace("_", "").isalnum():
            raise InvariantViolation(f"invalid field key: {self.key!r}")
        if not self.label.strip():
            raise InvariantViolation(f"field label is required for {self.key!r}")
        if self.type not in _FIELD_TYPES:
            raise InvariantViolation(f"unsupported field type {self.type!r} for {self.key!r}")
        if self.type in {"enum", "multichoice"} and not self.enum_values:
            raise InvariantViolation(f"enum/multichoice field {self.key!r} requires enum_values")
        if self.enum_values and any(not v for v in self.enum_values):
            raise InvariantViolation(f"enum_values for {self.key!r} contain empty entries")
        if self.sensitivity not in _SENSITIVITY_LEVELS:
            raise InvariantViolation(
                f"unsupported sensitivity {self.sensitivity!r} for {self.key!r}"
            )
        if self.derived is not None:
            _validate_derived_field_shape(self)
        if self.label_history:
            _validate_label_history(self)

    def rename_label(
        self,
        *,
        new_label: str,
        at_version: int,
        at: datetime | None = None,
    ) -> CaseTypeField:
        """Return a new field with ``new_label`` and an appended history entry.

        The machine ``key`` is preserved (P05-US8 / FM-012) so existing
        case data, integrations, and audit references that use the key
        remain valid. ``previous==new_label`` is rejected as a no-op so
        callers cannot pollute history.
        """
        if not new_label.strip():
            raise InvariantViolation("new label must be non-empty")
        if new_label == self.label:
            raise InvariantViolation(f"rename is a no-op (current label already {new_label!r})")
        appended = self.label_history + (
            LabelRename(
                previous=self.label,
                current=new_label,
                at_version=at_version,
                at=at,
            ),
        )
        return replace(self, label=new_label, label_history=appended)


def _validate_derived_field_shape(field: CaseTypeField) -> None:
    """Constrain ``derived`` to safe shapes only (P05-US6 / FM-012).

    Why these constraints: Pillar P05 §2 declares that v1 ships a
    declarative, non-code evaluator. We therefore refuse derived fields
    whose source operands would require I/O, recursion, or unbounded
    computation. The shape check enforces:

    - Every operand key is a non-empty string.
    - Arithmetic operators are only declared on numeric fields.
    - ``concat`` operands reference text-like fields.
    - Every referenced operand key is defined elsewhere in the schema
      (this validator runs with a bare ``CaseTypeField``; cross-field
      operand presence is enforced at the schema level).
    """
    spec = field.derived
    assert spec is not None
    for operand in spec.operands:
        if not operand or not isinstance(operand, str):
            raise InvariantViolation(
                f"derived operand must be a non-empty string key (got {operand!r})"
            )
    if (
        spec.operator in {"add", "sub", "mul", "div"}
        and field.type not in _DERIVED_ARITHMETIC_TYPES
    ):
        raise InvariantViolation(
            f"derived arithmetic operator {spec.operator!r} requires field type"
            f" 'integer' or 'decimal' (got {field.type!r})"
        )
    if spec.operator in {"concat", "coalesce"} and field.type not in {
        "text",
        "longtext",
        "enum",
        "multichoice",
        "json",
    }:
        raise InvariantViolation(
            f"derived {spec.operator!r} requires a text-like field type (got {field.type!r})"
        )


def _validate_label_history(field: CaseTypeField) -> None:
    """Ensure ``label_history`` is ordered by version and contains no duplicates."""
    if not field.label_history:
        return
    versions = [r.at_version for r in field.label_history]
    if versions != sorted(versions):
        raise InvariantViolation(f"label_history for {field.key!r} must be sorted by version")
    if len(set(versions)) != len(versions):
        raise InvariantViolation(f"label_history for {field.key!r} contains duplicate versions")


@dataclass(frozen=True, slots=True)
class CaseTypeSchema:
    """The complete published schema for a CaseTypeVersion.

    Carries field definitions, allowed document classifications, and
    declarative rules. Stored as JSON in the repository.

    Document upload policy (P03-US4 / FM-022): the schema may declare
    an optional ``document_max_size_bytes`` (default ``None`` means
    "use the application default") and an optional
    ``document_allowed_content_types`` tuple (empty means "any"). Both
    fields are JSON-serialized through the same ``schema_json`` blob
    so existing rows do not need a migration.

    ``document_reject_duplicates`` (default ``False``) rejects an
    upload whose SHA-256 already exists on the same case (T055). Set
    through the admin draft blob like the other policy keys; old rows
    without the key deserialize to ``False``.
    """

    fields: tuple[CaseTypeField, ...] = ()
    document_classifications: tuple[str, ...] = ()
    rules: tuple[dict[str, Any], ...] = ()
    document_max_size_bytes: int | None = None
    document_allowed_content_types: tuple[str, ...] = ()
    document_reject_duplicates: bool = False

    def field_keys(self) -> tuple[str, ...]:
        return tuple(f.key for f in self.fields)

    def required_field_keys(self) -> tuple[str, ...]:
        return tuple(f.key for f in self.fields if f.required)

    def field(self, key: str) -> CaseTypeField | None:
        for f in self.fields:
            if f.key == key:
                return f
        return None

    def sensitive_field_keys(self) -> tuple[str, ...]:
        """Return the keys of fields at or above the restricted threshold.

        Used by the field-level redactor (P11-US6) to determine which
        keys must be replaced with the ``Redacted`` marker when the
        actor lacks the sensitive-read grant. Field-level sensitivity is
        sourced from the published ``CaseTypeVersion`` — the schema is
        the contract, not the data values.
        """
        return tuple(f.key for f in self.fields if f.sensitivity in {"restricted", "phi"})

    def has_classification(self, classification: str) -> bool:
        return classification in self.document_classifications

    def derived_fields(self) -> tuple[CaseTypeField, ...]:
        """Return fields whose value is computed (P05-US6)."""
        return tuple(f for f in self.fields if f.derived is not None)

    def assert_derived_operands_resolvable(self) -> None:
        """Reject derived fields whose operand keys are not defined here.

        Called at publish time so a draft that references a missing
        operand cannot reach a published version. Without this check, a
        malformed schema would silently produce ``None`` for the derived
        field at runtime.
        """
        defined = {f.key for f in self.fields}
        for f in self.derived_fields():
            spec = f.derived
            assert spec is not None
            for operand in spec.operands:
                if operand not in defined:
                    raise InvariantViolation(
                        f"derived field {f.key!r} references undefined operand {operand!r}"
                    )
            if spec.fallback and spec.fallback not in defined:
                raise InvariantViolation(
                    f"derived field {f.key!r} references undefined fallback {spec.fallback!r}"
                )


@dataclass(frozen=True, slots=True)
class CaseTypeVersion:
    """A single version of a CaseType's schema.

    Lifecycle: ``draft`` → ``published`` → ``retired``. ``publish()`` is
    irreversible: the resulting instance is frozen with a ``published_at``
    timestamp and a ``published_by`` actor. Once published, the version's
    schema is immutable — any subsequent mutation attempt raises
    ``InvariantViolation`` (FR-015, AC-007).
    """

    id: UUID
    tenant_id: UUID
    case_type_id: UUID
    version_number: int
    schema: CaseTypeSchema
    status: CaseTypeVersionStatus
    published_at: datetime | None = None
    published_by: UUID | None = None
    created_at: datetime | None = None

    # ----- factories --------------------------------------------------------

    @staticmethod
    def draft(
        *,
        tenant_id: UUID,
        case_type_id: UUID,
        version_number: int,
        schema: CaseTypeSchema,
        now: datetime | None = None,
    ) -> CaseTypeVersion:
        if version_number < 1:
            raise InvariantViolation("version_number must be >= 1")
        return CaseTypeVersion(
            id=uuid4(),
            tenant_id=tenant_id,
            case_type_id=case_type_id,
            version_number=version_number,
            schema=schema,
            status=CaseTypeVersionStatus.DRAFT,
            published_at=None,
            published_by=None,
            created_at=now,
        )

    # ----- transitions ------------------------------------------------------

    def publish(self, *, actor_id: UUID, at: datetime) -> CaseTypeVersion:
        """Transition ``draft`` → ``published``. Irreversible.

        Returns a new immutable instance with the ``published_at`` and
        ``published_by`` fields stamped. Raises ``InvariantViolation`` if
        the version is not currently in ``draft`` status.
        """
        if self.status is not CaseTypeVersionStatus.DRAFT:
            raise InvariantViolation(f"cannot publish a {self.status.value} version")
        return CaseTypeVersion(
            id=self.id,
            tenant_id=self.tenant_id,
            case_type_id=self.case_type_id,
            version_number=self.version_number,
            schema=self.schema,
            status=CaseTypeVersionStatus.PUBLISHED,
            published_at=at,
            published_by=actor_id,
            created_at=self.created_at,
        )

    def retire(self) -> CaseTypeVersion:
        """Transition ``published`` → ``retired``. Irreversible.

        Retired versions are historical; no new cases may be created
        against them, but the version remains queryable so existing
        cases retain their schema binding (FM-008 / P05-US1).
        """
        if self.status is not CaseTypeVersionStatus.PUBLISHED:
            raise InvariantViolation(
                f"only published versions can be retired (got {self.status.value})"
            )
        return CaseTypeVersion(
            id=self.id,
            tenant_id=self.tenant_id,
            case_type_id=self.case_type_id,
            version_number=self.version_number,
            schema=self.schema,
            status=CaseTypeVersionStatus.RETIRED,
            published_at=self.published_at,
            published_by=self.published_by,
            created_at=self.created_at,
        )

    # ----- immutability guard ----------------------------------------------

    def assert_mutable(self) -> None:
        """Raise if the version is not in ``draft`` status.

        Used by repository helpers that guard against accidental UPDATE
        on a published version (FR-015). Database-level immutability is
        added in a later phase via a trigger + ORM listener (FM-003,
        AC-008); the domain invariant is the primary mechanism.
        """
        if self.status is not CaseTypeVersionStatus.DRAFT:
            raise InvariantViolation(f"version is {self.status.value} and cannot be modified")


@dataclass(frozen=True, slots=True)
class CaseType:
    """A business category of case.

    A CaseType aggregates many ``CaseTypeVersion``s. Only one is marked
    as the current (effective) version; that is the version new cases
    pin at creation time. Existing cases remain bound to their original
    version (FM-008).
    """

    id: UUID
    tenant_id: UUID
    key: str
    name: str
    description: str
    sensitivity_default: str = "internal"
    current_version_id: UUID | None = field(default=None)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.key or not self.key.replace("_", "").replace("-", "").isalnum():
            raise InvariantViolation(f"invalid case type key: {self.key!r}")
        if self.sensitivity_default not in _SENSITIVITY_LEVELS:
            raise InvariantViolation(f"invalid sensitivity_default: {self.sensitivity_default!r}")

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        key: str,
        name: str,
        description: str = "",
        sensitivity_default: str = "internal",
        now: datetime | None = None,
    ) -> CaseType:
        return CaseType(
            id=uuid4(),
            tenant_id=tenant_id,
            key=key,
            name=name,
            description=description,
            sensitivity_default=sensitivity_default,
            current_version_id=None,
            created_at=now,
            updated_at=now,
        )

    def with_published_version(self, version: CaseTypeVersion) -> CaseType:
        """Promote a published version to the CaseType's current version.

        Validates tenant ownership and that the version belongs to this
        CaseType. The result carries an updated ``updated_at`` so the
        audit row can read it.
        """
        if version.tenant_id != self.tenant_id:
            raise InvariantViolation("tenant mismatch on case type version")
        if version.case_type_id != self.id:
            raise InvariantViolation("version does not belong to this case type")
        if version.status is not CaseTypeVersionStatus.PUBLISHED:
            raise InvariantViolation("only published versions can be set as current")
        return replace(
            self,
            current_version_id=version.id,
            updated_at=version.published_at,
        )


__all__ = [
    "CaseType",
    "CaseTypeField",
    "CaseTypeSchema",
    "CaseTypeVersion",
    "CaseTypeVersionStatus",
    "DerivedFieldSpec",
    "LabelRename",
]
