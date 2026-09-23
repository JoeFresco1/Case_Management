"""Draft/publish-time definition validation orchestrator (P07, FR-015).

:func:`_validate_definition` is the single entry point called by
``FormVersion.draft``: it indexes sections/fields once, then runs each
independent rule group (duplicate keys/tab order, visibility references,
visibility cycles, validation rules, steps, calculated references) so no
single check exceeds the complexity ceiling (R2).

Symbols: ``_validate_definition``, ``_index_definition``,
``_reject_duplicate_tab_order``, ``_check_section_visibility_refs``,
``_check_calculated_refs``, ``_check_validation_rule``, ``_validate_steps``,
``_reject_section_visibility_cycle``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.domain.errors import InvariantViolation

from .conditions import _check_condition, _collect_referenced
from .constants import _FIELD_KEY_RE
from .helpers import _formula_operands, _reject_code_like_key
from .specs import FormDefinition, FormFieldSpec


def _validate_definition(definition: FormDefinition) -> None:
    """Draft/publish-time definition check; raises InvariantViolation on any defect.

    Orchestrates the independent rule groups (indexing, tab order, visibility,
    cycles, validations, steps, calculated refs) so each check stays small.
    """
    section_keys, seen_fields, field_by_key, section_of_field = _index_definition(definition)
    _reject_duplicate_tab_order(definition)
    _check_section_visibility_refs(definition, seen_fields, section_of_field)
    _reject_section_visibility_cycle(definition, section_of_field)
    for rule in definition.validations:
        _check_validation_rule(rule, field_keys=seen_fields)
    _validate_steps(definition, section_keys=section_keys)
    _check_calculated_refs(field_by_key, seen_fields)


def _index_definition(
    definition: FormDefinition,
) -> tuple[set[str], set[str], dict[str, FormFieldSpec], dict[str, str]]:
    """Index sections and fields; rejects duplicate keys and bad visibility conditions.

    Returns ``(section_keys, field_keys, field_by_key, section_of_field)`` for the
    later rule groups to reuse without re-walking the definition.
    """
    section_keys: set[str] = set()
    seen_fields: set[str] = set()
    field_by_key: dict[str, FormFieldSpec] = {}
    section_of_field: dict[str, str] = {}
    for section in definition.sections:
        if not section.key or section.key in section_keys:
            raise InvariantViolation(f"bad/duplicate section key {section.key!r}")
        section_keys.add(section.key)
        if section.visible_when is not None:
            _check_condition(section.visible_when)
        for field in section.fields:
            if not field.key or field.key in seen_fields:
                raise InvariantViolation(f"bad/duplicate field key {field.key!r}")
            seen_fields.add(field.key)
            field_by_key[field.key] = field
            section_of_field[field.key] = section.key
    return section_keys, seen_fields, field_by_key, section_of_field


def _reject_duplicate_tab_order(definition: FormDefinition) -> None:
    """Reject two fields sharing one ``tab_order``; raises InvariantViolation."""
    seen_tabs: dict[int, str] = {}
    for section in definition.sections:
        for field in section.fields:
            if field.tab_order is None:
                continue
            if field.tab_order in seen_tabs:
                raise InvariantViolation(
                    f"duplicate tab_order {field.tab_order} on "
                    f"{seen_tabs[field.tab_order]!r} and {field.key!r}"
                )
            seen_tabs[field.tab_order] = field.key


def _check_section_visibility_refs(
    definition: FormDefinition,
    seen_fields: set[str],
    section_of_field: dict[str, str],
) -> None:
    """Every ``visible_when`` reference must exist and not be its own section's field."""
    for section in definition.sections:
        if section.visible_when is None:
            continue
        refs: set[str] = set()
        _collect_referenced(section.visible_when, refs)
        for ref in refs:
            if ref not in seen_fields:
                raise InvariantViolation(f"section {section.key!r} refs unknown {ref!r}")
            if section_of_field.get(ref) == section.key:
                raise InvariantViolation(
                    f"ambiguous: section {section.key!r} refs own field {ref!r}"
                )


def _check_calculated_refs(field_by_key: dict[str, FormFieldSpec], seen_fields: set[str]) -> None:
    """Every calculated operand/fallback must name a defined field key."""
    for key, field in field_by_key.items():
        if field.formula is None:
            continue
        for operand in _formula_operands(field.formula):
            if operand not in seen_fields:
                raise InvariantViolation(f"calculated {key!r} refs unknown {operand!r}")
        fb = field.formula.get("fallback") if isinstance(field.formula, Mapping) else None
        if isinstance(fb, str) and fb not in seen_fields:
            raise InvariantViolation(f"calculated {key!r} refs unknown fallback {fb!r}")


def _check_validation_rule(rule: Mapping[str, Any], *, field_keys: set[str]) -> None:
    """Validate ``{when, then:{required, message?}}``; rejects code/dangling refs."""
    if not isinstance(rule, Mapping):
        raise InvariantViolation("validation must be mapping")
    for k in rule:
        if isinstance(k, str):
            _reject_code_like_key(k, field_key="<validation>")
    if set(rule.keys()) != {"when", "then"}:
        raise InvariantViolation("validation needs exactly when/then")
    when = rule.get("when")
    then = rule.get("then")
    if not isinstance(when, Mapping):
        raise InvariantViolation("validation when must be mapping")
    _check_condition(when)
    if not isinstance(then, Mapping):
        raise InvariantViolation("validation then must be mapping")
    for k in then:
        if isinstance(k, str):
            _reject_code_like_key(k, field_key="<validation>")
    if set(then.keys()) - {"required", "message"}:
        raise InvariantViolation("then allows only required/message")
    req = then.get("required")
    if not isinstance(req, str) or not _FIELD_KEY_RE.match(req) or req not in field_keys:
        raise InvariantViolation(f"then.required refs unknown {req!r}")
    msg = then.get("message")
    if msg is not None and (not isinstance(msg, str) or not msg.strip()):
        raise InvariantViolation("then.message must be non-empty")


def _validate_steps(definition: FormDefinition, *, section_keys: set[str]) -> None:
    """Validate multi-step ``steps`` entries; raises InvariantViolation on defect."""
    seen: set[str] = set()
    for entry in definition.steps:
        if not isinstance(entry, Mapping):
            raise InvariantViolation("step must be mapping")
        for k in entry:
            if isinstance(k, str):
                _reject_code_like_key(k, field_key="<step>")
        raw = entry.get("step_key") or entry.get("key") or ""
        if not isinstance(raw, str) or not _FIELD_KEY_RE.match(raw) or raw in seen:
            raise InvariantViolation(f"bad/duplicate step key {raw!r}")
        seen.add(raw)
        raw_sections = entry.get("section_keys") or ()
        if not isinstance(raw_sections, (list, tuple)):
            raise InvariantViolation(f"step {raw!r} section_keys must be list")
        for skey in raw_sections:
            if not isinstance(skey, str) or skey not in section_keys:
                raise InvariantViolation(f"step {raw!r} refs unknown section {skey!r}")


def _reject_section_visibility_cycle(
    definition: FormDefinition, section_of_field: dict[str, str]
) -> None:
    """Reject S->T visibility cycles; raises InvariantViolation on cycle."""
    edges: dict[str, set[str]] = {s.key: set() for s in definition.sections}
    for section in definition.sections:
        if section.visible_when is None:
            continue
        refs: set[str] = set()
        _collect_referenced(section.visible_when, refs)
        for ref in refs:
            owner = section_of_field.get(ref)
            if owner is not None and owner != section.key:
                edges[section.key].add(owner)
    visited: set[str] = set()
    stack: set[str] = set()

    def _visit(node: str) -> None:
        visited.add(node)
        stack.add(node)
        for nxt in edges.get(node, set()):
            if nxt not in visited:
                _visit(nxt)
            elif nxt in stack:
                raise InvariantViolation(f"ambiguous visibility cycle at {sorted(stack)}")
        stack.discard(node)

    for node in edges:
        if node not in visited:
            _visit(node)


__all__ = [
    "InvariantViolation",
    "_check_calculated_refs",
    "_check_section_visibility_refs",
    "_check_validation_rule",
    "_index_definition",
    "_reject_duplicate_tab_order",
    "_reject_section_visibility_cycle",
    "_validate_definition",
    "_validate_steps",
]
