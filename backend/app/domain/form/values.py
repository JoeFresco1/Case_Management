"""Submitted-value validation and preservation merges (P07-US3).

``validate_field_value`` dispatches one submitted value to a per-type-family
validator that raises ``ValidationFailed`` naming the field (honouring
``validation_message``). The ``apply_*_preservation`` helpers merge a
submission over stored case data while dropping hidden and
read-only/system/calculated keys so those values can never be forged inbound.

Symbols: ``validate_field_value``, ``_validate_text_value``,
``_validate_integer_value``, ``_validate_decimal_value``,
``_validate_boolean_value``, ``_validate_nonempty_string``,
``_validate_multichoice_value``, ``apply_submission_preservation``,
``apply_hidden_field_preservation``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.domain.errors import ValidationFailed

from .specs import FormFieldSpec


def validate_field_value(field: FormFieldSpec, value: Any) -> None:
    """Validate one submitted value; raises ValidationFailed naming the field.

    Uses ``validation_message`` when set so T3-US1 failures name the field
    without losing entered data. Read-only/system/calculated are skipped by
    callers (preserved, never validated as inbound).

    Dispatches per type family so each validator stays below the complexity
    ceiling; unknown types are rejected by ``FormFieldSpec`` construction first.
    """
    msg = field.validation_message or f"invalid value for {field.key!r}"
    kind = field.field_type
    if kind in {"text", "longtext"}:
        _validate_text_value(field, value, msg)
    elif kind == "integer":
        _validate_integer_value(field, value, msg)
    elif kind == "decimal":
        _validate_decimal_value(field, value, msg)
    elif kind == "boolean":
        _validate_boolean_value(field, value, msg)
    elif kind in {"date", "datetime", "lookup", "signature"}:
        _validate_nonempty_string(field, value, msg)
    elif kind == "enum":
        if value not in field.options:
            raise ValidationFailed(msg, field=field.key)
    elif kind == "multichoice":
        _validate_multichoice_value(field, value, msg)
    elif kind == "attachment":
        if not isinstance(value, Mapping) or not str(value.get("document_id") or "").strip():
            raise ValidationFailed(
                msg or f"attachment {field.key!r} needs document_id", field=field.key
            )
    elif kind in {"calculated", "system"}:
        raise ValidationFailed(f"{kind} field {field.key!r} is not submittable", field=field.key)


def _validate_text_value(field: FormFieldSpec, value: Any, msg: str) -> None:
    """Text/longtext: string type, length bounds, and optional regex pattern."""
    if not isinstance(value, str):
        raise ValidationFailed(msg, field=field.key)
    if field.min_length is not None and len(value) < field.min_length:
        raise ValidationFailed(msg, field=field.key)
    if field.max_length is not None and len(value) > field.max_length:
        raise ValidationFailed(msg, field=field.key)
    if field.pattern is not None and re.search(field.pattern, value) is None:
        raise ValidationFailed(msg, field=field.key)


def _validate_integer_value(field: FormFieldSpec, value: Any, msg: str) -> None:
    """Integer: exact int (bool excluded) within optional min/max bounds."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationFailed(msg, field=field.key)
    if field.minimum is not None and float(value) < field.minimum:
        raise ValidationFailed(msg, field=field.key)
    if field.maximum is not None and float(value) > field.maximum:
        raise ValidationFailed(msg, field=field.key)


def _validate_decimal_value(field: FormFieldSpec, value: Any, msg: str) -> None:
    """Decimal: int or float (bool excluded) within optional min/max bounds."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValidationFailed(msg, field=field.key)
    if field.minimum is not None and float(value) < field.minimum:
        raise ValidationFailed(msg, field=field.key)
    if field.maximum is not None and float(value) > field.maximum:
        raise ValidationFailed(msg, field=field.key)


def _validate_boolean_value(field: FormFieldSpec, value: Any, msg: str) -> None:
    """Boolean: strict ``bool`` only (an int is not a boolean)."""
    if not isinstance(value, bool):
        raise ValidationFailed(msg, field=field.key)


def _validate_nonempty_string(field: FormFieldSpec, value: Any, msg: str) -> None:
    """Date/datetime/lookup/signature: non-blank string."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(msg, field=field.key)


def _validate_multichoice_value(field: FormFieldSpec, value: Any, msg: str) -> None:
    """Multichoice: non-empty list/tuple whose members are declared options."""
    if not isinstance(value, (list, tuple)) or not value:
        raise ValidationFailed(msg, field=field.key)
    if any(v not in field.options for v in value):
        raise ValidationFailed(msg, field=field.key)


def apply_submission_preservation(
    *,
    stored_data: Mapping[str, Any],
    submitted_data: Mapping[str, Any],
    hidden_keys: frozenset[str],
    read_only_keys: frozenset[str],
) -> dict[str, Any]:
    """Merge submit over stored, dropping hidden + read-only/system keys (P07-US3).

    Hidden fields keep stored values; read-only/system/calculated keep stored or
    platform values. Returns a new dict; inputs untouched.
    """
    merged: dict[str, Any] = dict(stored_data)
    for key, value in submitted_data.items():
        if key in hidden_keys or key in read_only_keys:
            continue
        merged[key] = value
    return merged


def apply_hidden_field_preservation(
    *,
    stored_data: Mapping[str, Any],
    submitted_data: Mapping[str, Any],
    hidden_keys: frozenset[str],
) -> dict[str, Any]:
    """Preserve hidden fields (P07-US3); read-only-aware variant is preferred for new code."""
    return apply_submission_preservation(
        stored_data=stored_data,
        submitted_data=submitted_data,
        hidden_keys=hidden_keys,
        read_only_keys=frozenset(),
    )


__all__ = [
    "ValidationFailed",
    "_validate_boolean_value",
    "_validate_decimal_value",
    "_validate_integer_value",
    "_validate_multichoice_value",
    "_validate_nonempty_string",
    "_validate_text_value",
    "apply_hidden_field_preservation",
    "apply_submission_preservation",
    "validate_field_value",
]
