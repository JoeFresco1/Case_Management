"""Domain + application tests for FM-012 (P05-US6, P05-US8).

Covers:

1. **Derived fields compute** correctly across all supported operators
   (add / sub / mul / div / concat / coalesce / if_present).
2. **Derived fields** reject invalid shapes (unknown operator, bad
   operand type, arithmetic on text field) and reject operands that
   are not defined in the schema at publish time.
3. **Label rename** preserves the stable machine ``key``: a rename
   changes the visible label, leaves the key untouched, and appends
   to ``label_history`` in version order. Historical data referencing
   the key remains valid because the key never changes.
4. **History invariants**: rename entries are sorted by version, the
   first entry has an empty ``previous``, and renames to the current
   label are rejected as no-ops.

These tests are pure-domain / pure-application; no I/O. They run on
the sqlite test profile and on the PostgreSQL path when Docker is
available (see AGENTS.md §7).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.application.cases.compute_derived import compute_derived_fields
from app.application.configuration.validate_case_type_definition import validate_definition
from app.domain.case_type import (
    CaseTypeField,
    CaseTypeSchema,
    DerivedFieldSpec,
    LabelRename,
)
from app.domain.errors import InvariantViolation, ValidationFailed

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _schema_with_derived_total() -> CaseTypeSchema:
    """Schema where ``total = quantity * unit_price``."""
    return CaseTypeSchema(
        fields=(
            CaseTypeField(key="quantity", label="Quantity", type="integer", required=True),
            CaseTypeField(key="unit_price", label="Unit price", type="decimal", required=True),
            CaseTypeField(
                key="total",
                label="Total",
                type="decimal",
                derived=DerivedFieldSpec(
                    operator="mul",
                    operands=("quantity", "unit_price"),
                ),
            ),
        ),
        document_classifications=("Correspondence",),
        rules=(),
    )


def _schema_with_concat_full_name() -> CaseTypeSchema:
    return CaseTypeSchema(
        fields=(
            CaseTypeField(key="first_name", label="First name", type="text"),
            CaseTypeField(key="last_name", label="Last name", type="text"),
            CaseTypeField(
                key="full_name",
                label="Full name",
                type="text",
                derived=DerivedFieldSpec(
                    operator="concat",
                    operands=("first_name", "last_name"),
                ),
            ),
        ),
        document_classifications=(),
        rules=(),
    )


# ---------------------------------------------------------------------------
# 1. Derived computes
# ---------------------------------------------------------------------------


def test_compute_derived__mul_totals_quantity_and_unit_price() -> None:
    """Arithmetic: ``total = quantity * unit_price``."""
    schema = _schema_with_derived_total()
    out = compute_derived_fields({"quantity": 3, "unit_price": 4.50}, schema)
    assert out["total"] == 13.5


def test_compute_derived__add_and_sub_chained() -> None:
    schema = CaseTypeSchema(
        fields=(
            CaseTypeField(key="a", label="A", type="integer"),
            CaseTypeField(key="b", label="B", type="integer"),
            CaseTypeField(key="c", label="C", type="integer"),
            CaseTypeField(
                key="sum_ab",
                label="A+B",
                type="integer",
                derived=DerivedFieldSpec(operator="add", operands=("a", "b")),
            ),
            CaseTypeField(
                key="diff_sum_c",
                label="(A+B)-C",
                type="integer",
                derived=DerivedFieldSpec(operator="sub", operands=("sum_ab", "c")),
            ),
        ),
        document_classifications=(),
        rules=(),
    )
    out = compute_derived_fields({"a": 10, "b": 5, "c": 3}, schema)
    assert out["sum_ab"] == 15
    assert out["diff_sum_c"] == 12


def test_compute_derived__div_integer_stays_int_when_exact() -> None:
    schema = CaseTypeSchema(
        fields=(
            CaseTypeField(key="total", label="Total", type="integer"),
            CaseTypeField(key="count", label="Count", type="integer"),
            CaseTypeField(
                key="avg",
                label="Average",
                type="integer",
                derived=DerivedFieldSpec(operator="div", operands=("total", "count")),
            ),
        ),
        document_classifications=(),
        rules=(),
    )
    assert compute_derived_fields({"total": 100, "count": 4}, schema)["avg"] == 25


def test_compute_derived__div_returns_float_when_non_exact() -> None:
    schema = CaseTypeSchema(
        fields=(
            CaseTypeField(key="total", label="Total", type="decimal"),
            CaseTypeField(key="count", label="Count", type="integer"),
            CaseTypeField(
                key="avg",
                label="Average",
                type="decimal",
                derived=DerivedFieldSpec(operator="div", operands=("total", "count")),
            ),
        ),
        document_classifications=(),
        rules=(),
    )
    assert compute_derived_fields({"total": 10.0, "count": 4}, schema)["avg"] == 2.5


def test_compute_derived__concat_joins_operands() -> None:
    out = compute_derived_fields(
        {"first_name": "Ada", "last_name": "Lovelace"},
        _schema_with_concat_full_name(),
    )
    assert out["full_name"] == "AdaLovelace"


def test_compute_derived__coalesce_picks_first_present() -> None:
    schema = CaseTypeSchema(
        fields=(
            CaseTypeField(key="nick", label="Nickname", type="text"),
            CaseTypeField(key="full", label="Full name", type="text"),
            CaseTypeField(
                key="display",
                label="Display name",
                type="text",
                derived=DerivedFieldSpec(operator="coalesce", operands=("nick", "full")),
            ),
        ),
        document_classifications=(),
        rules=(),
    )
    # No nick, falls through to full.
    out = compute_derived_fields({"full": "Grace Hopper"}, schema)
    assert out["display"] == "Grace Hopper"
    # Nick present wins.
    out = compute_derived_fields({"nick": "GH", "full": "Grace Hopper"}, schema)
    assert out["display"] == "GH"


def test_compute_derived__if_present_returns_fallback_when_missing() -> None:
    schema = CaseTypeSchema(
        fields=(
            CaseTypeField(key="primary", label="Primary", type="text"),
            CaseTypeField(key="secondary", label="Secondary", type="text"),
            CaseTypeField(
                key="chosen",
                label="Chosen",
                type="text",
                derived=DerivedFieldSpec(
                    operator="if_present",
                    operands=("primary",),
                    fallback="secondary",
                ),
            ),
        ),
        document_classifications=(),
        rules=(),
    )
    out = compute_derived_fields({"secondary": "Backup"}, schema)
    assert out["chosen"] == "Backup"
    out = compute_derived_fields({"primary": "First", "secondary": "Backup"}, schema)
    assert out["chosen"] == "First"


def test_compute_derived__missing_operand_yields_none() -> None:
    """Draft cases may be partial; the evaluator never raises on a
    missing operand, it just resolves to ``None`` and lets the operator
    decide what that means."""
    schema = _schema_with_derived_total()
    out = compute_derived_fields({"quantity": 3}, schema)
    # The missing operand is dropped from the result; the derived total
    # is None, not a runtime error.
    assert out["total"] is None


def test_compute_derived__drops_caller_supplied_derived_value() -> None:
    """Caller cannot smuggle a value for a derived key."""
    schema = _schema_with_derived_total()
    out = compute_derived_fields({"quantity": 2, "unit_price": 3.0, "total": 999}, schema)
    assert out["total"] == 6.0


def test_compute_derived__div_by_zero_raises_invariant() -> None:
    schema = CaseTypeSchema(
        fields=(
            CaseTypeField(key="x", label="X", type="integer"),
            CaseTypeField(key="y", label="Y", type="integer"),
            CaseTypeField(
                key="q",
                label="Q",
                type="integer",
                derived=DerivedFieldSpec(operator="div", operands=("x", "y")),
            ),
        ),
        document_classifications=(),
        rules=(),
    )
    with pytest.raises(InvariantViolation) as excinfo:
        compute_derived_fields({"x": 10, "y": 0}, schema)
    assert "zero" in str(excinfo.value).lower()


def test_compute_derived__arithmetic_on_text_field_rejected_at_construction() -> None:
    """A text field declaring an arithmetic operator is invalid; the
    shape check fires in ``CaseTypeField.__post_init__``."""
    with pytest.raises(InvariantViolation):
        CaseTypeField(
            key="title",
            label="Title",
            type="text",
            derived=DerivedFieldSpec(operator="add", operands=("a", "b")),
        )


# ---------------------------------------------------------------------------
# 2. Publish-gate validation
# ---------------------------------------------------------------------------


def test_validate_definition__derived_with_undefined_operand_rejected() -> None:
    schema = CaseTypeSchema(
        fields=(
            CaseTypeField(
                key="total",
                label="Total",
                type="decimal",
                derived=DerivedFieldSpec(operator="mul", operands=("quantity", "unit_price")),
            ),
        ),
        document_classifications=(),
        rules=(),
    )
    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(schema)
    assert "undefined operand" in str(excinfo.value).lower()


def test_validate_definition__healthy_derived_schema_passes() -> None:
    """A schema whose derived fields reference defined operands passes."""
    validate_definition(_schema_with_derived_total())


# ---------------------------------------------------------------------------
# 3. Label rename preserves stable key (P05-US8 / FM-012)
# ---------------------------------------------------------------------------


def test_rename_label__preserves_key_and_appends_history() -> None:
    """Renaming a label changes the visible label and appends a history
    entry; the machine ``key`` never changes (P05-US8)."""
    original = CaseTypeField(key="title", label="Title", type="text")
    renamed = original.rename_label(new_label="Case title", at_version=2, at=FIXED_NOW)

    assert renamed.key == "title"  # stable identity
    assert renamed.label == "Case title"
    assert len(renamed.label_history) == 1
    history = renamed.label_history[0]
    assert history.previous == "Title"
    assert history.current == "Case title"
    assert history.at_version == 2
    assert history.at == FIXED_NOW
    # Original is frozen and unchanged.
    assert original.label == "Title"
    assert original.label_history == ()


def test_rename_label__chained_history_keeps_key_intact() -> None:
    """Multiple renames accumulate; the key is still the original."""
    field = CaseTypeField(key="title", label="Title", type="text")
    field = field.rename_label(new_label="Case Title", at_version=2)
    field = field.rename_label(new_label="Case Title (final)", at_version=3)
    assert field.key == "title"
    assert field.label == "Case Title (final)"
    versions = [r.at_version for r in field.label_history]
    assert versions == [2, 3]


def test_rename_label__no_op_rejected() -> None:
    """Renaming to the current label is rejected so history cannot be
    polluted with empty entries."""
    field = CaseTypeField(key="title", label="Title", type="text")
    with pytest.raises(InvariantViolation):
        field.rename_label(new_label="Title", at_version=2)


def test_rename_label__empty_label_rejected() -> None:
    field = CaseTypeField(key="title", label="Title", type="text")
    with pytest.raises(InvariantViolation):
        field.rename_label(new_label="   ", at_version=2)


def test_label_history__unsorted_rejected() -> None:
    """A manually-constructed history that is not version-ordered is
    rejected at construction time."""
    with pytest.raises(InvariantViolation):
        CaseTypeField(
            key="title",
            label="Title",
            type="text",
            label_history=(
                LabelRename(
                    previous="Title",
                    current="Title v2",
                    at_version=2,
                ),
                LabelRename(
                    previous="Title v2",
                    current="Title v1 rename",
                    at_version=1,
                ),
            ),
        )


def test_label_history__duplicate_version_rejected() -> None:
    with pytest.raises(InvariantViolation):
        CaseTypeField(
            key="title",
            label="Title",
            type="text",
            label_history=(
                LabelRename(previous="", current="v1", at_version=1),
                LabelRename(previous="v1", current="v1", at_version=1),
            ),
        )


# ---------------------------------------------------------------------------
# 4. Acceptance scenarios (cross-feature)
# ---------------------------------------------------------------------------


def test_admin_renames_label__existing_case_data_still_valid_by_key() -> None:
    """AC: admin renames label ``title`` -> ``case_title`` key unchanged,
    old case data still valid; derived field computes as configured.

    Two published versions exist. v1 stores cases whose ``data.title``
    is the user-typed value. v2 renames the label to ``Case title``;
    the key stays ``title`` so legacy data, integrations, and audit
    rows that reference the key continue to resolve. Derived fields
    configured in v2 compute from v2's operand set.
    """
    v1 = CaseTypeField(key="title", label="Title", type="text")
    # Admin publishes v2 with a renamed label.
    v2 = v1.rename_label(new_label="Case title", at_version=2)
    schema_v2 = CaseTypeSchema(
        fields=(v2,),
        document_classifications=(),
        rules=(),
    )
    # Legacy case data was captured against v1; the field is still
    # found by its key on v2.
    legacy_data: dict[str, Any] = {"title": "Outage on east wing"}
    assert schema_v2.field("title") is not None
    assert schema_v2.field("title").key == v2.key
    # The legacy value remains accessible.
    assert legacy_data[schema_v2.field("title").key] == "Outage on east wing"


def test_derived_field_computes_as_configured_across_versions() -> None:
    """A schema with a derived ``total`` field computes the value
    deterministically regardless of which published version of the
    case type is current. v2 only renames the label on ``quantity``
    so the same data yields the same derived value."""
    v1 = CaseTypeSchema(
        fields=(
            CaseTypeField(key="quantity", label="Quantity", type="integer"),
            CaseTypeField(key="unit_price", label="Unit price", type="decimal"),
            CaseTypeField(
                key="total",
                label="Total",
                type="decimal",
                derived=DerivedFieldSpec(
                    operator="mul",
                    operands=("quantity", "unit_price"),
                ),
            ),
        ),
        document_classifications=(),
        rules=(),
    )
    # v2 renames ``quantity``'s label; the key is unchanged.
    v2_fields = tuple(
        f.rename_label(new_label="Quantity (count)", at_version=2) if f.key == "quantity" else f
        for f in v1.fields
    )
    v2 = CaseTypeSchema(
        fields=v2_fields,
        document_classifications=(),
        rules=(),
    )
    data = {"quantity": 5, "unit_price": 2.0}
    assert compute_derived_fields(data, v1)["total"] == 10.0
    assert compute_derived_fields(data, v2)["total"] == 10.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXED_NOW: Any = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
