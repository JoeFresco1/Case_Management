"""Condition DSL for section visibility and validation ``when`` clauses.

One whitelisted single-operator mapping shape (``eq``/``ne``/``in``/``exists``/
``truthy``) shared by publish-time structural checks (:func:`_check_condition`
and friends) and the runtime evaluator (:func:`_evaluate_visibility`), plus
reference collection (:func:`_collect_referenced`,
:func:`_section_visibility_referenced_keys`) used for cycle detection and the
submission-merge allowlist.

Symbols: ``_check_condition``, ``_check_comparison_operand``,
``_check_exists_operand``, ``_check_truthy_operand``, ``_evaluate_visibility``,
``_section_visibility_referenced_keys``, ``_collect_referenced``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.domain.errors import InvariantViolation

from .constants import _ALLOWED_OPERATORS, _MAX_CONDITION_DEPTH
from .helpers import _is_json_scalar, _reject_code_like_key, _require_field_key
from .specs import FormDefinition


def _check_condition(condition: Mapping[str, Any], *, _depth: int = 0) -> None:
    """Validate one whitelisted condition; rejects composites/code/dangling shapes."""
    if _depth > _MAX_CONDITION_DEPTH:
        raise InvariantViolation(f"condition depth exceeds {_MAX_CONDITION_DEPTH}")
    if not isinstance(condition, Mapping) or not condition or len(condition) != 1:
        raise InvariantViolation("condition must be a single-operator mapping")
    ((op, operand),) = condition.items()
    _reject_code_like_key(op, field_key="<condition>")
    if op not in _ALLOWED_OPERATORS:
        raise InvariantViolation(f"unknown form operator: {op!r}")
    if op in {"eq", "ne", "in"}:
        _check_comparison_operand(op, operand)
    elif op == "exists":
        _check_exists_operand(operand)
    else:  # truthy — the only remaining whitelisted operator
        _check_truthy_operand(operand)


def _check_comparison_operand(op: str, operand: Any) -> None:
    """Validate ``eq``/``ne``/``in`` operand ``{field, value}``; raises on defect."""
    if not isinstance(operand, Mapping):
        raise InvariantViolation(f"{op!r} needs mapping operand")
    if set(operand.keys()) - {"field", "value"}:
        raise InvariantViolation(f"{op!r} allows only field/value")
    _require_field_key(operand.get("field"), message=f"{op!r} needs a field key")
    if "value" not in operand:
        raise InvariantViolation(f"{op!r} needs a value")
    value = operand["value"]
    if op == "in":
        if not isinstance(value, (list, tuple)) or not value:
            raise InvariantViolation("'in' needs non-empty list/tuple")
        if any(not _is_json_scalar(e) for e in value):
            raise InvariantViolation("'in' values must be JSON scalars")
    elif not _is_json_scalar(value):
        raise InvariantViolation(f"{op!r} value must be JSON scalar")


def _check_exists_operand(operand: Any) -> None:
    """Validate ``exists`` operand (bare field key or ``{field}``); raises on defect."""
    if isinstance(operand, str):
        _require_field_key(operand, message="'exists' needs field key")
    elif isinstance(operand, Mapping):
        if set(operand.keys()) != {"field"}:
            raise InvariantViolation("'exists' allows only field")
        _require_field_key(operand.get("field"), message="'exists' needs field key")
    else:
        raise InvariantViolation("'exists' needs string or {field} mapping")


def _check_truthy_operand(operand: Any) -> None:
    """Validate ``truthy`` operand ``{field}``; raises on defect."""
    if not isinstance(operand, Mapping) or set(operand.keys()) != {"field"}:
        raise InvariantViolation("'truthy' needs {field}")
    _require_field_key(operand.get("field"), message="'truthy' needs field key")


def _evaluate_visibility(
    condition: Mapping[str, Any], context: Mapping[str, Any], *, _depth: int = 0
) -> bool:
    """Evaluate one whitelisted condition; raises InvariantViolation on bad shape/depth."""
    if _depth > _MAX_CONDITION_DEPTH:
        raise InvariantViolation(f"condition depth exceeds {_MAX_CONDITION_DEPTH}")
    ((op, operand),) = condition.items()
    if op == "exists":
        field = operand if isinstance(operand, str) else operand.get("field", "")
        return field in context and context[field] not in (None, "")
    if op == "truthy":
        return bool(context.get(operand.get("field", "")))
    field = operand.get("field", "")
    value: Any = operand.get("value")
    actual: Any = context.get(field)
    if op == "eq":
        return bool(actual == value)
    if op == "ne":
        return bool(actual != value)
    if op == "in":
        if not isinstance(actual, (list, tuple, set, str)):
            return False
        return bool(value in actual)
    raise InvariantViolation(f"unsupported operator at runtime: {op!r}")


def _section_visibility_referenced_keys(definition: FormDefinition) -> frozenset[str]:
    """All field keys referenced by any ``visible_when`` (submission-merge allowlist)."""
    referenced: set[str] = set()
    for section in definition.sections:
        if section.visible_when is None:
            continue
        _collect_referenced(section.visible_when, referenced)
    return frozenset(referenced)


def _collect_referenced(condition: Mapping[str, Any], out: set[str], *, _depth: int = 0) -> None:
    """Append every ``field`` reference in ``condition`` to ``out``."""
    if _depth > _MAX_CONDITION_DEPTH or not isinstance(condition, Mapping):
        return
    if "field" in condition and isinstance(condition["field"], str):
        out.add(condition["field"])
    for value in condition.values():
        if isinstance(value, Mapping):
            _collect_referenced(value, out, _depth=_depth + 1)


__all__ = [
    "_check_comparison_operand",
    "_check_condition",
    "_check_exists_operand",
    "_check_truthy_operand",
    "_collect_referenced",
    "_evaluate_visibility",
    "_section_visibility_referenced_keys",
]
