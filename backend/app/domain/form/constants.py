"""Whitelist constants for the form domain (P07, FM-036/037/038).

Every frozenset/regex here is a publish-time whitelist consulted by both the
structural validators (:mod:`specs`, :mod:`conditions`, :mod:`validation`) and
the runtime evaluators (:mod:`conditions`, :mod:`calculated`, :mod:`values`),
so a shape rejected at draft time cannot surface at run time (Constitution
IX/XIV — no arbitrary code, only whitelisted operators/formulas).

Symbols: ``_MAX_CONDITION_DEPTH``, ``_ALLOWED_OPERATORS``, ``_FIELD_TYPES``,
``_SECTION_KINDS``, ``_CALCULATED_OPERATORS``, ``_ARITHMETIC_OPERATORS``,
``_SIGNATURE_KINDS``, ``_SYSTEM_PROPERTIES``, ``_FIELD_KEY_RE``,
``_CONNECTOR_RE``, ``_CODE_LIKE_KEYS``, ``_NUMERIC_FIELD_TYPES``,
``_TEXT_LIKE_FIELD_TYPES``.
"""

from __future__ import annotations

import re

_MAX_CONDITION_DEPTH: int = 6
_ALLOWED_OPERATORS: frozenset[str] = frozenset({"eq", "ne", "in", "exists", "truthy"})
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
        "attachment",
        "calculated",
        "lookup",
        "signature",
        "system",
    }
)
_SECTION_KINDS: frozenset[str] = frozenset({"section", "panel", "repeating"})
_CALCULATED_OPERATORS: frozenset[str] = frozenset(
    {"add", "sub", "mul", "div", "concat", "coalesce", "if_present"}
)
# Numeric subset of _CALCULATED_OPERATORS; these share one left-to-right evaluator.
_ARITHMETIC_OPERATORS: frozenset[str] = frozenset({"add", "sub", "mul", "div"})
_SIGNATURE_KINDS: frozenset[str] = frozenset({"handwritten", "typed", "external"})
_SYSTEM_PROPERTIES: frozenset[str] = frozenset(
    {
        "case.reference",
        "case.status",
        "case.created_at",
        "case.updated_at",
        "case.created_by",
        "actor.id",
        "actor.display_name",
        "tenant.id",
    }
)
_FIELD_KEY_RE: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONNECTOR_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?$")
_CODE_LIKE_KEYS: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "import",
        "lambda",
        "__import__",
        "__proto__",
        "__class__",
        "function",
        "javascript",
        "$where",
        "script",
        "code",
    }
)
_NUMERIC_FIELD_TYPES: frozenset[str] = frozenset({"integer", "decimal"})
_TEXT_LIKE_FIELD_TYPES: frozenset[str] = frozenset(
    {"text", "longtext", "enum", "multichoice", "date", "datetime"}
)

__all__ = [
    "_ALLOWED_OPERATORS",
    "_ARITHMETIC_OPERATORS",
    "_CALCULATED_OPERATORS",
    "_CODE_LIKE_KEYS",
    "_CONNECTOR_RE",
    "_FIELD_KEY_RE",
    "_FIELD_TYPES",
    "_MAX_CONDITION_DEPTH",
    "_NUMERIC_FIELD_TYPES",
    "_SECTION_KINDS",
    "_SIGNATURE_KINDS",
    "_SYSTEM_PROPERTIES",
    "_TEXT_LIKE_FIELD_TYPES",
    "re",
]
