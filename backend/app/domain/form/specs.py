"""Form layout dataclasses: fields, sections, definition, and layout queries.

Structural half of the form spec (P07): :class:`FormFieldSpec` /
:class:`FormSectionSpec` / :class:`FormDefinition` plus the per-field shape
checks run at construction time and the definition-level layout queries
(``effective_tab_order``, ``read_only_field_keys``, ``_field_map``). Draft-
and publish-time definition orchestration lives in :mod:`validation`;
submitted-value checks live in :mod:`values`.

Symbols: ``FormFieldSpec``, ``FormSectionSpec``, ``FormDefinition``,
``_check_default_matches_type``, ``_check_formula_shape``,
``_check_formula_operand_shape``, ``_require_operand_keys``, ``_field_map``,
``effective_tab_order``, ``read_only_field_keys``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.domain.errors import InvariantViolation

from .constants import (
    _CALCULATED_OPERATORS,
    _CONNECTOR_RE,
    _FIELD_KEY_RE,
    _FIELD_TYPES,
    _NUMERIC_FIELD_TYPES,
    _SECTION_KINDS,
    _SIGNATURE_KINDS,
    _SYSTEM_PROPERTIES,
    _TEXT_LIKE_FIELD_TYPES,
)
from .helpers import _reject_code_like_key


@dataclass(frozen=True, slots=True)
class FormFieldSpec:
    """One field on a form; ``key`` is stable, ``label`` may rename.

    ``field_type`` selects validation family; ``read_only``/``system``/``calculated``
    never accept inbound values. ``tab_order`` is keyboard order (unique, None when
    not focusable). Range/type bounds, ``options``, ``pattern``, ``validation_message``,
    ``default_value`` are declarative. Specialty payloads are type-gated.
    Raises InvariantViolation on any shape defect.
    """

    key: str
    label: str
    required: bool = False
    placeholder: str | None = None
    help_text: str | None = None
    field_type: str = "text"
    read_only: bool = False
    tab_order: int | None = None
    options: tuple[str, ...] = ()
    min_length: int | None = None
    max_length: int | None = None
    minimum: float | None = None
    maximum: float | None = None
    pattern: str | None = None
    validation_message: str | None = None
    default_value: str | int | float | bool | None = None
    formula: Mapping[str, Any] | None = None
    lookup_source: str | None = None
    attachment_classification: str | None = None
    attachment_max_size_bytes: int | None = None
    attachment_allowed_content_types: tuple[str, ...] = ()
    signature_kind: str | None = None
    signature_connector: str | None = None
    system_property: str | None = None

    def __post_init__(self) -> None:
        """Validate field shape; raises InvariantViolation on defect.

        Delegates one helper per rule group so no single function exceeds the
        complexity ceiling (R2). Helpers run in the original check order, so the
        first failing rule still reports the same message as before.
        """
        self._check_identity()
        self._check_options()
        self._check_length_bounds()
        self._check_numeric_bounds()
        self._check_pattern_and_message()
        if self.default_value is not None:
            _check_default_matches_type(self)
        self._check_formula_binding()
        self._check_lookup_binding()
        self._check_attachment_binding()
        self._check_signature_binding()
        self._check_system_binding()

    def _check_identity(self) -> None:
        """Key, label, declared type, and tab_order identity rules."""
        if not self.key or not _FIELD_KEY_RE.match(self.key):
            raise InvariantViolation(f"invalid form field key: {self.key!r}")
        if not self.label.strip():
            raise InvariantViolation(f"field label required for {self.key!r}")
        if self.field_type not in _FIELD_TYPES:
            raise InvariantViolation(f"unsupported type {self.field_type!r} for {self.key!r}")
        if self.tab_order is not None and self.tab_order < 0:
            raise InvariantViolation(f"tab_order for {self.key!r} must be >= 0")
        if self.read_only and self.tab_order is not None:
            raise InvariantViolation(f"read-only {self.key!r} must not declare tab_order")

    def _check_options(self) -> None:
        """``options`` is required on enum/multichoice and forbidden elsewhere."""
        if self.field_type in {"enum", "multichoice"}:
            if not self.options:
                raise InvariantViolation(f"{self.key!r} requires options")
            if any(not v for v in self.options) or len(set(self.options)) != len(self.options):
                raise InvariantViolation(f"bad options for {self.key!r}")
        elif self.options:
            raise InvariantViolation(f"options only on enum/multichoice (got {self.key!r})")

    def _check_length_bounds(self) -> None:
        """``min_length``/``max_length`` must be non-negative, ordered, text-like."""
        if self.min_length is not None and self.min_length < 0:
            raise InvariantViolation(f"min_length for {self.key!r} must be >= 0")
        if self.max_length is not None and self.max_length < 0:
            raise InvariantViolation(f"max_length for {self.key!r} must be >= 0")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise InvariantViolation(f"min_length > max_length for {self.key!r}")
        if (
            self.min_length is not None or self.max_length is not None
        ) and self.field_type not in _TEXT_LIKE_FIELD_TYPES:
            raise InvariantViolation(f"length bounds need text-like type (got {self.key!r})")

    def _check_numeric_bounds(self) -> None:
        """``minimum``/``maximum`` must be ordered and only on integer/decimal."""
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise InvariantViolation(f"minimum > maximum for {self.key!r}")
        if (
            self.minimum is not None or self.maximum is not None
        ) and self.field_type not in _NUMERIC_FIELD_TYPES:
            raise InvariantViolation(f"numeric bounds need integer/decimal (got {self.key!r})")

    def _check_pattern_and_message(self) -> None:
        """``pattern`` compiles on text-like types; ``validation_message`` is non-blank."""
        if self.pattern is not None:
            if self.field_type not in {"text", "longtext"}:
                raise InvariantViolation(f"pattern needs text/longtext (got {self.key!r})")
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise InvariantViolation(f"invalid pattern for {self.key!r}") from exc
        if self.validation_message is not None and not self.validation_message.strip():
            raise InvariantViolation(f"validation_message for {self.key!r} must be non-empty")

    def _check_formula_binding(self) -> None:
        """``formula`` is mandatory on calculated and forbidden on other types."""
        if self.field_type == "calculated":
            if self.formula is None:
                raise InvariantViolation(f"calculated {self.key!r} requires formula")
            _check_formula_shape(self.formula, field_key=self.key)
            if not self.read_only:
                raise InvariantViolation(f"calculated {self.key!r} must be read_only")
        elif self.formula is not None:
            raise InvariantViolation(f"formula only on calculated (got {self.key!r})")

    def _check_lookup_binding(self) -> None:
        """``lookup_source`` is mandatory on lookup and forbidden elsewhere."""
        if self.field_type == "lookup":
            if not self.lookup_source or not _FIELD_KEY_RE.match(self.lookup_source):
                raise InvariantViolation(f"lookup {self.key!r} requires source slug")
            _reject_code_like_key(self.lookup_source, field_key=self.key)
        elif self.lookup_source is not None:
            raise InvariantViolation(f"lookup_source only on lookup (got {self.key!r})")

    def _check_attachment_binding(self) -> None:
        """Attachment classification/size/content-types are mandatory on attachment."""
        if self.field_type == "attachment":
            if not (self.attachment_classification or "").strip():
                raise InvariantViolation(f"attachment {self.key!r} requires classification")
            if self.attachment_max_size_bytes is not None and self.attachment_max_size_bytes <= 0:
                raise InvariantViolation(f"attachment max size for {self.key!r} must be > 0")
            if any(not c.strip() for c in self.attachment_allowed_content_types):
                raise InvariantViolation(f"bad content types for {self.key!r}")
        elif (
            self.attachment_classification is not None
            or self.attachment_max_size_bytes is not None
            or self.attachment_allowed_content_types
        ):
            raise InvariantViolation(f"attachment attrs only on attachment (got {self.key!r})")

    def _check_signature_binding(self) -> None:
        """Signature kind is whitelisted; connector syntax rejects code-like keys."""
        if self.field_type == "signature":
            if self.signature_kind not in _SIGNATURE_KINDS:
                raise InvariantViolation(
                    f"signature {self.key!r} needs kind {sorted(_SIGNATURE_KINDS)}"
                )
            if self.signature_connector is not None:
                if not _CONNECTOR_RE.match(self.signature_connector):
                    raise InvariantViolation(f"bad connector for {self.key!r}")
                _reject_code_like_key(self.signature_connector, field_key=self.key)
        elif self.signature_kind is not None or self.signature_connector is not None:
            raise InvariantViolation(f"signature attrs only on signature (got {self.key!r})")

    def _check_system_binding(self) -> None:
        """System fields expose whitelisted properties and are always read-only."""
        if self.field_type == "system":
            if self.system_property not in _SYSTEM_PROPERTIES:
                raise InvariantViolation(f"system {self.key!r} needs {sorted(_SYSTEM_PROPERTIES)}")
            if not self.read_only or self.required or self.tab_order is not None:
                raise InvariantViolation(
                    f"system {self.key!r} must be read_only, optional, untabbed"
                )
        elif self.system_property is not None:
            raise InvariantViolation(f"system_property only on system (got {self.key!r})")


@dataclass(frozen=True, slots=True)
class FormSectionSpec:
    """Field group; unit of conditional visibility. ``kind`` in section/panel/repeating."""

    key: str
    label: str
    fields: tuple[FormFieldSpec, ...]
    visible_when: Mapping[str, Any] | None = None
    kind: str = "section"
    description: str | None = None
    min_items: int | None = None
    max_items: int | None = None

    def __post_init__(self) -> None:
        """Validate section shape; raises InvariantViolation on defect."""
        if not self.key or not _FIELD_KEY_RE.match(self.key):
            raise InvariantViolation(f"invalid section key: {self.key!r}")
        if not self.label.strip():
            raise InvariantViolation(f"section label required for {self.key!r}")
        if self.kind not in _SECTION_KINDS:
            raise InvariantViolation(f"bad kind {self.kind!r} for {self.key!r}")
        if self.kind == "repeating":
            if self.min_items is not None and self.min_items < 0:
                raise InvariantViolation(f"min_items for {self.key!r} must be >= 0")
            if self.max_items is not None and self.max_items < 1:
                raise InvariantViolation(f"max_items for {self.key!r} must be >= 1")
            if (
                self.min_items is not None
                and self.max_items is not None
                and self.min_items > self.max_items
            ):
                raise InvariantViolation(f"min_items > max_items for {self.key!r}")
        elif self.min_items is not None or self.max_items is not None:
            raise InvariantViolation(f"min/max only on repeating (got {self.key!r})")


@dataclass(frozen=True, slots=True)
class FormDefinition:
    """Canonical layout, ``validations`` when/then rules, optional ``steps``."""

    sections: tuple[FormSectionSpec, ...] = ()
    validations: tuple[Mapping[str, Any], ...] = ()
    steps: tuple[Mapping[str, Any], ...] = ()


def _check_default_matches_type(field: FormFieldSpec) -> None:
    """Check ``default_value`` matches ``field_type``; raises InvariantViolation otherwise."""
    default = field.default_value
    assert default is not None
    kind = field.field_type
    if kind in {"text", "longtext", "date", "datetime"}:
        if not isinstance(default, str):
            raise InvariantViolation(f"default for {field.key!r} must be str")
    elif kind == "integer":
        if not isinstance(default, int) or isinstance(default, bool):
            raise InvariantViolation(f"default for {field.key!r} must be int")
    elif kind == "decimal":
        if not isinstance(default, (int, float)) or isinstance(default, bool):
            raise InvariantViolation(f"default for {field.key!r} must be numeric")
    elif kind == "boolean":
        if not isinstance(default, bool):
            raise InvariantViolation(f"default for {field.key!r} must be bool")
    elif kind in {"enum", "multichoice"}:
        if not isinstance(default, str) or default not in field.options:
            raise InvariantViolation(f"default for {field.key!r} must be an option")
    else:
        raise InvariantViolation(f"default not allowed on {kind} {field.key!r}")


def _check_formula_shape(formula: Mapping[str, Any], *, field_key: str) -> None:
    """Validate calculated formula ``{op, operands, fallback?}``; rejects code/shapes."""
    if not isinstance(formula, Mapping):
        raise InvariantViolation(f"formula for {field_key!r} must be mapping")
    for k in formula:
        if isinstance(k, str):
            _reject_code_like_key(k, field_key=field_key)
    if set(formula.keys()) - {"op", "operands", "fallback"}:
        raise InvariantViolation(f"formula for {field_key!r} allows op/operands/fallback")
    raw_op = formula.get("op")
    if not isinstance(raw_op, str) or raw_op not in _CALCULATED_OPERATORS:
        raise InvariantViolation(f"bad calculated op {raw_op!r} for {field_key!r}")
    _reject_code_like_key(raw_op, field_key=field_key)
    operands = _check_formula_operand_shape(raw_op, formula, field_key=field_key)
    _require_operand_keys(operands, field_key=field_key)


def _check_formula_operand_shape(
    raw_op: str, formula: Mapping[str, Any], *, field_key: str
) -> list[Any] | tuple[Any, ...]:
    """Validate operand cardinality per operator; returns the operand collection.

    ``if_present`` takes exactly one operand plus a fallback key; every other
    operator takes >= 2 operands (and ``coalesce`` forbids ``fallback``).
    """
    operands = formula.get("operands")
    if raw_op == "if_present":
        if not isinstance(operands, (list, tuple)) or len(operands) != 1:
            raise InvariantViolation(f"if_present for {field_key!r} needs 1 operand")
        fb = formula.get("fallback")
        if not isinstance(fb, str) or not _FIELD_KEY_RE.match(fb):
            raise InvariantViolation(f"if_present for {field_key!r} needs fallback key")
        _reject_code_like_key(fb, field_key=field_key)
    else:
        if not isinstance(operands, (list, tuple)) or len(operands) < 2:
            raise InvariantViolation(f"{raw_op!r} for {field_key!r} needs >= 2 operands")
        if raw_op == "coalesce" and "fallback" in formula:
            raise InvariantViolation(f"coalesce for {field_key!r} takes no fallback")
    assert isinstance(operands, (list, tuple))
    return operands


def _require_operand_keys(operands: list[Any] | tuple[Any, ...], *, field_key: str) -> None:
    """Every formula operand must be a well-formed, non-code field key."""
    for operand in operands:
        if not isinstance(operand, str) or not _FIELD_KEY_RE.match(operand):
            raise InvariantViolation(f"formula operand {operand!r} must be field key")
        _reject_code_like_key(operand, field_key=field_key)


def _field_map(definition: FormDefinition) -> dict[str, FormFieldSpec]:
    """Map field key->spec for the whole definition (definition order preserved)."""
    out: dict[str, FormFieldSpec] = {}
    for section in definition.sections:
        for field in section.fields:
            out[field.key] = field
    return out


def effective_tab_order(definition: FormDefinition) -> tuple[str, ...]:
    """Render tab order: explicit ``tab_order`` first, then definition order.

    Read-only/system/calculated fields are not focusable and are excluded.
    Raises InvariantViolation on duplicate tab_order (also caught at draft time).
    """
    ordered: list[tuple[int, int, str]] = []
    fallback: list[tuple[int, str]] = []
    pos = 0
    seen: set[int] = set()
    for section in definition.sections:
        for field in section.fields:
            is_focusable = not field.read_only and field.field_type not in {"system", "calculated"}
            if field.tab_order is not None:
                if field.tab_order in seen:
                    raise InvariantViolation(f"duplicate tab_order {field.tab_order}")
                seen.add(field.tab_order)
                if is_focusable:
                    ordered.append((field.tab_order, pos, field.key))
            elif is_focusable:
                fallback.append((pos, field.key))
            pos += 1
    ordered.sort()
    fallback.sort()
    return tuple(k for _, _, k in ordered) + tuple(k for _, k in fallback)


def read_only_field_keys(definition: FormDefinition) -> frozenset[str]:
    """Keys that never accept inbound values (read_only, system, calculated)."""
    out: set[str] = set()
    for section in definition.sections:
        for field in section.fields:
            if field.read_only or field.field_type in {"system", "calculated"}:
                out.add(field.key)
    return frozenset(out)


__all__ = [
    "Any",
    "FormDefinition",
    "FormFieldSpec",
    "FormSectionSpec",
    "Mapping",
    "_check_default_matches_type",
    "_check_formula_operand_shape",
    "_check_formula_shape",
    "_field_map",
    "_require_operand_keys",
    "dataclass",
    "effective_tab_order",
    "read_only_field_keys",
]
