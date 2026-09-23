"""Deterministic calculated-field formula evaluation (P07, Constitution IX/XIV).

``evaluate_calculated`` is a pure function over the whitelisted operator set
defined in :mod:`constants`: arithmetic ops (add/sub/mul/div) share one
left-to-right evaluator that refuses division by zero, while the value-
selection ops (concat/coalesce/if_present) stay in the dispatcher so no
single branch tree exceeds the complexity ceiling. No I/O, no code execution.

Symbols: ``evaluate_calculated``, ``_as_number``, ``_evaluate_arithmetic``,
``_first_present``, ``_evaluate_if_present``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.domain.errors import InvariantViolation

from .constants import _ARITHMETIC_OPERATORS, _CALCULATED_OPERATORS
from .helpers import _formula_operands


def evaluate_calculated(formula: Mapping[str, Any], data: Mapping[str, Any]) -> Any:
    """Pure deterministic formula evaluation; raises InvariantViolation on bad shape/div0.

    Arithmetic ops share ``_evaluate_arithmetic``; the value-selection ops
    (concat/coalesce/if_present) are non-numeric and stay in the dispatcher so
    no single branch tree exceeds the complexity ceiling.
    """
    if not isinstance(formula, Mapping):
        raise InvariantViolation("formula must be mapping")
    op = str(formula.get("op"))
    if op not in _CALCULATED_OPERATORS:
        raise InvariantViolation(f"bad calculated op {op!r}")
    operands = _formula_operands(formula)
    vals = [data.get(k) for k in operands]
    if op in _ARITHMETIC_OPERATORS:
        return _evaluate_arithmetic(op, vals)
    if op == "concat":
        return "".join("" if v is None else str(v) for v in vals)
    if op == "coalesce":
        return _first_present(vals)
    return _evaluate_if_present(formula, vals, data)


def _as_number(value: Any) -> float:
    """Coerce an operand to float for arithmetic; rejects bools/non-numerics."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvariantViolation(f"numeric operand got {value!r}")
    return float(value)


def _evaluate_arithmetic(op: str, vals: list[Any]) -> float:
    """Evaluate ``add``/``sub``/``mul``/``div`` left-to-right; raises on div-by-zero."""
    if op == "add":
        return sum(_as_number(v) for v in vals)
    if op == "sub":
        first, rest = _as_number(vals[0]), vals[1:]
        for v in rest:
            first -= _as_number(v)
        return first
    if op == "mul":
        prod = 1.0
        for v in vals:
            prod *= _as_number(v)
        return prod
    # div — a zero denominator is an invariant violation, not a silent inf
    result = _as_number(vals[0])
    for v in vals[1:]:
        denom = _as_number(v)
        if denom == 0:
            raise InvariantViolation("division by zero in calculated field")
        result /= denom
    return result


def _first_present(vals: list[Any]) -> Any:
    """Return the first non-empty value (None/""/[]/{} count as absent)."""
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None


def _evaluate_if_present(
    formula: Mapping[str, Any], vals: list[Any], data: Mapping[str, Any]
) -> Any:
    """``if_present``: operand value when non-empty, else the ``fallback`` field."""
    if vals and vals[0] not in (None, "", [], {}):
        return vals[0]
    fb = formula.get("fallback")
    return data.get(str(fb)) if isinstance(fb, str) else None


__all__ = [
    "_as_number",
    "_evaluate_arithmetic",
    "_evaluate_if_present",
    "_first_present",
    "evaluate_calculated",
]
