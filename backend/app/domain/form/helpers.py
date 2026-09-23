"""Shared guard helpers for form keys, scalars, and formula operands.

Small pure predicates/validators referenced by more than one form submodule
(:mod:`specs`, :mod:`conditions`, :mod:`validation`, :mod:`calculated`).
Living in this leaf module keeps a single implementation per rule and avoids
circular imports between those modules.

Symbols: ``_require_field_key``, ``_is_json_scalar``, ``_reject_code_like_key``,
``_formula_operands``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.domain.errors import InvariantViolation

from .constants import _CODE_LIKE_KEYS, _FIELD_KEY_RE


def _require_field_key(raw: Any, *, message: str) -> str:
    """Assert ``raw`` is a well-formed non-code field key; returns it."""
    if not isinstance(raw, str) or not _FIELD_KEY_RE.match(raw):
        raise InvariantViolation(message)
    _reject_code_like_key(raw, field_key="<condition>")
    return raw


def _is_json_scalar(value: Any) -> bool:
    """True for inert JSON scalars (None/bool/int/float/str); dicts/lists are not scalars."""
    return value is None or isinstance(value, (str, int, float, bool))


def _reject_code_like_key(value: str, *, field_key: str) -> None:
    """Reject code-like identifiers; raises InvariantViolation when ``value`` contains one."""
    lowered = value.lower()
    for token in _CODE_LIKE_KEYS:
        if token in lowered:
            raise InvariantViolation(f"arbitrary code rejected for {field_key!r}: {value!r}")


def _formula_operands(formula: Mapping[str, Any]) -> tuple[str, ...]:
    """Operand keys from a validated formula mapping."""
    raw = formula.get("operands")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(e) for e in raw if isinstance(e, str))


__all__ = [
    "_formula_operands",
    "_is_json_scalar",
    "_reject_code_like_key",
    "_require_field_key",
]
