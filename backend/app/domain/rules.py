"""Pure-function rule evaluator (P05 §2 / P02 / T067).

``evaluate(condition, context) -> bool`` interprets the declarative JSON
guard documents the workflow engine and case-type rules consume. The
same operator set is used by:

- CaseType field rules (``schema_json.rules``, FM-012 / P05-US6).
- Workflow transition guards (``WorkflowTransition.guard``, FR-009 /
  FM-014 / P02-US1).

The evaluator is **pure**: no I/O, no time, no randomness, no module
state. Determinism is a Constitutional requirement (Constitution XIV —
the deterministic core before AI) so two replays of the same
``(condition, context)`` always return the same result.

# Operator catalog (T067)

Logical / quantifier:

- ``all`` — every child condition must hold. ``{"all": [...]}``
- ``any`` — at least one child condition must hold. ``{"any": [...]}``
- ``not`` — the single child must not hold. ``{"not": {...}}``

Comparison / membership:

- ``eq`` — ``field == value``
- ``ne`` — ``field != value``
- ``in`` — ``value in field`` (``field`` must be a collection)
- ``gt`` / ``gte`` / ``lt`` / ``lte`` — numeric ordering

Existence / shape:

- ``exists`` — ``field`` is present and non-empty (non-None, non-empty
  collection). ``{"exists": "field"}`` is the canonical form;
  ``{"exists": {"field": "f", "min": 1}}`` extends with a minimum count.
- ``contains`` — ``field`` contains the literal ``value``. ``field``
  must be a collection or string.
- ``regex`` — ``field`` matches a regex pattern. ``{"regex":
  {"field": "f", "pattern": "..."}}``.

# Depth limit

The validator at publish time rejects conditions nested beyond
``_MAX_NESTING_DEPTH`` levels (P05 / P02 plan). The runtime evaluator
also enforces the same cap so a malformed runtime JSON cannot bypass
the publish-time check.

# Failure mode

A structurally invalid condition (``{"unknown": ...}``, wrong-typed
arguments, missing ``field``) raises :class:`RuleEvaluationError`. The
use case maps this to :class:`ValidationFailed` so the HTTP boundary
returns 422. A condition that evaluates against a missing
``context["field"]`` follows the ``exists`` semantic: missing fields
are ``None`` for ordering and equality, and ``False`` for ``exists``.

# Two accepted shapes (T067 / T071 unification)

The canonical shape is single-key ``{operator: operand}``
(``{"all": [...]}``, ``{"eq": {"field": "f", "value": 1}}``). The
workflow authoring shape (``{"op": "all", "clauses": [...]}``,
``{"op": "eq", "field": "f", "value": 1}``) is accepted as well and
normalized before evaluation, so the publish-time validator, the
simulator, the seed blueprints, and this evaluator all speak the same
language. Normalization is shallow-structure only (no I/O, no time);
unknown operators still raise :class:`RuleEvaluationError`
fail-closed in either shape.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from app.domain.errors import ValidationFailed

# Mirrors the validator's operator set so a runtime evaluator cannot
# accept anything the publish-time validator would reject.
ALLOWED_OPERATORS: Final[frozenset[str]] = frozenset(
    {
        "all",
        "any",
        "not",
        "eq",
        "ne",
        "in",
        "gt",
        "gte",
        "lt",
        "lte",
        "exists",
        "contains",
        "regex",
    }
)

# Same depth bound the publish-time validator uses
# (``configuration/validate_definition.py`` /
# ``configuration/validate_case_type_definition.py``). The runtime
# evaluator enforces it as a defense-in-depth so a hand-crafted
# condition cannot stack arbitrarily deep at evaluation time.
_MAX_NESTING_DEPTH: Final[int] = 6

# Regex cache for ``regex`` operator. Conditions are declarative JSON;
# the same pattern is evaluated against many contexts, so memoizing
# compiled patterns is a worthwhile optimization.
_PATTERN_CACHE: Final[dict[str, re.Pattern[str]]] = {}


class RuleEvaluationError(ValidationFailed):
    """A condition is structurally invalid at evaluation time.

    Inherits from :class:`ValidationFailed` so the HTTP boundary maps
    it to 422 ``validation_failed`` automatically (errors.py). The
    rule validator is the publish-time guard; this error is the
    runtime guarantee that a malformed runtime condition cannot escape
    with a silent ``False``.
    """


def normalize_condition(condition: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize an authoring-shape condition to canonical single-key form.

    Canonical ``{"all": [...]}`` passes through unchanged. Authoring
    ``{"op": "all", "clauses": [...]}`` becomes ``{"all": [...]}``
    (recursively); ``{"op": "not", "clause": {...}}`` becomes
    ``{"not": {...}}``; ``{"op": "<leaf>", "field": "f", ...}`` becomes
    ``{"<leaf>": {"field": "f", ...}}`` (the ``op``/``field`` keys are
    lifted out, the rest of the mapping is the operand verbatim).

    Raises :class:`RuleEvaluationError` for non-mappings, empty
    mappings, and unknown operators — fail-closed in either shape.
    Depth is NOT enforced here; :func:`evaluate` applies
    ``_MAX_NESTING_DEPTH`` as it recurses.
    """
    if not isinstance(condition, Mapping) or not condition:
        raise RuleEvaluationError(
            "rule condition must be a non-empty mapping",
            field="<condition>",
        )
    if "op" not in condition:
        # Canonical shape (or garbage): recurse the known composite
        # positions so mixed nesting (canonical parent, authoring
        # child) normalizes totally; unknown operators are rejected
        # by the dispatch below. Leaf operands are left verbatim — a
        # ``value`` payload that happens to look like a condition is
        # data, not syntax.
        ((only_op, only_operand),) = (
            list(condition.items()) if len(condition) == 1 else ((None, None),)
        )
        if only_op in {"all", "any"} and isinstance(only_operand, list):
            return {only_op: [normalize_condition(c) for c in only_operand]}
        if only_op == "not" and isinstance(only_operand, Mapping):
            return {only_op: normalize_condition(only_operand)}
        return dict(condition)
    op = condition.get("op")
    if op not in ALLOWED_OPERATORS or not isinstance(op, str):
        raise RuleEvaluationError(
            f"unknown rule operator: {op!r}",
            field="<condition>",
        )
    if op in {"all", "any"}:
        clauses = condition.get("clauses")
        if not isinstance(clauses, list):
            raise RuleEvaluationError(
                f"{op!r} requires a 'clauses' array",
                field=op,
            )
        return {op: [normalize_condition(child) for child in clauses]}
    if op == "not":
        clause = condition.get("clause")
        if not isinstance(clause, Mapping):
            raise RuleEvaluationError(
                "'not' requires a 'clause' mapping",
                field=op,
            )
        return {op: normalize_condition(clause)}
    operand = {k: v for k, v in condition.items() if k != "op"}
    return {op: operand}


def evaluate(  # noqa: PLR0911, PLR0912 - operator dispatch is intentionally flat
    condition: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    _depth: int = 0,
) -> bool:
    """Evaluate a declarative condition against a context dict.

    Args:
        condition: the JSON condition document, canonical single-key
            (``{"all": [...]}``) or authoring shape (``{"op": "all",
            "clauses": [...]}`` — normalized first; see
            :func:`normalize_condition`). Composite operators (``all``,
            ``any``, ``not``) take a nested condition list / dict.
        context: the field map to evaluate against. Typically
            ``{"case": {...}, "case.documents": [...], ...}`` — the
            field paths used by the workflow author.

    Returns:
        ``True`` iff the condition holds.

    Raises:
        RuleEvaluationError: the condition is malformed (unknown
            operator, missing ``field`` on a leaf operator, wrong
            operand type, nesting beyond ``_MAX_NESTING_DEPTH``).
    """
    if _depth > _MAX_NESTING_DEPTH:
        raise RuleEvaluationError(
            f"rule nesting exceeds depth {_MAX_NESTING_DEPTH}",
            field="<condition>",
        )
    condition = normalize_condition(condition)
    if len(condition) != 1:
        raise RuleEvaluationError(
            "rule condition must declare exactly one operator",
            field="<condition>",
        )
    ((op, operand),) = condition.items()
    if op not in ALLOWED_OPERATORS:
        raise RuleEvaluationError(
            f"unknown rule operator: {op!r}",
            field="<condition>",
        )

    if op == "all":
        return _all(operand, context, _depth)
    if op == "any":
        return _any(operand, context, _depth)
    if op == "not":
        return not _not_operand(operand, context, _depth)
    # Leaf operators — every one of them needs a ``field`` key.
    field, min_count, pattern = _leaf_shape(op, operand)
    value = _resolve(field, context)
    if op == "exists":
        return _exists(value, min_count)
    if op == "eq":
        return bool(value == operand["value"])
    if op == "ne":
        return bool(value != operand["value"])
    if op == "in":
        if not isinstance(value, (list, tuple, set, str)):
            raise RuleEvaluationError(
                f"'in' field {field!r} must be a collection or string",
                field=field,
            )
        return operand["value"] in value
    if op in {"gt", "gte", "lt", "lte"}:
        return _ordering(op, field, value, operand["value"])
    if op == "contains":
        return _contains(field, value, operand["value"])
    if op == "regex":
        return _regex(field, value, pattern)
    # Defensive: an operator slipped through the whitelist.
    raise RuleEvaluationError(f"unhandled rule operator: {op!r}", field=op)


# ---------------------------------------------------------------------------
# Composite operators
# ---------------------------------------------------------------------------


def _all(operand: Any, context: Mapping[str, Any], depth: int) -> bool:
    if not isinstance(operand, list) or not operand:
        raise RuleEvaluationError(
            "'all' requires a non-empty list of conditions",
            field="all",
        )
    return all(evaluate(c, context, _depth=depth + 1) for c in operand)


def _any(operand: Any, context: Mapping[str, Any], depth: int) -> bool:
    if not isinstance(operand, list) or not operand:
        raise RuleEvaluationError(
            "'any' requires a non-empty list of conditions",
            field="any",
        )
    return any(evaluate(c, context, _depth=depth + 1) for c in operand)


def _not_operand(operand: Any, context: Mapping[str, Any], depth: int) -> bool:
    if not isinstance(operand, Mapping):
        raise RuleEvaluationError("'not' requires a single nested condition", field="not")
    return evaluate(operand, context, _depth=depth + 1)


# ---------------------------------------------------------------------------
# Leaf helpers
# ---------------------------------------------------------------------------


def _leaf_shape(op: str, operand: Any) -> tuple[str, int | None, str | None]:
    """Validate a leaf operator's shape and return ``(field, min, pattern)``.

    ``min`` and ``pattern`` are populated only by ``exists`` and
    ``regex`` respectively; the other operators leave them as
    sentinels so the call site can ignore them.
    """
    # ``exists`` accepts both ``"exists": "field"`` and
    # ``"exists": {"field": "f", "min": N}`` — the string form is a
    # shortcut for the canonical mapping. Handle the string form first
    # so a bare-string operand never reaches the mapping check below.
    if op == "exists" and isinstance(operand, str):
        return operand, None, None
    if not isinstance(operand, Mapping):
        raise RuleEvaluationError(f"leaf operator {op!r} requires a mapping operand", field=op)
    # ``exists`` accepts both the bare-string form above and the
    # canonical mapping form below.
    if op == "exists":
        field = operand.get("field")
        if not field or not isinstance(field, str):
            raise RuleEvaluationError("'exists' requires a 'field' string", field="exists")
        min_count = operand.get("min")
        if min_count is not None and (
            not isinstance(min_count, int) or isinstance(min_count, bool) or min_count < 1
        ):
            raise RuleEvaluationError("'exists.min' must be a positive integer", field=field)
        return field, min_count, None
    # ``regex`` is always a mapping with ``field`` and ``pattern``.
    if op == "regex":
        field = operand.get("field")
        pattern = operand.get("pattern")
        if not field or not isinstance(field, str):
            raise RuleEvaluationError("'regex' requires a 'field' string", field="regex")
        if not pattern or not isinstance(pattern, str):
            raise RuleEvaluationError("'regex' requires a 'pattern' string", field=field)
        return field, None, pattern
    # All other leaf operators share the same shape: ``{"field": "f",
    # "value": v}``.
    field = operand.get("field")
    if not field or not isinstance(field, str):
        raise RuleEvaluationError(f"operator {op!r} requires a 'field' string", field=op)
    if "value" not in operand:
        raise RuleEvaluationError(f"operator {op!r} requires a 'value' entry", field=field)
    return field, None, None


def _resolve(field: str, context: Mapping[str, Any]) -> Any:
    """Look up ``field`` in ``context``.

    Field paths use dotted notation (``case.documents``,
    ``case.priority``). A missing key returns ``None`` — leaves are
    free to translate that into ``False`` (``exists``, ``contains``)
    or a comparison against ``None`` (``eq``/``ne``). The guard
    function never raises for a missing field.
    """
    cursor: Any = context
    for part in field.split("."):
        if not isinstance(cursor, Mapping):
            return None
        cursor = cursor.get(part)
        if cursor is None:
            return None
    return cursor


def _exists(value: Any, min_count: int | None) -> bool:
    if value is None:
        return False
    if min_count is None:
        return value not in (None, "", [], {}, ())
    if isinstance(value, (list, tuple, set, str)):
        return len(value) >= min_count
    # A scalar with ``min`` is nonsensical; treat the rule as not
    # holding rather than raising so a misconfigured guard never blocks
    # a transition silently. The publish-time validator should reject
    # this shape; the runtime guard fails closed to a safe ``False``.
    return False


def _ordering(op: str, field: str, lhs: Any, rhs: Any) -> bool:
    # ``None`` never orders meaningfully; fail closed so a missing
    # field cannot accidentally satisfy a guard.
    if lhs is None or rhs is None:
        return False
    if isinstance(lhs, bool) or isinstance(rhs, bool):
        raise RuleEvaluationError(
            f"operator {op!r} does not accept boolean operands",
            field=field,
        )
    try:
        if op == "gt":
            return bool(lhs > rhs)
        if op == "gte":
            return bool(lhs >= rhs)
        if op == "lt":
            return bool(lhs < rhs)
        return bool(lhs <= rhs)
    except TypeError as exc:
        raise RuleEvaluationError(
            f"operator {op!r} operands are not comparable", field=field
        ) from exc


def _contains(field: str, value: Any, needle: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple, set, str)):
        return needle in value
    raise RuleEvaluationError(
        f"'contains' field {field!r} must be a collection or string",
        field=field,
    )


def _regex(field: str, value: Any, pattern: str | None) -> bool:
    if value is None:
        return False
    if not isinstance(value, str):
        raise RuleEvaluationError(f"'regex' field {field!r} must be a string", field=field)
    if pattern is None:
        # The leaf-shape validator already rejects a missing pattern;
        # this branch is defensive only.
        return False
    compiled = _PATTERN_CACHE.get(pattern)
    if compiled is None:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise RuleEvaluationError(
                f"invalid regex pattern for {field!r}: {exc}",
                field=field,
            ) from exc
        _PATTERN_CACHE[pattern] = compiled
    return compiled.search(value) is not None


__all__ = [
    "ALLOWED_OPERATORS",
    "RuleEvaluationError",
    "evaluate",
    "normalize_condition",
]
