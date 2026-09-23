"""Compute derived fields on a case's structured data (P05-US6 / FM-012).

This module is the **evaluator** for the declarative ``derived`` spec
attached to ``CaseTypeField`` (``app/domain/case_type.py``). It is a
pure function: given a published schema and a candidate ``data`` dict,
it returns a new dict with every declared derived field filled in.

Why a pure function:
- Determinism. Two cases with identical data and identical schema
  produce identical derived values, so re-running on edit, replay, or
  audit reconstruction yields the same answer.
- Testability. No I/O, no time, no randomness means the suite can
  cover edge cases without fakes.
- Constitutional alignment. Pillar P05 §2 forbids arbitrary code in
  v1; this evaluator implements the operator vocabulary exhaustively
  and refuses everything else at construction time.

Failure mode: an operand that is required by the derived spec but
absent from the data yields ``None`` for the derived field. The caller
(case-create / case-edit) treats that as the stored value; downstream
read paths surface ``null``. The evaluator never raises on missing
operands because a draft case is allowed to be partially populated.
The only raises are for genuine contradictions (non-numeric operand to
arithmetic operator, division by zero).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.domain.case_type import CaseTypeSchema
from app.domain.errors import InvariantViolation


def compute_derived_fields(
    data: Mapping[str, Any],
    schema: CaseTypeSchema,
) -> dict[str, Any]:
    """Return ``data`` augmented with every derived field's value.

    Caller-supplied values for derived keys are **dropped**: the schema
    owns the value. Everything else is preserved untouched so the result
    is a drop-in replacement for ``data`` in the case aggregate.
    """
    result: dict[str, Any] = dict(data)
    for field in schema.derived_fields():
        spec = field.derived
        assert spec is not None
        try:
            value = _evaluate(spec, result, schema)
        except _DerivedOperandError as exc:
            raise InvariantViolation(f"derived field {field.key!r} failed: {exc}") from exc
        result[field.key] = value
    return result


class _DerivedOperandError(Exception):
    """Raised internally when an operand cannot be coerced to a value."""


def _evaluate(  # noqa: PLR0911, PLR0912, PLR0915 — operator dispatch table
    spec: Any,
    data: Mapping[str, Any],
    schema: CaseTypeSchema,
) -> Any:
    op = spec.operator
    operands = [_resolve(o, data, schema) for o in spec.operands]
    # Why: arithmetic / concat operators cannot meaningfully operate on
    # a missing operand. Draft / partial cases therefore yield ``None``
    # for arithmetic-derived fields; operators that explicitly model
    # absence (coalesce, if_present) inspect each operand below.
    if op in {"add", "sub", "mul", "div", "concat"} and any(o is None for o in operands):
        return None
    if op == "add":
        return _arithmetic(operands, lambda a, b: a + b)
    if op == "sub":
        return _arithmetic(operands, lambda a, b: a - b)
    if op == "mul":
        return _arithmetic(operands, lambda a, b: a * b)
    if op == "div":
        if operands[1] == 0:
            raise _DerivedOperandError("division by zero")
        # why: int / int stays int in Python; coerce to float for a
        # stable, type-predictable result on decimal cases.
        a, b = operands
        if isinstance(a, int) and isinstance(b, int) and a % b == 0:
            return a // b
        return a / b
    if op == "concat":
        return "".join(_as_text(o) for o in operands)
    if op == "coalesce":
        for o in operands:
            if _is_present(o):
                return o
        return None
    if op == "if_present":
        primary = operands[0]
        if _is_present(primary):
            return primary
        if spec.fallback is None:
            return None
        return _resolve(spec.fallback, data, schema)
    raise _DerivedOperandError(f"unsupported operator {op!r}")


def _resolve(key: str, data: Mapping[str, Any], schema: CaseTypeSchema) -> Any:
    """Return ``data[key]`` if present, else the field's default (``None``).

    The lookup prefers the original input so callers may pre-populate a
    derived key during testing. Fields missing from the input return
    ``None``; the operator decides what that means.
    """
    if key in data:
        return data[key]
    field = schema.field(key)
    if field is None:
        raise _DerivedOperandError(f"operand {key!r} is not defined in the schema")
    return None


def _arithmetic(
    operands: list[Any],
    fold: Any,
) -> int | float:
    if len(operands) < 2:
        raise _DerivedOperandError("arithmetic operator needs at least 2 operands")
    coerced = [_as_number(o) for o in operands]
    total = coerced[0]
    for nxt in coerced[1:]:
        total = fold(total, nxt)
    return total


def _as_number(value: Any) -> int | float:
    if isinstance(value, bool):
        # Why: bool is a subclass of int; reject booleans from numeric
        # derivations to avoid accidental ``True + True == 2``.
        raise _DerivedOperandError("boolean is not a valid numeric operand")
    if isinstance(value, (int, float)):
        return value
    raise _DerivedOperandError(f"cannot coerce {value!r} to a number")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    raise _DerivedOperandError(f"cannot coerce {value!r} to text")


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return not (isinstance(value, (list, tuple, dict)) and len(value) == 0)


__all__ = [
    "compute_derived_fields",
]
