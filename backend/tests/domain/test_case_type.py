"""Domain tests for `app.domain.case_type` (P05-US1, FM-008, FR-015).

Scenarios covered:

1. **Draft factory**: `CaseTypeVersion.draft` constructs an immutable
   instance with the right defaults.
2. **Lifecycle transitions**: ``draft → published → retired`` succeed;
   any attempt to publish a non-draft version raises
   ``InvariantViolation``.
3. **Immutability guard**: a published version is frozen. Re-publishing
   (or any status-changing method) raises.
4. **Field invariants**: malformed ``CaseTypeField`` (bad key, bad
   type, missing enum_values on enum, bad sensitivity) raises.
5. **Schema validator**: ``validate_definition`` accepts a healthy
   schema, rejects missing-required, duplicate-key, and rule-shape
   defects.

These tests are pure-domain — no I/O, no fakes, no DB. They run on
every Python interpreter the CI uses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.application.configuration.validate_case_type_definition import validate_definition
from app.domain.case_type import (
    CaseType,
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.errors import InvariantViolation, ValidationFailed

# ---------------------------------------------------------------------------
# Fixed inputs
# ---------------------------------------------------------------------------


ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
BETA_TENANT = UUID("22222222-2222-2222-2222-222222222222")
ACTOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def _healthy_schema() -> CaseTypeSchema:
    """A minimal valid schema: one text field, one classification, one rule."""
    return CaseTypeSchema(
        fields=(CaseTypeField(key="title", label="Title", type="text", required=True),),
        document_classifications=("Correspondence",),
        rules=({"op": "exists", "field": "title"},),
    )


def _draft(*, version_number: int = 1) -> CaseTypeVersion:
    return CaseTypeVersion.draft(
        tenant_id=ACME_TENANT,
        case_type_id=uuid4(),
        version_number=version_number,
        schema=_healthy_schema(),
        now=FIXED_NOW,
    )


# ---------------------------------------------------------------------------
# 1. Draft factory
# ---------------------------------------------------------------------------


def test_draft_factory__constructs_immutable_draft_with_defaults() -> None:
    """`CaseTypeVersion.draft` returns a draft with `status=draft`,
    `published_at=None`, and the supplied timestamps."""
    draft = _draft()
    assert draft.status is CaseTypeVersionStatus.DRAFT
    assert draft.published_at is None
    assert draft.published_by is None
    assert draft.created_at == FIXED_NOW
    assert draft.version_number == 1
    assert draft.schema.field_keys() == ("title",)


def test_draft_factory__rejects_non_positive_version() -> None:
    """Version numbers are 1-based."""
    with pytest.raises(InvariantViolation):
        CaseTypeVersion.draft(
            tenant_id=ACME_TENANT,
            case_type_id=uuid4(),
            version_number=0,
            schema=_healthy_schema(),
            now=FIXED_NOW,
        )


# ---------------------------------------------------------------------------
# 2. Lifecycle transitions
# ---------------------------------------------------------------------------


def test_publish__draft_to_published__stamps_actor_and_time() -> None:
    """`publish` returns a new immutable instance with `published_at` /
    `published_by` set. The original draft is untouched (frozen)."""
    draft = _draft()
    published = draft.publish(actor_id=ACTOR, at=FIXED_NOW)

    assert published.status is CaseTypeVersionStatus.PUBLISHED
    assert published.published_at == FIXED_NOW
    assert published.published_by == ACTOR
    assert published.id == draft.id
    # The draft instance is unchanged.
    assert draft.status is CaseTypeVersionStatus.DRAFT
    assert draft.published_at is None


def test_publish__on_published_version__raises_invariant() -> None:
    """A published version cannot be re-published. This is the FR-015
    immutability contract (also enforced at the repository / DB layer)."""
    published = _draft().publish(actor_id=ACTOR, at=FIXED_NOW)
    with pytest.raises(InvariantViolation) as excinfo:
        published.publish(actor_id=ACTOR, at=FIXED_NOW)
    assert "published" in str(excinfo.value).lower()


def test_publish__on_retired_version__raises_invariant() -> None:
    """A retired version is terminal and cannot be re-published."""
    published = _draft().publish(actor_id=ACTOR, at=FIXED_NOW)
    retired = published.retire()
    assert retired.status is CaseTypeVersionStatus.RETIRED
    with pytest.raises(InvariantViolation):
        retired.publish(actor_id=ACTOR, at=FIXED_NOW)


def test_retire__only_published_can_be_retired() -> None:
    """Drafts and retired versions are not eligible for retire."""
    draft = _draft()
    with pytest.raises(InvariantViolation):
        draft.retire()

    published = draft.publish(actor_id=ACTOR, at=FIXED_NOW)
    retired = published.retire()
    assert retired.status is CaseTypeVersionStatus.RETIRED
    # Double-retire is rejected.
    with pytest.raises(InvariantViolation):
        retired.retire()


# ---------------------------------------------------------------------------
# 3. assert_mutable guard
# ---------------------------------------------------------------------------


def test_assert_mutable__raises_on_non_draft() -> None:
    """`assert_mutable` is the helper called by repositories before any
    UPDATE. It must raise on every non-draft status."""
    published = _draft().publish(actor_id=ACTOR, at=FIXED_NOW)
    with pytest.raises(InvariantViolation):
        published.assert_mutable()

    retired = published.retire()
    with pytest.raises(InvariantViolation):
        retired.assert_mutable()


def test_assert_mutable__silent_on_draft() -> None:
    """`assert_mutable` is a no-op for drafts."""
    _draft().assert_mutable()


# ---------------------------------------------------------------------------
# 4. CaseTypeField invariants
# ---------------------------------------------------------------------------


def test_field__empty_key__rejected() -> None:
    with pytest.raises(InvariantViolation):
        CaseTypeField(key="", label="Label", type="text")


def test_field__key_with_special_chars__rejected() -> None:
    """Keys are alphanumeric + underscore (stable identifier across versions)."""
    with pytest.raises(InvariantViolation):
        CaseTypeField(key="title!", label="Title", type="text")


def test_field__empty_label__rejected() -> None:
    with pytest.raises(InvariantViolation):
        CaseTypeField(key="title", label="   ", type="text")


def test_field__unknown_type__rejected() -> None:
    with pytest.raises(InvariantViolation):
        CaseTypeField(key="title", label="Title", type="richtext")


def test_field__enum_without_values__rejected() -> None:
    with pytest.raises(InvariantViolation):
        CaseTypeField(key="priority", label="Priority", type="enum")


def test_field__enum_with_empty_value__rejected() -> None:
    with pytest.raises(InvariantViolation):
        CaseTypeField(
            key="priority",
            label="Priority",
            type="enum",
            enum_values=("low", ""),
        )


def test_field__bad_sensitivity__rejected() -> None:
    with pytest.raises(InvariantViolation):
        CaseTypeField(
            key="title",
            label="Title",
            type="text",
            sensitivity="top-secret",
        )


# ---------------------------------------------------------------------------
# 5. CaseType invariants
# ---------------------------------------------------------------------------


def test_case_type__bad_sensitivity_default__rejected() -> None:
    with pytest.raises(InvariantViolation):
        CaseType.create(
            tenant_id=ACME_TENANT,
            key="ServiceRequest",
            name="Service Request",
            sensitivity_default="top-secret",
        )


def test_case_type__with_published_version__rejects_draft_version() -> None:
    """Only published versions can be set as the CaseType's current."""
    case_type = CaseType.create(tenant_id=ACME_TENANT, key="ServiceRequest", name="Service Request")
    draft = CaseTypeVersion.draft(
        tenant_id=ACME_TENANT,
        case_type_id=case_type.id,
        version_number=1,
        schema=_healthy_schema(),
    )
    with pytest.raises(InvariantViolation):
        case_type.with_published_version(draft)


def test_case_type__with_published_version__rejects_cross_tenant() -> None:
    """`with_published_version` is tenant-aware: a version belonging to
    a different tenant cannot be promoted."""
    case_type = CaseType.create(tenant_id=ACME_TENANT, key="ServiceRequest", name="Service Request")
    foreign_published = CaseTypeVersion.draft(
        tenant_id=BETA_TENANT,
        case_type_id=case_type.id,
        version_number=1,
        schema=_healthy_schema(),
    ).publish(actor_id=ACTOR, at=FIXED_NOW)

    with pytest.raises(InvariantViolation):
        case_type.with_published_version(foreign_published)


# ---------------------------------------------------------------------------
# 6. validate_definition
# ---------------------------------------------------------------------------


def test_validate_definition__healthy_schema__returns_none() -> None:
    """A well-formed schema is accepted."""
    validate_definition(_healthy_schema())


def test_validate_definition__empty_fields__rejected() -> None:
    """An empty schema has no value and is rejected at the validator."""
    schema = CaseTypeSchema(
        fields=(),
        document_classifications=("Correspondence",),
        rules=(),
    )
    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(schema)
    assert excinfo.value.field == "fields"


def test_validate_definition__duplicate_field_key__rejected() -> None:
    schema = CaseTypeSchema(
        fields=(
            CaseTypeField(key="title", label="Title", type="text"),
            CaseTypeField(key="title", label="Title 2", type="text"),
        ),
        document_classifications=(),
        rules=(),
    )
    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(schema)
    assert "duplicate" in str(excinfo.value).lower()


def test_validate_definition__enum_values_on_non_enum_field__rejected() -> None:
    """`enum_values` is meaningful only for ``enum`` / ``multichoice``."""
    schema = CaseTypeSchema(
        fields=(
            CaseTypeField(
                key="priority",
                label="Priority",
                type="text",
                enum_values=("low", "high"),
            ),
        ),
        document_classifications=(),
        rules=(),
    )
    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(schema)
    assert excinfo.value.field is not None
    assert "enum_values" in excinfo.value.field


def test_validate_definition__empty_classification_entry__rejected() -> None:
    schema = CaseTypeSchema(
        fields=(CaseTypeField(key="title", label="Title", type="text"),),
        document_classifications=("Correspondence", ""),
        rules=(),
    )
    with pytest.raises(ValidationFailed):
        validate_definition(schema)


def test_validate_definition__duplicate_classification__rejected() -> None:
    schema = CaseTypeSchema(
        fields=(CaseTypeField(key="title", label="Title", type="text"),),
        document_classifications=("Correspondence", "Correspondence"),
        rules=(),
    )
    with pytest.raises(ValidationFailed):
        validate_definition(schema)


def test_validate_definition__unknown_rule_operator__rejected() -> None:
    schema = CaseTypeSchema(
        fields=(CaseTypeField(key="title", label="Title", type="text"),),
        document_classifications=(),
        rules=({"op": "definitely-not-real", "field": "title"},),
    )
    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(schema)
    assert "operator" in str(excinfo.value).lower() or "unknown" in str(excinfo.value).lower()


def test_validate_definition__rule_missing_field__rejected() -> None:
    """A leaf rule without a `field` reference is rejected."""
    schema = CaseTypeSchema(
        fields=(CaseTypeField(key="title", label="Title", type="text"),),
        document_classifications=(),
        rules=({"op": "exists"},),
    )
    with pytest.raises(ValidationFailed):
        validate_definition(schema)


def test_validate_definition__nested_composite_rule__accepted() -> None:
    """`all` / `any` / `not` composites nest; bounded depth is fine."""
    schema = CaseTypeSchema(
        fields=(CaseTypeField(key="title", label="Title", type="text"),),
        document_classifications=(),
        rules=(
            {
                "op": "all",
                "clauses": [
                    {"op": "exists", "field": "title"},
                    {
                        "op": "any",
                        "clauses": [
                            {"op": "exists", "field": "title"},
                            {"op": "not", "clause": {"op": "exists", "field": "title"}},
                        ],
                    },
                ],
            },
        ),
    )
    validate_definition(schema)


def test_validate_definition__deeply_nested_rule__rejected() -> None:
    """Beyond the depth cap, the validator rejects to keep the input finite."""
    clause = {"op": "exists", "field": "title"}
    for _ in range(10):
        clause = {"op": "all", "clauses": [clause]}
    schema = CaseTypeSchema(
        fields=(CaseTypeField(key="title", label="Title", type="text"),),
        document_classifications=(),
        rules=(clause,),
    )
    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(schema)
    assert "depth" in str(excinfo.value).lower()


__all__ = [
    "test_draft_factory__constructs_immutable_draft_with_defaults",
]
