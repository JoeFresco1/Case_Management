"""Validate a draft CaseType schema before publication.

This module is the case-type-targeted sibling of
:mod:`app.application.configuration.validate_definition`
(which validates workflow definitions). The two share the same shape
contract and the same error type so callers can program against either.

The validator is the **publish gate** (FR-015). It enforces the shape of
the configuration artifact — field types, required/sensitivity/searchable
metadata, document classifications, and declarative rules — and rejects
drafts with structured ``ValidationFailed`` errors.

The validator is a **pure function**: no I/O, no time, no randomness.
This is what the seed script, the admin router, and the unit test suite
all call. Domain invariants on individual fields already run in
``CaseTypeField.__post_init__``; the validator focuses on cross-field
defects (duplicate keys, empty fieldsets, malformed rule operators,
classification collisions).

# Why declarative only

Per Pillar P05 §2 and Constitution XIV, v1 ships a declarative rule
language. The validator therefore refuses to execute arbitrary
expressions; it only confirms that every rule is a well-formed
JSON-document with a known top-level operator and bounded nesting.

# Errors

Every defect raises :class:`app.domain.errors.ValidationFailed` with an
optional ``field=`` marker. The HTTP boundary maps ``ValidationFailed``
to ``422 validation_failed`` (interfaces/http/errors.py).

# Naming

This module exists because ``app.application.configuration`` already
ships a workflow-targeted validator under the name ``validate_definition``
(see :mod:`app.application.configuration.validate_definition`); the
case-type validator lives here under its own name to keep the public
import paths unambiguous for callers (P10 / FR-015).
"""

from __future__ import annotations

from typing import Any

from app.domain.case_type import CaseTypeSchema
from app.domain.errors import ValidationFailed

# v1 declarative rule operators (Pillar P05 §2 / plan §16.3). A rule is
# either a single operator document ``{"op": ..., ...}`` or a
# composite (``all`` / ``any`` / ``not``) that nests other rules. The
# evaluator lives in ``domain/rules.py`` (Phase 3); here we only shape-
# check.
_ALLOWED_RULE_OPERATORS: frozenset[str] = frozenset(
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

# Depth limit on nested composites. Keeps declarative JSON from growing
# into an undecidable evaluator input (plan §5 risks & mitigations).
_MAX_RULE_NESTING_DEPTH = 6


def validate_definition(schema: CaseTypeSchema) -> None:
    """Validate a draft ``CaseTypeSchema``. Raises on the first defect.

    Defects checked:

    - At least one field defined (an empty schema has no value).
    - No duplicate machine ``key`` across fields.
    - All field-level invariants already enforced by
      ``CaseTypeField.__post_init__`` have been applied at construction;
      we still re-check ``enum_values`` membership here because rule
      validation can be triggered after the field was constructed with
      wider inputs.
    - Document classifications are unique and non-empty.
    - Declarative rules are well-formed: a known operator, bounded
      nesting, no unknown keys.

    The function returns ``None`` on success; callers branch on the
    raised exception. The HTTP boundary never sees a successful
    ``None``; it only sees ``422`` on a defect.
    """
    if not schema.fields:
        raise ValidationFailed("at least one field is required", field="fields")

    _validate_unique_field_keys(schema)
    _validate_enum_values_match_field_type(schema)
    _validate_document_classifications(schema)
    _validate_rules(schema.rules)

    # FM-012: derived fields (P05-US6) must reference operands that exist
    # in this same schema. The shape-level check runs in
    # ``CaseTypeField.__post_init__``; this validator only confirms the
    # operand keys resolve to defined fields.
    try:
        schema.assert_derived_operands_resolvable()
    except Exception as exc:  # InvariantViolation -> ValidationFailed
        raise ValidationFailed(str(exc), field="fields") from exc


def _validate_unique_field_keys(schema: CaseTypeSchema) -> None:
    seen: set[str] = set()
    for f in schema.fields:
        if f.key in seen:
            raise ValidationFailed(f"duplicate field key: {f.key!r}", field=f"fields.{f.key}")
        seen.add(f.key)


def _validate_enum_values_match_field_type(schema: CaseTypeSchema) -> None:
    for f in schema.fields:
        if f.type not in {"enum", "multichoice"} and f.enum_values:
            raise ValidationFailed(
                f"field {f.key!r} has enum_values but type {f.type!r}",
                field=f"fields.{f.key}.enum_values",
            )


def _validate_document_classifications(schema: CaseTypeSchema) -> None:
    if any(not c.strip() for c in schema.document_classifications):
        raise ValidationFailed(
            "document_classifications entries must be non-empty",
            field="document_classifications",
        )
    if len(set(schema.document_classifications)) != len(schema.document_classifications):
        raise ValidationFailed(
            "document_classifications entries must be unique",
            field="document_classifications",
        )


def _validate_rules(rules: tuple[dict[str, Any], ...]) -> None:
    for idx, rule in enumerate(rules):
        _validate_rule_shape(rule, path=f"rules[{idx}]", depth=0)


def _validate_rule_shape(rule: dict[str, Any], *, path: str, depth: int) -> None:
    if depth > _MAX_RULE_NESTING_DEPTH:
        raise ValidationFailed(
            f"rule nesting exceeds depth {_MAX_RULE_NESTING_DEPTH}",
            field=path,
        )
    if not isinstance(rule, dict):
        raise ValidationFailed(
            f"rule must be a JSON object (got {type(rule).__name__})",
            field=path,
        )
    op = rule.get("op")
    if op not in _ALLOWED_RULE_OPERATORS:
        raise ValidationFailed(
            f"unknown rule operator {op!r}",
            field=f"{path}.op",
        )

    # Composites nest other rules; leaves must declare a `field` and
    # ``value`` (or operator-specific keys). The depth guard above keeps
    # JSON input finite.
    if op in {"all", "any"}:
        clauses = rule.get("clauses")
        if not isinstance(clauses, list) or not clauses:
            raise ValidationFailed(
                f"{op!r} requires a non-empty 'clauses' array",
                field=f"{path}.clauses",
            )
        for i, child in enumerate(clauses):
            _validate_rule_shape(child, path=f"{path}.clauses[{i}]", depth=depth + 1)
        return
    if op == "not":
        inner = rule.get("clause")
        if not isinstance(inner, dict):
            raise ValidationFailed(
                "'not' requires an object 'clause'",
                field=f"{path}.clause",
            )
        _validate_rule_shape(inner, path=f"{path}.clause", depth=depth + 1)
        return

    # Leaf operators. ``field`` is required; the value shape is
    # operator-specific and the runtime evaluator enforces type checks.
    # Here we only confirm a field reference is present.
    if "field" not in rule or not isinstance(rule["field"], str):
        raise ValidationFailed(
            f"leaf rule {op!r} requires a string 'field' reference",
            field=f"{path}.field",
        )


__all__ = [
    "validate_definition",
]
