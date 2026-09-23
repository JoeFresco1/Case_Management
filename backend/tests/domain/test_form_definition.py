"""Pure-domain tests for T3 FormDefinition/Version (P07 FM-036/037/038, P05 meta).

Covers deterministic fields, sections, conditions, validation, tab order,
calculated, lookup, attachment, signature, system properties. Acceptance:
published immutable, cases pin versions, ambiguous/code rejected, hidden
preserved safely. No I/O; runs on sqlite profile and CI without Docker.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.domain.errors import InvariantViolation, ValidationFailed
from app.domain.form import (
    FormBinding,
    FormDefinition,
    FormFieldSpec,
    FormSectionSpec,
    FormVersion,
    FormVersionStatus,
    apply_hidden_field_preservation,
    apply_submission_preservation,
    effective_tab_order,
    evaluate_calculated,
    read_only_field_keys,
    validate_field_value,
)

TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
ACTOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)


def _simple_section(key: str = "s", field_key: str = "f") -> FormSectionSpec:
    """One section with one text field (minimal healthy shape)."""
    return FormSectionSpec(
        key=key,
        label=key.title(),
        fields=(FormFieldSpec(key=field_key, label="Field"),),
    )


def _draft(
    sections: tuple[FormSectionSpec, ...] | None = None,
    validations: tuple[Mapping[str, Any], ...] = (),
    steps: tuple[Mapping[str, Any], ...] = (),
) -> FormVersion:
    """Draft version with given body (defaults to one simple section)."""
    secs = sections if sections is not None else (_simple_section(),)
    return FormVersion.draft(
        tenant_id=TENANT,
        key="Intake",
        version_number=1,
        definition=FormDefinition(
            sections=secs,
            validations=validations,
            steps=steps,
        ),
        now=NOW,
    )


def test_attachment_field_valid__accepts_classification() -> None:
    # given / when
    field = FormFieldSpec(
        key="doc", label="Doc", field_type="attachment", attachment_classification="Evidence"
    )
    # then
    assert field.attachment_classification == "Evidence"


def test_attachment_missing_classification__rejects() -> None:
    # given / when / then
    with pytest.raises(InvariantViolation):
        FormFieldSpec(key="doc", label="Doc", field_type="attachment")


def test_attachment_attrs_on_text__rejects() -> None:
    # given / when / then
    with pytest.raises(InvariantViolation):
        FormFieldSpec(key="t", label="T", attachment_classification="Evidence")


def test_binding_pin_match__applies() -> None:
    # given
    case_type_version_id = uuid4()
    binding = FormBinding.draft(
        tenant_id=TENANT, case_type_version_id=case_type_version_id, form_version_id=uuid4()
    ).publish(actor_id=ACTOR, at=NOW)
    # when / then
    binding.assert_applies_to_case(tenant_id=TENANT, case_type_version_id=case_type_version_id)
    assert binding.is_published is True


def test_binding_pin_mismatch__rejects() -> None:
    # given
    binding = FormBinding.draft(
        tenant_id=TENANT, case_type_version_id=uuid4(), form_version_id=uuid4()
    ).publish(actor_id=ACTOR, at=NOW)
    # when / then
    with pytest.raises(InvariantViolation, match="pinned"):
        binding.assert_applies_to_case(tenant_id=TENANT, case_type_version_id=uuid4())


def test_binding_retired__not_published_and_rejects_pin() -> None:
    # given
    binding = FormBinding.draft(
        tenant_id=TENANT, case_type_version_id=uuid4(), form_version_id=uuid4()
    ).publish(actor_id=ACTOR, at=NOW)
    retired = binding.retire(actor_id=ACTOR, at=NOW)
    # when / then
    assert retired.is_published is False
    with pytest.raises(InvariantViolation, match="retired"):
        retired.assert_applies_to_case(
            tenant_id=TENANT, case_type_version_id=binding.case_type_version_id
        )


def test_binding_tenant_mismatch__rejects() -> None:
    # given
    binding = FormBinding.draft(
        tenant_id=TENANT, case_type_version_id=uuid4(), form_version_id=uuid4()
    ).publish(actor_id=ACTOR, at=NOW)
    # when / then
    with pytest.raises(InvariantViolation, match="tenant"):
        binding.assert_applies_to_case(
            tenant_id=OTHER_TENANT, case_type_version_id=binding.case_type_version_id
        )


def test_binding_unpublished__rejects_pin() -> None:
    # given
    binding = FormBinding.draft(
        tenant_id=TENANT, case_type_version_id=uuid4(), form_version_id=uuid4()
    )
    # when / then
    with pytest.raises(InvariantViolation, match="not published"):
        binding.assert_applies_to_case(
            tenant_id=TENANT, case_type_version_id=binding.case_type_version_id
        )


def test_calculated_div_by_zero__raises() -> None:
    # given
    formula = {"op": "div", "operands": ["a", "b"]}
    # when / then
    with pytest.raises(InvariantViolation, match="division by zero"):
        evaluate_calculated(formula, {"a": 1, "b": 0})


def test_calculated_evaluation__deterministic_results() -> None:
    # given / when / then
    assert evaluate_calculated({"op": "add", "operands": ["a", "b"]}, {"a": 2, "b": 3}) == 5.0
    assert (
        evaluate_calculated({"op": "concat", "operands": ["a", "b"]}, {"a": "x", "b": "y"}) == "xy"
    )
    assert (
        evaluate_calculated({"op": "coalesce", "operands": ["a", "b"]}, {"a": None, "b": "v"})
        == "v"
    )
    assert (
        evaluate_calculated(
            {"op": "if_present", "operands": ["a"], "fallback": "b"}, {"a": "", "b": "fb"}
        )
        == "fb"
    )


def test_calculated_field_valid__requires_formula_and_readonly() -> None:
    # given / when
    field = FormFieldSpec(
        key="total",
        label="Total",
        field_type="calculated",
        read_only=True,
        formula={"op": "add", "operands": ["a", "b"]},
    )
    # then
    assert field.formula is not None


def test_calculated_missing_formula__rejects() -> None:
    # given / when / then
    with pytest.raises(InvariantViolation):
        FormFieldSpec(key="total", label="Total", field_type="calculated", read_only=True)


def test_calculated_not_readonly__rejects() -> None:
    # given / when / then
    with pytest.raises(InvariantViolation):
        FormFieldSpec(
            key="total",
            label="Total",
            field_type="calculated",
            formula={"op": "add", "operands": ["a", "b"]},
        )


def test_calculated_unknown_operand__rejects_at_draft() -> None:
    # given
    sections = (
        FormSectionSpec(
            key="s",
            label="S",
            fields=(
                FormFieldSpec(
                    key="total",
                    label="Total",
                    field_type="calculated",
                    read_only=True,
                    formula={"op": "add", "operands": ["a", "ghost"]},
                ),
                FormFieldSpec(key="a", label="A", field_type="integer"),
            ),
        ),
    )
    # when / then
    with pytest.raises(InvariantViolation, match="unknown"):
        _draft(sections=sections)


def test_condition_code_operator__rejects() -> None:
    # given
    sections = (
        _simple_section(),
        FormSectionSpec(
            key="hide",
            label="Hide",
            fields=(FormFieldSpec(key="g", label="G"),),
            visible_when={"eval": {"field": "f", "value": 1}},
        ),
    )
    # when / then
    with pytest.raises(InvariantViolation, match="arbitrary code"):
        _draft(sections=sections)


def test_condition_extra_operand_keys__rejects() -> None:
    # given
    sections = (
        _simple_section(),
        FormSectionSpec(
            key="hide",
            label="Hide",
            fields=(FormFieldSpec(key="g", label="G"),),
            visible_when={"eq": {"field": "f", "value": 1, "code": "x"}},
        ),
    )
    # when / then
    with pytest.raises(InvariantViolation, match="only"):
        _draft(sections=sections)


def test_condition_unknown_field__rejects() -> None:
    # given
    sections = (
        _simple_section(),
        FormSectionSpec(
            key="hide",
            label="Hide",
            fields=(FormFieldSpec(key="g", label="G"),),
            visible_when={"eq": {"field": "ghost", "value": 1}},
        ),
    )
    # when / then
    with pytest.raises(InvariantViolation, match="unknown"):
        _draft(sections=sections)


def test_condition_unknown_operator__rejects() -> None:
    # given
    sections = (
        _simple_section(),
        FormSectionSpec(
            key="hide",
            label="Hide",
            fields=(FormFieldSpec(key="g", label="G"),),
            visible_when={"regex": {"field": "f", "value": "^a"}},
        ),
    )
    # when / then
    with pytest.raises(InvariantViolation, match="unknown form operator"):
        _draft(sections=sections)


def test_default_mismatch__rejects() -> None:
    # given / when / then
    with pytest.raises(InvariantViolation):
        FormFieldSpec(key="n", label="N", field_type="integer", default_value="not-int")


def test_duplicate_field_key__rejects() -> None:
    # given
    sections = (
        FormSectionSpec(key="a", label="A", fields=(FormFieldSpec(key="f", label="F"),)),
        FormSectionSpec(key="b", label="B", fields=(FormFieldSpec(key="f", label="F2"),)),
    )
    # when / then
    with pytest.raises(InvariantViolation, match="duplicate field"):
        _draft(sections=sections)


def test_duplicate_tab_order__rejects() -> None:
    # given
    sections = (
        FormSectionSpec(
            key="s",
            label="S",
            fields=(
                FormFieldSpec(key="a", label="A", tab_order=0),
                FormFieldSpec(key="b", label="B", tab_order=0),
            ),
        ),
    )
    # when / then
    with pytest.raises(InvariantViolation, match="duplicate tab_order"):
        _draft(sections=sections)


def test_effective_tab_order__explicit_first_then_definition() -> None:
    # given
    definition = FormDefinition(
        sections=(
            FormSectionSpec(
                key="s",
                label="S",
                fields=(
                    FormFieldSpec(key="first", label="First"),
                    FormFieldSpec(key="second", label="Second", tab_order=0),
                    FormFieldSpec(key="ro", label="RO", read_only=True),
                    FormFieldSpec(
                        key="sys",
                        label="Sys",
                        field_type="system",
                        system_property="case.reference",
                        read_only=True,
                    ),
                ),
            ),
        )
    )
    # when
    order = effective_tab_order(definition)
    # then
    assert order == ("second", "first")


def test_enum_without_options__rejects() -> None:
    # given / when / then
    with pytest.raises(InvariantViolation):
        FormFieldSpec(key="p", label="P", field_type="enum")


def test_hidden_preservation__keeps_stored() -> None:
    # given
    stored = {"visible": "old", "hidden": "keep"}
    # when
    merged = apply_hidden_field_preservation(
        stored_data=stored,
        submitted_data={"visible": "new", "hidden": "blank"},
        hidden_keys=frozenset({"hidden"}),
    )
    # then
    assert merged == {"visible": "new", "hidden": "keep"}
    assert stored == {"visible": "old", "hidden": "keep"}


def test_lookup_bad_source__rejects() -> None:
    # given / when / then
    with pytest.raises(InvariantViolation):
        FormFieldSpec(key="c", label="C", field_type="lookup", lookup_source="has space")


def test_lookup_field_valid__accepts_slug() -> None:
    # given / when
    field = FormFieldSpec(key="cust", label="Cust", field_type="lookup", lookup_source="customers")
    # then
    assert field.lookup_source == "customers"


def test_publish_lifecycle__immutable_and_guarded() -> None:
    # given
    draft = _draft()
    # when
    published = draft.publish(actor_id=ACTOR, at=NOW)
    # then
    assert published.status is FormVersionStatus.PUBLISHED
    assert draft.status is FormVersionStatus.DRAFT
    with pytest.raises(InvariantViolation):
        published.publish(actor_id=ACTOR, at=NOW)
    with pytest.raises(InvariantViolation):
        published.assert_mutable()
    draft.assert_mutable()
    retired = published.retire()
    assert retired.status is FormVersionStatus.RETIRED
    with pytest.raises(InvariantViolation):
        retired.retire()


def test_readonly_and_system_preserved__drops_inbound() -> None:
    # given
    stored = {"name": "old", "owner": "alice", "ref": "C-1"}
    # when
    merged = apply_submission_preservation(
        stored_data=stored,
        submitted_data={"name": "new", "owner": "mallory", "ref": "C-2"},
        hidden_keys=frozenset(),
        read_only_keys=frozenset({"owner", "ref"}),
    )
    # then
    assert merged == {"name": "new", "owner": "alice", "ref": "C-1"}
    assert read_only_field_keys(
        FormDefinition(
            sections=(
                FormSectionSpec(
                    key="s",
                    label="S",
                    fields=(
                        FormFieldSpec(key="owner", label="Owner", read_only=True),
                        FormFieldSpec(key="name", label="Name"),
                    ),
                ),
            )
        )
    ) == frozenset({"owner"})


def test_readonly_with_tab_order__rejects() -> None:
    # given / when / then
    with pytest.raises(InvariantViolation):
        FormFieldSpec(key="ro", label="RO", read_only=True, tab_order=1)


def test_repeating_bounds_invalid__rejects() -> None:
    # given / when / then
    with pytest.raises(InvariantViolation):
        FormSectionSpec(key="r", label="R", fields=(), kind="repeating", min_items=3, max_items=1)


def test_repeating_section_valid__accepts_bounds() -> None:
    # given / when
    section = FormSectionSpec(
        key="jobs", label="Jobs", fields=(), kind="repeating", min_items=0, max_items=3
    )
    # then
    assert section.max_items == 3


def test_self_referential_visibility__rejects_as_ambiguous() -> None:
    # given
    sections = (
        FormSectionSpec(
            key="s",
            label="S",
            fields=(FormFieldSpec(key="f", label="F"),),
            visible_when={"eq": {"field": "f", "value": 1}},
        ),
    )
    # when / then
    with pytest.raises(InvariantViolation, match="ambiguous"):
        _draft(sections=sections)


def test_signature_bad_kind__rejects() -> None:
    # given / when / then
    with pytest.raises(InvariantViolation):
        FormFieldSpec(key="sig", label="Sig", field_type="signature", signature_kind="wet-ink")


def test_signature_field_valid__accepts_seam() -> None:
    # given / when
    field = FormFieldSpec(
        key="sig",
        label="Sig",
        field_type="signature",
        signature_kind="external",
        signature_connector="docusign",
    )
    # then
    assert field.signature_kind == "external"


def test_steps_unknown_section__rejects() -> None:
    # given
    sections = (_simple_section(),)
    steps: tuple[Mapping[str, Any], ...] = (
        {"step_key": "one", "label": "One", "section_keys": ["ghost"]},
    )
    # when / then
    with pytest.raises(InvariantViolation, match="unknown section"):
        _draft(sections=sections, steps=steps)


def test_system_field_valid__read_only_pinned() -> None:
    # given / when
    field = FormFieldSpec(
        key="ref",
        label="Ref",
        field_type="system",
        system_property="case.reference",
        read_only=True,
    )
    # then
    assert field.system_property == "case.reference"


def test_system_not_readonly__rejects() -> None:
    # given / when / then
    with pytest.raises(InvariantViolation):
        FormFieldSpec(key="ref", label="Ref", field_type="system", system_property="case.reference")


def test_validation_bad_then_action__rejects_code() -> None:
    # given
    sections = (
        _simple_section(field_key="a"),
        FormSectionSpec(key="b", label="B", fields=(FormFieldSpec(key="c", label="C"),)),
    )
    validations: tuple[Mapping[str, Any], ...] = (
        {"when": {"eq": {"field": "a", "value": 1}}, "then": {"exec": "c"}},
    )
    # when / then
    with pytest.raises(InvariantViolation):
        _draft(sections=sections, validations=validations)


def test_validation_requires_known_field__rejects_dangling() -> None:
    # given
    sections = (_simple_section(field_key="a"),)
    validations: tuple[Mapping[str, Any], ...] = (
        {"when": {"eq": {"field": "a", "value": 1}}, "then": {"required": "ghost"}},
    )
    # when / then
    with pytest.raises(InvariantViolation, match="unknown"):
        _draft(sections=sections, validations=validations)


def test_validation_valid_rule__accepts_with_message() -> None:
    # given
    sections = (
        FormSectionSpec(
            key="a", label="A", fields=(FormFieldSpec(key="is_appeal", label="Appeal?"),)
        ),
        FormSectionSpec(key="b", label="B", fields=(FormFieldSpec(key="reason", label="Reason"),)),
    )
    validations: tuple[Mapping[str, Any], ...] = (
        {
            "when": {"eq": {"field": "is_appeal", "value": True}},
            "then": {"required": "reason", "message": "Tell us why"},
        },
    )
    # when
    version = _draft(sections=sections, validations=validations)
    # then
    assert len(version.definition.validations) == 1


def test_validate_field_value__custom_message_named() -> None:
    # given
    field = FormFieldSpec(
        key="age",
        label="Age",
        field_type="integer",
        minimum=0,
        maximum=150,
        validation_message="Age 0-150",
    )
    # when / then
    with pytest.raises(ValidationFailed) as exc:
        validate_field_value(field, -1)
    assert exc.value.field == "age"
    assert "Age 0-150" in str(exc.value)


def test_validate_field_value__enum_and_attachment_shapes() -> None:
    # given
    enum_field = FormFieldSpec(key="p", label="P", field_type="enum", options=("low", "high"))
    attach = FormFieldSpec(
        key="doc", label="Doc", field_type="attachment", attachment_classification="Evidence"
    )
    # when / then
    validate_field_value(enum_field, "low")
    with pytest.raises(ValidationFailed):
        validate_field_value(enum_field, "urgent")
    with pytest.raises(ValidationFailed):
        validate_field_value(attach, {"no_id": 1})


def test_visibility_cycle__rejects_as_ambiguous() -> None:
    # given
    sections = (
        FormSectionSpec(
            key="a",
            label="A",
            fields=(FormFieldSpec(key="fa", label="FA"),),
            visible_when={"eq": {"field": "fb", "value": 1}},
        ),
        FormSectionSpec(
            key="b",
            label="B",
            fields=(FormFieldSpec(key="fb", label="FB"),),
            visible_when={"eq": {"field": "fa", "value": 1}},
        ),
    )
    # when / then
    with pytest.raises(InvariantViolation, match="cycle"):
        _draft(sections=sections)


def test_visible_sections__submission_override_reveals() -> None:
    # given
    sections = (
        _simple_section(key="intake", field_key="is_appeal"),
        FormSectionSpec(
            key="detail",
            label="Detail",
            fields=(FormFieldSpec(key="reason", label="Reason"),),
            visible_when={"eq": {"field": "is_appeal", "value": True}},
        ),
    )
    version = _draft(sections=sections)
    # when
    hidden = version.visible_section_keys(visible_data={})
    shown = version.visible_section_keys(visible_data={}, submission_data={"is_appeal": True})
    # then
    assert hidden == frozenset({"intake"})
    assert shown == frozenset({"intake", "detail"})
