"""Domain tests for ``app.domain.reference_format`` (FM-009 / P01-US9).

Pure-domain tests — no I/O, no fakes, no DB. They cover:

1. ``validate_template`` accepts the canonical tenant/year/sequence
   template and the case-type-aware template from the acceptance
   scenario (``C-{TYPE}-{YYYY}-{SEQ}``).
2. ``validate_template`` rejects: empty input, unknown tokens,
   unbalanced braces, missing ``{SEQ}``, out-of-range ``SEQ`` pad
   width, and over-length templates.
3. ``ReferenceFormat.render`` substitutes each known token correctly,
   including the optional ``{SEQ:Nd}`` pad width, and enforces the
   per-argument character validation (the validator is defence-in-depth
   on the render path so a misconfigured template cannot reach the
   wire with garbage).
4. Acceptance scenario: an *existing* case reference is unchanged by
   a format change. The repository layer is responsible for not
   retroactively rewriting; at the domain layer the property is
   captured by the immutable ``ReferenceFormat`` entity and the
   ``CaseReference`` invariant (already covered by
   ``tests/domain/test_case.py`` — this test asserts the cross-module
   contract).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.domain.case import Case, CaseReference, CaseStatus
from app.domain.errors import InvariantViolation, ValidationFailed
from app.domain.reference_format import (
    MAX_RENDERED_LENGTH,
    PLATFORM_DEFAULT_TEMPLATE,
    ReferenceFormat,
    platform_default_template,
    validate_template,
)

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
BETA_TENANT = UUID("22222222-2222-2222-2222-222222222222")
ACTOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1. validate_template: accept canonical shapes
# ---------------------------------------------------------------------------


def test_validate_template__accepts_platform_default() -> None:
    """The platform default is valid — first-slice data must keep working."""
    validate_template(PLATFORM_DEFAULT_TEMPLATE)  # must not raise


def test_validate_template__accepts_type_aware_format() -> None:
    """Acceptance scenario template ``C-{TYPE}-{YYYY}-{SEQ}`` is valid."""
    validate_template("C-{TYPE}-{YYYY}-{SEQ}")


def test_validate_template__accepts_optional_seq_pad() -> None:
    """``{SEQ:08d}`` and the bare ``{SEQ}`` are both valid."""
    validate_template("PR-{TENANT}-{SEQ:08}")
    validate_template("PR-{TENANT}-{SEQ}")


def test_validate_template__accepts_yy_short_year() -> None:
    """``{YY}`` is allowed for compact two-digit year projections."""
    validate_template("MRN-{YY}-{SEQ}")


# ---------------------------------------------------------------------------
# 2. validate_template: reject malformed shapes
# ---------------------------------------------------------------------------


def test_validate_template__rejects_empty() -> None:
    """Empty and whitespace-only templates are rejected with 422-mapped error."""
    with pytest.raises(ValidationFailed):
        validate_template("")
    with pytest.raises(ValidationFailed):
        validate_template("   ")


def test_validate_template__rejects_unknown_token() -> None:
    """Unknown tokens (e.g. ``{USERNAME}``) are rejected.

    Why this matters: a closed token vocabulary stops an admin from
    smuggling sensitive values into a reference prefix; the validator
    is the gate that enforces the closure.
    """
    with pytest.raises(ValidationFailed) as ei:
        validate_template("C-{USERNAME}-{SEQ}")
    assert "USERNAME" in str(ei.value)


def test_validate_template__rejects_missing_seq() -> None:
    """A reference without a sequence is rejected — it would be a category."""
    with pytest.raises(ValidationFailed) as ei:
        validate_template("C-{TENANT}-{YYYY}")
    assert "{SEQ}" in str(ei.value)


def test_validate_template__rejects_unbalanced_braces() -> None:
    """Stray braces (open without close, or vice versa) are rejected."""
    with pytest.raises(ValidationFailed):
        validate_template("C-{TENANT")
    with pytest.raises(ValidationFailed):
        validate_template("C-TENANT}")


def test_validate_template__rejects_seq_pad_out_of_range() -> None:
    """Pad width must be between 4 and 12 (inclusive)."""
    with pytest.raises(ValidationFailed):
        validate_template("C-{TENANT}-{SEQ:3}")
    with pytest.raises(ValidationFailed):
        validate_template("C-{TENANT}-{SEQ:99}")


def test_validate_template__rejects_oversized_template() -> None:
    """A template longer than ``MAX_RENDERED_LENGTH`` is rejected."""
    huge = "X" * (MAX_RENDERED_LENGTH + 1) + "{SEQ}"
    with pytest.raises(ValidationFailed):
        validate_template(huge)


# ---------------------------------------------------------------------------
# 3. ReferenceFormat.render: substitution + per-arg validation
# ---------------------------------------------------------------------------


def _format(template: str, *, tenant: UUID = ACME_TENANT) -> ReferenceFormat:
    return ReferenceFormat(tenant_id=tenant, template=template)


def test_render__substitutes_all_known_tokens() -> None:
    """Render a full template and check every token substitution."""
    fmt = _format("C-{TYPE}-{YYYY}-{SEQ}")
    rendered = fmt.render(
        tenant_short="ACME",
        type_key="SERVICEREQUEST",
        now=FIXED_NOW,
        sequence=42,
    )
    assert rendered == "C-SERVICEREQUEST-2026-000042"


def test_render__substitutes_seq_pad() -> None:
    """``{SEQ:08}`` zero-pads to 8 digits; the default pad is 6."""
    fmt_default = _format("PR-{TENANT}-{SEQ}")
    fmt_wide = _format("PR-{TENANT}-{SEQ:08}")
    seq = 12345
    rendered_default = fmt_default.render(
        tenant_short="ACME", type_key="X", now=FIXED_NOW, sequence=seq
    )
    rendered_wide = fmt_wide.render(tenant_short="ACME", type_key="X", now=FIXED_NOW, sequence=seq)
    # Default pad is 6 digits — 12345 fits and is zero-padded to 6.
    assert rendered_default.endswith("-012345")
    # Explicit 8-digit pad zero-pads to 8.
    assert rendered_wide.endswith("-00012345")


def test_render__substitutes_yy_short_year() -> None:
    """``{YY}`` emits the two-digit year."""
    fmt = _format("MRN-{YY}-{SEQ}")
    rendered = fmt.render(
        tenant_short="ACME",
        type_key="X",
        now=FIXED_NOW,
        sequence=1,
    )
    assert rendered == "MRN-26-000001"


def test_render__rejects_bad_tenant_short() -> None:
    """A lowercase or oversized tenant short key is rejected at render time."""
    fmt = _format("C-{TENANT}-{YYYY}-{SEQ}")
    with pytest.raises(InvariantViolation):
        fmt.render(
            tenant_short="acme",  # lowercase: not in the allowed charset
            type_key="X",
            now=FIXED_NOW,
            sequence=1,
        )
    with pytest.raises(InvariantViolation):
        fmt.render(
            tenant_short="A" * 17,  # too long
            type_key="X",
            now=FIXED_NOW,
            sequence=1,
        )


def test_render__rejects_bad_type_key() -> None:
    """Lowercase or non-identifier characters in the type key are rejected."""
    fmt = _format("C-{TYPE}-{SEQ}")
    with pytest.raises(InvariantViolation):
        fmt.render(
            tenant_short="ACME",
            type_key="servicerequest",  # lowercase
            now=FIXED_NOW,
            sequence=1,
        )


def test_render__rejects_negative_sequence() -> None:
    """A negative sequence cannot render; ``{SEQ}`` always zero-pads."""
    fmt = _format("C-{TENANT}-{SEQ}")
    with pytest.raises(InvariantViolation):
        fmt.render(
            tenant_short="ACME",
            type_key="X",
            now=FIXED_NOW,
            sequence=-1,
        )


# ---------------------------------------------------------------------------
# 4. ReferenceFormat is immutable (frozen dataclass)
# ---------------------------------------------------------------------------


def test_reference_format__is_frozen() -> None:
    """Mutating an instantiated ``ReferenceFormat`` must raise."""
    fmt = _format("C-{TENANT}-{SEQ}")
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        fmt.template = "OTHER"  # type: ignore[misc]


def test_reference_format__rejects_invalid_template_in_ctor() -> None:
    """The constructor itself runs the validator — bad templates fail fast."""
    with pytest.raises(ValidationFailed):
        ReferenceFormat(tenant_id=ACME_TENANT, template="C-{NOPE}")


# ---------------------------------------------------------------------------
# 5. Acceptance scenario: existing case reference is unchanged after
#    a format change. This is the user-facing property FM-009 promises
#    ("Administrators configure validated human-readable reference
#    formats without changing immutable internal identity").
# ---------------------------------------------------------------------------


def test_existing_case_reference__unchanged_by_format_change() -> None:
    """Existing ``Case.reference`` is preserved when an admin changes the
    tenant's reference format template.

    Why at the domain layer: the immutable internal identity is the
    Case's UUID plus the persisted ``CaseReference``. The format
    template only governs **future** emissions. This test constructs a
    Case with the legacy ``C-{TENANT}-{YYYY}-{SEQ}`` reference and
    asserts that after the admin publishes a new ``C-{TYPE}-{YYYY}-{SEQ}``
    template, the existing Case's reference is byte-identical.
    """
    legacy_ref = CaseReference(value="C-ACME-2026-000042")
    case = Case(
        id=uuid4(),
        tenant_id=ACME_TENANT,
        reference=legacy_ref,
        case_type_version_id=uuid4(),
        status=CaseStatus.OPEN,
        priority="normal",
        title="Legacy case",
        description="",
        data={},
        current_workflow_state=None,
        version_token=1,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
        created_by=ACTOR,
    )
    # Sanity: the legacy reference matches the legacy format shape.
    assert legacy_ref.value == "C-ACME-2026-000042"

    # Admin publishes a brand-new template. The reference of the
    # existing case must be unchanged — the format change does not
    # touch existing rows.
    new_format = ReferenceFormat(
        tenant_id=ACME_TENANT,
        template="C-{TYPE}-{YYYY}-{SEQ}",
    )
    # Render a future emission to confirm the new format is in effect.
    next_emission = new_format.render(
        tenant_short="ACME",
        type_key="SERVICEREQUEST",
        now=FIXED_NOW,
        sequence=43,
    )
    assert next_emission == "C-SERVICEREQUEST-2026-000043"

    # The existing case's reference is unchanged.
    assert case.reference is legacy_ref
    assert case.reference.value == "C-ACME-2026-000042"


# ---------------------------------------------------------------------------
# 6. Cross-tenant: per-tenant format isolation (Pillar P01 §2).
# ---------------------------------------------------------------------------


def test_per_tenant_format__is_isolated() -> None:
    """Two tenants with different templates do not affect each other."""
    acme = ReferenceFormat(tenant_id=ACME_TENANT, template="C-{TYPE}-{SEQ}")
    beta = ReferenceFormat(tenant_id=BETA_TENANT, template="PR-{TENANT}-{YYYY}-{SEQ}")
    now = FIXED_NOW
    assert acme.render(tenant_short="ACME", type_key="X", now=now, sequence=1) == "C-X-000001"
    assert (
        beta.render(tenant_short="BETA", type_key="X", now=now, sequence=1) == "PR-BETA-2026-000001"
    )


# ---------------------------------------------------------------------------
# 7. platform_default_template() function (defensive — the function exists
#    so tests can monkeypatch without mutating module globals).
# ---------------------------------------------------------------------------


def test_platform_default_template_function__matches_constant() -> None:
    """The function returns the same value as ``PLATFORM_DEFAULT_TEMPLATE``."""
    assert platform_default_template() == PLATFORM_DEFAULT_TEMPLATE
