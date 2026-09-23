"""Declarative guard document validation (Pillar P05 §2 / plan §16.3).

Guards are the ``{"op": ..., "field": ...}`` (or canonical
``{"all": [...]}``) documents attached to workflow transitions. This
module owns:

- the closed operator allowlist :data:`_ALLOWED_GUARD_OPERATORS` —
  the same set the case-type rule validator and the runtime evaluator
  (``domain/rules.py``) speak;
- the nesting-depth cap :data:`_MAX_GUARD_NESTING_DEPTH` so a workflow
  guard and a case-type rule have the same input-complexity bound;
- canonical→authoring shape conversion
  (:func:`_ensure_authoring_shape`, :func:`_to_authoring_shape`) so
  every structural check applies identically to both spellings
  (T067/T071 unification);
- the recursive shape walk :func:`_validate_guard_shape` — operator
  allowlist, clauses shape, and the P03-US5 document-requirements
  cross-check against ``known_classifications``.

Pure functions: no I/O. Split from the pre-R2 single-file validator
(``validate_workflow_definition.py``) by responsibility.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.domain.errors import ValidationFailed

# v1 declarative guard operators (Pillar P05 §2 / plan §16.3). Mirrors
# the case-type validator's operator set; the runtime evaluator
# (``domain/rules.py``) interprets them.
_ALLOWED_GUARD_OPERATORS: frozenset[str] = frozenset(
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

# Depth limit on nested guard composites. Matches the case-type
# validator so a workflow guard and a case-type rule have the same
# input complexity bound.
_MAX_GUARD_NESTING_DEPTH = 6


def _ensure_authoring_shape(guard: dict[str, Any], *, path: str) -> dict[str, Any]:
    """Return the guard in authoring shape, converting if necessary.

    Canonical single-key guards (``{"all": [...]}`` — the same
    language ``domain.rules.evaluate`` speaks) convert to authoring
    form so every structural check in :func:`_validate_guard_shape`
    (operator allowlist, clauses shape, classification cross-check)
    applies identically to both spellings (T067/T071 unification).

    Args:
        guard: the raw guard document from the workflow JSON.
        path: dotted authoring path used in ``ValidationFailed.field``.

    Returns:
        The guard with an ``op`` key (authoring shape).

    Raises:
        ValidationFailed: a canonical guard is malformed.
    """
    if "op" in guard:
        return guard
    return _to_authoring_shape(guard, path=path)


def _to_authoring_shape(guard: dict[str, Any], *, path: str) -> dict[str, Any]:
    """Convert a canonical single-key guard to authoring shape.

    ``{"all": [...]}`` becomes ``{"op": "all", "clauses": [...]}``;
    ``{"not": {...}}`` becomes ``{"op": "not", "clause": {...}}``;
    ``{"<leaf>": {...}}`` becomes ``{"op": "<leaf>", ...}`` and
    ``{"exists": "field"}`` becomes ``{"op": "exists", "field":
    "field"}``. Anything else raises — the caller reports the publish
    path so the author can locate the defect.

    Args:
        guard: a single-key canonical guard document.
        path: dotted authoring path used in ``ValidationFailed.field``.

    Returns:
        The equivalent authoring-shape guard.

    Raises:
        ValidationFailed: not exactly one key, unknown operator, or an
            operand that is neither a mapping nor a field string.
    """
    if len(guard) != 1:
        raise ValidationFailed(
            "guard must declare exactly one operator",
            field=path,
        )
    ((op, operand),) = guard.items()
    if op not in _ALLOWED_GUARD_OPERATORS:
        raise ValidationFailed(
            f"unknown guard operator {op!r}",
            field=f"{path}.op",
        )
    if op in {"all", "any"}:
        return {"op": op, "clauses": operand}
    if op == "not":
        return {"op": op, "clause": operand}
    if isinstance(operand, str):
        return {"op": op, "field": operand}
    if isinstance(operand, dict):
        return {"op": op, **operand}
    raise ValidationFailed(
        f"guard operator {op!r} requires a mapping or field-string operand",
        field=path,
    )


def _validate_guard_shape(
    guard: dict[str, Any],
    *,
    path: str,
    depth: int,
    known_classifications: Iterable[str] = (),
) -> None:
    """Recursively validate one guard document's shape.

    Checks (pure, no I/O): nesting depth cap, JSON-object shape,
    operator allowlist, composite clause structure, leaf ``field``
    reference, and the P03-US5 document-requirements cross-check.

    Args:
        guard: the guard document (authoring or canonical spelling).
        path: dotted authoring path for ``ValidationFailed.field``.
        depth: current recursion depth (0 at the top level).
        known_classifications: document classification labels the
            tenant has configured; an empty iterable skips the
            cross-check but the shape check still applies.

    Raises:
        ValidationFailed: the guard document is malformed.
    """
    if depth > _MAX_GUARD_NESTING_DEPTH:
        raise ValidationFailed(
            f"guard nesting exceeds depth {_MAX_GUARD_NESTING_DEPTH}",
            field=path,
        )
    if not isinstance(guard, dict):
        raise ValidationFailed(
            f"guard must be a JSON object (got {type(guard).__name__})",
            field=path,
        )
    guard = _ensure_authoring_shape(guard, path=path)
    op = guard.get("op")
    if op not in _ALLOWED_GUARD_OPERATORS:
        raise ValidationFailed(
            f"unknown guard operator {op!r}",
            field=f"{path}.op",
        )

    if op in {"all", "any"}:
        clauses = guard.get("clauses")
        if not isinstance(clauses, list) or not clauses:
            raise ValidationFailed(
                f"{op!r} requires a non-empty 'clauses' array",
                field=f"{path}.clauses",
            )
        for i, child in enumerate(clauses):
            _validate_guard_shape(
                child,
                path=f"{path}.clauses[{i}]",
                depth=depth + 1,
                known_classifications=known_classifications,
            )
        return
    if op == "not":
        inner = guard.get("clause")
        if not isinstance(inner, dict):
            raise ValidationFailed(
                "'not' requires an object 'clause'",
                field=f"{path}.clause",
            )
        _validate_guard_shape(
            inner,
            path=f"{path}.clause",
            depth=depth + 1,
            known_classifications=known_classifications,
        )
        return

    if "field" not in guard or not isinstance(guard["field"], str):
        raise ValidationFailed(
            f"leaf guard {op!r} requires a string 'field' reference",
            field=f"{path}.field",
        )

    # P03-US5: a guard that documents per-classification must be
    # well-formed at publish time so a typo (``"Contsract"``) does not
    # silently degrade to "no document required" at runtime.
    field_ref = guard["field"]
    if field_ref.startswith("case.documents_by_classification."):
        classification = guard.get("classification")
        if not isinstance(classification, str) or not classification.strip():
            raise ValidationFailed(
                "document requirements guard requires a non-empty string 'classification' entry",
                field=f"{path}.classification",
            )
        classification_value = classification.strip()
        known = frozenset(known_classifications)
        if known and classification_value not in known:
            raise ValidationFailed(
                f"document requirements guard references unknown "
                f"classification {classification_value!r}",
                field=f"{path}.classification",
            )
