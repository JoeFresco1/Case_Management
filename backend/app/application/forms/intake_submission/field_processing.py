"""Deterministic field stages shared by intake and in-case resubmit.

Pure functions over a :class:`~app.domain.form.FormDefinition` plus a
data dict — no database I/O. The only awaited seams are the optional
:class:`~.ports.LookupProvider` and :class:`~.ports.SignatureConnector`
ports, which never touch persistence either.

Stages, in the order ``handle`` (and the in-case
:mod:`app.application.forms.submit_form` path) run them:

1. **Visibility keys** — :func:`hidden_field_keys` /
   :func:`visible_field_keys` derive which field keys live in hidden vs
   visible sections; :func:`field_map` indexes every field by key.
2. **Lookup pre-population** — :func:`apply_lookup_prepopulation` fills
   blank ``lookup`` fields from the configured source before validation.
3. **Calculated fields** — :func:`apply_calculated_fields` evaluates
   formulas to a fixed point; caller-supplied values never win.
4. **System fields** — :func:`apply_system_fields` fills ``system``
   properties from the intake context (reference, actor, tenant).
5. **Signature seam** — :func:`check_signature_fields` fails closed for
   ``external`` signatures without a wired connector.
6. **Attachment routing** — :func:`route_attachment_references`
   normalizes attachment values to ``{"document_id": ...}``.
7. **Deterministic validation** — :func:`validate_visible_fields_deterministic`
   collects every offender into one ``ValidationFailed`` naming each
   field so the UI can highlight problems without losing entered data.

Both paths reuse these stages so intake and resubmit validate
identically (P07-US3 / T3-US1).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from app.domain.errors import InvariantViolation, ValidationFailed
from app.domain.form import (
    FormDefinition,
    _evaluate_visibility,
    evaluate_calculated,
    read_only_field_keys,
    validate_field_value,
)

from .ports import LookupProvider, SignatureConnector


def hidden_field_keys(
    definition: FormDefinition, visible_sections: frozenset[str]
) -> frozenset[str]:
    """Return every field key living in a hidden section."""
    hidden: set[str] = set()
    for section in definition.sections:
        if section.key in visible_sections:
            continue
        for field in section.fields:
            hidden.add(field.key)
    return frozenset(hidden)


def field_map(definition: FormDefinition) -> dict[str, Any]:
    """Map field key -> spec for the whole definition."""
    out: dict[str, Any] = {}
    for section in definition.sections:
        for field in section.fields:
            out[field.key] = field
    return out


def visible_field_keys(
    definition: FormDefinition, visible_sections: frozenset[str]
) -> frozenset[str]:
    """Return every field key living in a visible section."""
    visible: set[str] = set()
    for section in definition.sections:
        if section.key not in visible_sections:
            continue
        for field in section.fields:
            visible.add(field.key)
    return frozenset(visible)


def validate_visible_fields_deterministic(  # noqa: PLR0912 -- staged checks by design
    *,
    definition: FormDefinition,
    visible_sections: frozenset[str],
    merged_data: Mapping[str, Any],
) -> None:
    """Validate visible fields deterministically, naming every offender.

    Checks, in order: (a) visible required fields present; (b) per-field
    type/range/pattern/enum via :func:`validate_field_value` for visible,
    non-read-only fields holding a non-blank value; (c) form-level
    ``when -> then: required`` rules. All failures are collected into one
    ``ValidationFailed`` whose ``field`` is the comma-joined sorted key
    set, so the UI highlights every problem *without losing entered
    data* (the caller retains ``submitted_data``; nothing was written).
    Read-only/system/calculated keys are never validated as inbound.
    """
    problems: dict[str, str] = {}
    read_only = read_only_field_keys(definition)
    fields = field_map(definition)
    visible = visible_field_keys(definition, visible_sections)
    # (a) visible required.
    for section in definition.sections:
        if section.key not in visible_sections:
            continue
        for spec in section.fields:
            if not spec.required:
                continue
            if _is_blank(merged_data.get(spec.key)):
                problems[spec.key] = f"required field {spec.key!r} is missing"
    # (b) per-field shape for supplied visible values.
    for key in sorted(visible):
        field_spec = fields.get(key)
        if field_spec is None or key in read_only:
            continue
        value = merged_data.get(key)
        if _is_blank(value):
            continue
        # why: attachment/signature/lookup shapes are checked by their
        # dedicated stages (routing / seam), not by the generic
        # validator — but a wrong *type* (e.g. int for text) still
        # fails here deterministically.
        if field_spec.field_type in {"attachment", "signature", "lookup"} and isinstance(
            value, Mapping
        ):
            if (
                field_spec.field_type == "attachment"
                and not str(value.get("document_id") or "").strip()
            ):
                problems[key] = field_spec.validation_message or f"invalid value for {key!r}"
            continue
        try:
            validate_field_value(field_spec, value)
        except ValidationFailed as exc:
            problems[key] = str(exc)
    # (c) form-level rules.
    for rule in definition.validations:
        when = rule.get("when") if isinstance(rule, Mapping) else None
        if when is None:
            continue
        if not _evaluate_visibility(when, merged_data):
            continue
        then = rule.get("then") if isinstance(rule, Mapping) else None
        if not isinstance(then, Mapping):
            continue
        required_field = then.get("required")
        if not isinstance(required_field, str):
            continue
        if _is_blank(merged_data.get(required_field)):
            problems[required_field] = (
                f"form rule requires {required_field!r} when the condition holds"
            )
    if problems:
        ordered = sorted(problems)
        raise ValidationFailed(
            "form validation failed: " + "; ".join(f"{k}: {problems[k]}" for k in ordered),
            field=",".join(ordered),
        )


def apply_calculated_fields(
    definition: FormDefinition, data: Mapping[str, Any]
) -> tuple[dict[str, Any], frozenset[str]]:
    """Evaluate every ``calculated`` field deterministically.

    Iterates to a fixed point (bounded by calculated-field count + 1) so
    chained formulas (total -> grand_total) resolve in dependency order
    regardless of declaration order. Caller-supplied values for
    calculated keys are dropped: the formula owns the value. Returns the
    new data dict plus the set of calculated keys that were (re)computed.
    """
    fields = field_map(definition)
    calculated = [k for k, s in fields.items() if s.field_type == "calculated"]
    if not calculated:
        return dict(data), frozenset()
    result: dict[str, Any] = dict(data)
    for key in calculated:
        result.pop(key, None)
    computed: set[str] = set()
    # why: bounded fixed-point — each pass may resolve one more chain
    # level; +1 catches the stable state without looping forever.
    for _ in range(len(calculated) + 1):
        changed = False
        for key in calculated:
            spec = fields[key]
            assert spec.formula is not None
            try:
                value = evaluate_calculated(spec.formula, result)
            except InvariantViolation:
                # why: missing numeric operand on a partial intake
                # yields None (same contract as case derived fields);
                # genuine shape errors were rejected at publish time.
                value = None
            if result.get(key) != value:
                result[key] = value
                changed = True
        if not changed:
            break
        computed.update(calculated)
    else:
        computed.update(calculated)
    return result, frozenset(computed | set(calculated))


def apply_system_fields(
    definition: FormDefinition,
    data: Mapping[str, Any],
    *,
    system_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Fill ``system`` fields from the intake context (never from input).

    Unknown ``system_property`` names were rejected at publish time; an
    absent context value leaves the field unset (required-validation
    then decides, since system fields are always optional per domain).
    """
    fields = field_map(definition)
    result: dict[str, Any] = dict(data)
    for key, spec in fields.items():
        if spec.field_type != "system":
            continue
        prop = spec.system_property or ""
        if prop in system_context:
            result[key] = system_context[prop]
    return result


async def apply_lookup_prepopulation(
    definition: FormDefinition,
    data: Mapping[str, Any],
    *,
    tenant_id: UUID,
    lookup: LookupProvider | None,
) -> dict[str, Any]:
    """Fill blank ``lookup`` fields from the configured source.

    Deterministic: the same (source, query) pair always yields the same
    value; the provider owns caching/freshness. A ``None`` provider
    leaves blanks untouched (required-validation then fails closed with
    the field named). ``DependencyUnavailable`` from the provider
    propagates so the intake rolls back instead of storing a half-filled
    case.
    """
    fields = field_map(definition)
    lookups = [k for k, s in fields.items() if s.field_type == "lookup"]
    if not lookups:
        return dict(data)
    result: dict[str, Any] = dict(data)
    for key in lookups:
        if not _is_blank(result.get(key)):
            continue
        spec = fields[key]
        source = spec.lookup_source or ""
        if lookup is None:
            continue
        # why: ValidationFailed (misconfigured source) and
        # DependencyUnavailable (source down) both propagate — the
        # former names the field, the latter rolls the intake back.
        value = await lookup.resolve(tenant_id, source, data)
        if value is not None and str(value).strip():
            result[key] = value
    return result


async def check_signature_fields(
    definition: FormDefinition,
    data: Mapping[str, Any],
    *,
    tenant_id: UUID,
    connector: SignatureConnector | None,
) -> None:
    """Enforce the signature seam (fails closed without a connector).

    ``handwritten`` / ``typed`` kinds are non-blank strings (already
    covered by required + ``validate_field_value``); this stage only
    handles ``external``: without a wired ``SignatureConnector`` the
    submission is rejected naming the field (never silently downgraded
    to a typed mark). A connector ``False`` verdict rejects the same
    way; ``DependencyUnavailable`` propagates for rollback.
    """
    fields = field_map(definition)
    for key, spec in fields.items():
        if spec.field_type != "signature" or spec.signature_kind != "external":
            continue
        if _is_blank(data.get(key)):
            continue  # why: blank + required is the validator's job.
        if connector is None:
            raise ValidationFailed(
                f"external signature {key!r} requires a wired signature connector",
                field=key,
            )
        payload = data.get(key)
        query: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {"value": payload}
        verified = await connector.verify_external(
            tenant_id, spec.signature_connector or "default", query
        )
        if not verified:
            raise ValidationFailed(
                f"external signature {key!r} was not verified",
                field=key,
            )


def route_attachment_references(
    definition: FormDefinition,
    data: Mapping[str, Any],
    *,
    known_document_ids: Mapping[str, str],
) -> dict[str, Any]:
    """Normalize attachment field values to ``{"document_id": ...}``.

    Values already in reference shape pass through; inline-blob fields
    are stamped with the freshly stored document id from
    ``known_document_ids``. Anything else fails closed naming the
    field (a raw string/bytes payload never lands in case data).
    """
    fields = field_map(definition)
    result: dict[str, Any] = dict(data)
    for key, spec in fields.items():
        if spec.field_type != "attachment":
            continue
        value = result.get(key)
        if _is_blank(value):
            continue
        # why: freshly stored ids win first — a "pending" marker stamped
        # before validation must resolve here, never leak into case data.
        if key in known_document_ids:
            result[key] = {"document_id": known_document_ids[key]}
            continue
        if isinstance(value, Mapping) and str(value.get("document_id") or "").strip():
            continue
        raise ValidationFailed(
            spec.validation_message or f"attachment {key!r} needs an uploaded document",
            field=key,
        )
    return result


def _is_blank(value: Any) -> bool:
    """Treat None/empty-string/empty-container as blank (0/False are values)."""
    if value is None:
        return True
    if isinstance(value, str) and value == "":
        return True
    return bool(isinstance(value, (list, tuple, dict, set)) and len(value) == 0)


__all__ = [
    "apply_calculated_fields",
    "apply_lookup_prepopulation",
    "apply_system_fields",
    "check_signature_fields",
    "field_map",
    "hidden_field_keys",
    "route_attachment_references",
    "validate_visible_fields_deterministic",
    "visible_field_keys",
    "_is_blank",
]
