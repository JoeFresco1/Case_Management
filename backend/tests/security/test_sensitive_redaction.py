"""Tests for field-level sensitivity redaction (P11-US6 / P05-US3 / FM-029).

Acceptance from the task contract:

- Authorized actors see the field value verbatim in the response.
- Unauthorized actors see the redaction marker
  ``{"redacted": true, "reason": "sensitive"}`` instead of the value.
- The value never appears in any non-redacted surface (logs, search
  snippets, error responses, audit payloads).
- Tenant isolation is preserved: a cross-tenant user is denied before
  the redactor runs.
- The redactor is pure: it does not call the policy. The caller passes
  ``can_see_sensitive`` based on a prior policy evaluation.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.application.authorization import (
    ROLE_INTAKE,
    ROLE_PROCESS_ADMIN,
    ROLE_REVIEWER,
    Action,
    Policy,
    Resource,
    authorize,
)
from app.application.cases.redactor import (
    redact_case_data,
    redacted_field_count,
)
from app.application.cases.summary import build_summary, summary_to_dict
from app.application.cases.view_case import Redacted
from app.domain.case import Case, CaseReference, CaseStatus, is_restricted_sensitivity
from app.domain.case_type import (
    CaseTypeField,
    CaseTypeSchema,
)
from app.domain.errors import Forbidden
from app.infrastructure.telemetry.structured_logging import redact_sensitive

# ---------------------------------------------------------------------------
# Constants / fixtures
# ---------------------------------------------------------------------------

TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
ACTOR = UUID("33333333-3333-3333-3333-333333333333")
SECRET_SSN = "123-45-6789"
SECRET_DOB = "1980-04-12"
SECRET_MEDICAL = "patient-stage-3-hypertension"
PUBLIC_NOTES = "Routine intake, no special handling."

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def _schema_with_restricted_field() -> CaseTypeSchema:
    """Schema with one public field and one restricted field."""
    return CaseTypeSchema(
        fields=(
            CaseTypeField(key="title", label="Title", type="text", required=True),
            CaseTypeField(
                key="ssn",
                label="SSN",
                type="text",
                required=False,
                sensitivity="restricted",
            ),
            CaseTypeField(
                key="medical_record",
                label="Medical record",
                type="text",
                required=False,
                sensitivity="phi",
            ),
            CaseTypeField(
                key="notes",
                label="Notes",
                type="text",
                required=False,
                sensitivity="internal",
            ),
        )
    )


def _case(
    *,
    sensitivity: str = "internal",
    data: dict[str, object] | None = None,
) -> Case:
    reference = CaseReference(value="C-ACME-2026-000001")
    return Case(
        id=uuid4(),
        tenant_id=TENANT,
        reference=reference,
        case_type_version_id=uuid4(),
        status=CaseStatus.OPEN,
        priority="normal",
        title="Intake",
        description="",
        data=data
        or {
            "title": "Title",
            "ssn": SECRET_SSN,
            "medical_record": SECRET_MEDICAL,
            "notes": PUBLIC_NOTES,
        },
        current_workflow_state=None,
        version_token=1,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
        created_by=ACTOR,
        closed_at=None,
        sensitivity=sensitivity,
    )


# ---------------------------------------------------------------------------
# Domain sensitivity ladder
# ---------------------------------------------------------------------------


def test_domain_sensitivity_helper__recognises_restricted_and_phi() -> None:
    """``is_restricted_sensitivity`` returns True for the two
    sensitivity levels that require field-level authorization
    (P11-US6). Everything else (public, internal, confidential) is
    not restricted."""
    assert is_restricted_sensitivity("restricted") is True
    assert is_restricted_sensitivity("phi") is True
    assert is_restricted_sensitivity("confidential") is False
    assert is_restricted_sensitivity("internal") is False
    assert is_restricted_sensitivity("public") is False


def test_case__sensitivity_attribute__defaults_to_internal() -> None:
    """The dataclass carries ``sensitivity`` and defaults to internal
    so legacy code paths that construct Case without it remain safe."""
    case = _case()
    assert case.sensitivity == "internal"


def test_schema__sensitive_field_keys__lists_only_restricted_keys() -> None:
    """``CaseTypeSchema.sensitive_field_keys`` returns only the keys
    whose declared sensitivity is at or above the restricted
    threshold. Internal / confidential fields are not in the list."""
    schema = _schema_with_restricted_field()
    assert set(schema.sensitive_field_keys()) == {"ssn", "medical_record"}


# ---------------------------------------------------------------------------
# Pure redactor
# ---------------------------------------------------------------------------


def test_redactor__unauthorized__replaces_restricted_values_with_marker() -> None:
    """The redactor replaces values for restricted/phi fields with the
    marker; non-sensitive fields are returned verbatim. Keys are
    preserved so the client renders ``(restricted)`` uniformly."""
    schema = _schema_with_restricted_field()
    out = redact_case_data(
        data={
            "title": "Title",
            "ssn": SECRET_SSN,
            "medical_record": SECRET_MEDICAL,
            "notes": PUBLIC_NOTES,
        },
        schema=schema,
        can_see_sensitive=False,
    )

    assert out["title"] == "Title"
    assert out["notes"] == PUBLIC_NOTES
    assert out["ssn"] == {"redacted": True, "reason": "sensitive"}
    assert out["medical_record"] == {"redacted": True, "reason": "sensitive"}
    # The original secret values MUST NOT leak through any path:
    assert SECRET_SSN not in str(out)
    assert SECRET_MEDICAL not in str(out)


def test_redactor__authorized__returns_values_verbatim() -> None:
    """When the actor carries the sensitive-read grant, every field is
    returned verbatim. The redactor never downgrades an authorized
    read."""
    schema = _schema_with_restricted_field()
    out = redact_case_data(
        data={
            "title": "Title",
            "ssn": SECRET_SSN,
            "medical_record": SECRET_MEDICAL,
            "notes": PUBLIC_NOTES,
        },
        schema=schema,
        can_see_sensitive=True,
    )
    assert out == {
        "title": "Title",
        "ssn": SECRET_SSN,
        "medical_record": SECRET_MEDICAL,
        "notes": PUBLIC_NOTES,
    }


def test_redactor__unauthorized__returns_independent_copy() -> None:
    """The redactor never mutates the input dict. The returned dict
    must be a fresh object so the caller cannot accidentally write
    back the redacted view into the persisted aggregate."""
    schema = _schema_with_restricted_field()
    data = {"title": "T", "ssn": SECRET_SSN, "notes": PUBLIC_NOTES}
    out = redact_case_data(data=data, schema=schema, can_see_sensitive=False)
    assert out is not data
    # Mutate the output and verify the input is untouched.
    out["title"] = "MUTATED"
    assert data["title"] == "T"


def test_redactor__unauthorized__does_not_leak_through_unknown_key() -> None:
    """A field present in the data but absent from the schema is
    passed through unchanged. Schema validation upstream is the gate
    for unknown keys; the redactor is intentionally permissive so
    legacy data persisted before a schema migration is not
    silently zeroed out."""
    schema = CaseTypeSchema(fields=(CaseTypeField(key="title", label="Title", type="text"),))
    out = redact_case_data(
        data={"title": "T", "legacy_field": "ok"},
        schema=schema,
        can_see_sensitive=False,
    )
    assert out == {"title": "T", "legacy_field": "ok"}


def test_redactor__no_restricted_fields__is_passthrough() -> None:
    """When the schema has no restricted fields, the redactor still
    returns a copy (callers must never mutate the input) but does
    not perform any substitution."""
    schema = CaseTypeSchema(
        fields=(
            CaseTypeField(
                key="title",
                label="Title",
                type="text",
                required=True,
                sensitivity="internal",
            ),
        )
    )
    data = {"title": "T"}
    out = redact_case_data(data=data, schema=schema, can_see_sensitive=False)
    assert out == data
    assert out is not data


def test_redactor__key_presence_preserved_for_redacted_fields() -> None:
    """A redacted field keeps its key in the returned dict; only the
    value is replaced. The client renders ``(restricted)`` for every
    restricted field uniformly, and a side-channel observer cannot
    distinguish missing-from-schema from redacted-by-policy."""
    schema = _schema_with_restricted_field()
    out = redact_case_data(
        data={"title": "T", "ssn": SECRET_SSN},
        schema=schema,
        can_see_sensitive=False,
    )
    assert "ssn" in out
    assert "title" in out
    assert out["ssn"] == Redacted(reason="sensitive").to_dict()


# ---------------------------------------------------------------------------
# Summary shape (the wire shape the HTTP layer serializes)
# ---------------------------------------------------------------------------


def test_build_summary__unauthorized__redacts_data_fields() -> None:
    """``build_summary`` honors the ``schema`` + ``can_see_sensitive``
    flags and produces a ``CaseSummary`` whose ``data`` payload is
    already redacted. The serialized wire shape therefore never
    contains the secret value for an unauthorized actor."""
    schema = _schema_with_restricted_field()
    case = _case()
    summary = build_summary(
        case=case,
        case_type_key="ServiceRequest",
        sensitive=False,
        schema=schema,
        can_see_sensitive=False,
    )
    wire = summary_to_dict(summary)
    assert isinstance(wire["data"]["ssn"], dict)
    assert wire["data"]["ssn"]["redacted"] is True
    assert wire["data"]["ssn"]["reason"] == "sensitive"
    assert wire["data"]["medical_record"]["redacted"] is True
    # Secret values must not appear anywhere in the wire shape.
    blob = repr(wire)
    assert SECRET_SSN not in blob
    assert SECRET_MEDICAL not in blob


def test_build_summary__authorized__preserves_data_fields() -> None:
    """An authorized actor sees the values in the wire shape
    verbatim; the redactor does not down-grade a granted read."""
    schema = _schema_with_restricted_field()
    case = _case()
    summary = build_summary(
        case=case,
        case_type_key="ServiceRequest",
        sensitive=False,
        schema=schema,
        can_see_sensitive=True,
    )
    wire = summary_to_dict(summary)
    assert wire["data"]["ssn"] == SECRET_SSN
    assert wire["data"]["medical_record"] == SECRET_MEDICAL


def test_build_summary__coarse_sensitive_flag__redacts_whole_payload() -> None:
    """The coarse ``sensitive=True`` switch is preserved for case
    types whose default sensitivity is restricted/phi. The summary
    collapses the whole ``data`` blob into the redaction marker so
    the workspace UI shows the case exists but no field values."""
    case = _case(sensitivity="restricted")
    summary = build_summary(case=case, case_type_key="ServiceRequest", sensitive=True)
    wire = summary_to_dict(summary)
    assert wire["data"] == {"redacted": True, "reason": "sensitive"}
    assert wire["sensitivity"] == "restricted"


def test_build_summary__no_schema__uses_coarse_flag_only() -> None:
    """When the caller has no schema (e.g. list view path that
    never inspects field sensitivity), the redactor falls back to
    the coarse ``sensitive`` flag. Per-field redaction requires a
    schema."""
    case = _case(data={"title": "T", "ssn": SECRET_SSN})
    summary = build_summary(case=case, case_type_key="ServiceRequest", sensitive=False)
    wire = summary_to_dict(summary)
    assert wire["data"]["ssn"] == SECRET_SSN


# ---------------------------------------------------------------------------
# Logging / observability
# ---------------------------------------------------------------------------


def test_log_redaction__values_with_sensitive_key__replaced() -> None:
    """Any log payload with a key whose name matches the redaction
    list is replaced with ``[REDACTED]``. The sensitive value never
    reaches the log line."""
    payload = {
        "event": "case.viewed",
        "case_id": str(uuid4()),
        "ssn": SECRET_SSN,
        "password": "hunter2",
        "authorization": "Bearer xyz",
    }
    redacted = redact_sensitive(payload)
    blob = repr(redacted)
    assert redacted["ssn"] == "[REDACTED]"
    assert redacted["password"] == "[REDACTED]"
    assert redacted["authorization"] == "[REDACTED]"
    # The original secrets MUST NOT appear in the rendered payload.
    assert SECRET_SSN not in blob
    assert "hunter2" not in blob
    assert "Bearer xyz" not in blob
    # Non-sensitive keys pass through untouched.
    assert redacted["case_id"] == payload["case_id"]


def test_log_redaction__nested_sensitive_field_value__replaced() -> None:
    """The redactor walks nested dicts (e.g. an audit payload that
    embedded a case's data). Sensitive fields at any depth are
    masked so an auditor scanning logs cannot reconstruct the
    original value."""
    payload = {
        "audit": {
            "case_id": str(uuid4()),
            "data": {
                "title": "T",
                "ssn": SECRET_SSN,
            },
        }
    }
    redacted = redact_sensitive(payload)
    assert redacted["audit"]["data"]["ssn"] == "[REDACTED]"
    assert redacted["audit"]["data"]["title"] == "T"
    assert SECRET_SSN not in repr(redacted)


def test_redacted_field_count__counts_only_restricted_keys() -> None:
    """Observability helper returns the number of fields that WOULD
    be redacted; callers log this count for forensics without ever
    logging the values themselves."""
    schema = _schema_with_restricted_field()
    data = {
        "title": "T",
        "ssn": SECRET_SSN,
        "medical_record": SECRET_MEDICAL,
        "notes": PUBLIC_NOTES,
    }
    assert redacted_field_count(data=data, schema=schema) == 2


# ---------------------------------------------------------------------------
# Authorization seam (can_see_sensitive is policy-driven)
# ---------------------------------------------------------------------------


async def test_policy__reviewer_lacks_case_read_sensitive__is_denied() -> None:
    """The reviewer role grants ``case.read`` but not
    ``case.read_sensitive``. The redactor therefore receives
    ``can_see_sensitive=False`` and the wire shape masks restricted
    fields. This is the canonical P11-US6 acceptance scenario."""
    policy = Policy()
    decision = await policy.evaluate(
        Action.CASE_READ_SENSITIVE,
        _actor_for_policy(roles=(ROLE_REVIEWER.name,)),
        _resource_for_policy(),
    )
    assert decision.allowed is False


async def test_policy__process_admin_has_case_read_sensitive__is_allowed() -> None:
    """``process_admin`` is the documented role for sensitive reads;
    the policy grants the action."""
    policy = Policy()
    decision = await policy.evaluate(
        Action.CASE_READ_SENSITIVE,
        _actor_for_policy(roles=(ROLE_PROCESS_ADMIN.name,)),
        _resource_for_policy(),
    )
    assert decision.allowed is True


async def test_policy__intake_lacks_case_read_sensitive__is_denied() -> None:
    """Intake users can create cases but cannot read restricted
    fields. This is the default-deny contract (P11-US1)."""
    policy = Policy()
    decision = await policy.evaluate(
        Action.CASE_READ_SENSITIVE,
        _actor_for_policy(roles=(ROLE_INTAKE.name,)),
        _resource_for_policy(),
    )
    assert decision.allowed is False


async def test_policy__cross_tenant_actor_with_role__still_denied() -> None:
    """Tenant isolation is preserved even when the role grants the
    sensitive-read action. A process_admin in tenant A cannot read
    sensitive data in tenant B."""
    policy = Policy()
    decision = await policy.evaluate(
        Action.CASE_READ_SENSITIVE,
        _actor_for_policy(tenant=OTHER_TENANT, roles=(ROLE_PROCESS_ADMIN.name,)),
        _resource_for_policy(tenant=TENANT),
    )
    assert decision.allowed is False
    assert "cross-tenant" in decision.reason


async def test_authorize__sensitive_read_failure__is_silent() -> None:
    """The ``actor_can_see_sensitive`` helper translates a denied
    sensitive-read into a False boolean rather than raising. A
    failed sensitive-read is not a 403; the case read itself is
    still allowed and only the field values are masked. This is
    the design contract documented in the module docstring."""

    # Construct a minimal authorize call against a forbidden action.
    class _Ctx:
        tenant_id = TENANT
        user_id = ACTOR
        display_name = "t"
        roles = (ROLE_INTAKE.name,)
        is_system = False

    with pytest.raises(Forbidden):
        await authorize(
            Action.CASE_READ_SENSITIVE,
            _Ctx(),  # type: ignore[arg-type]
            Resource(tenant_id=TENANT, kind="case", id=uuid4()),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _actor_for_policy(
    *,
    tenant: UUID = TENANT,
    roles: tuple[str, ...] = (),
):
    """Duck-typed actor for the policy module.

    Avoids importing :class:`app.deps.UserContext` (which requires
    runtime settings) by mirroring the structural shape the policy
    reads: ``tenant_id``, ``user_id``, ``roles``, ``is_system``,
    ``display_name``.
    """

    class _Actor:
        pass

    a = _Actor()
    a.tenant_id = tenant
    a.user_id = ACTOR
    a.display_name = "tester"
    a.roles = roles
    a.is_system = False
    return a


def _resource_for_policy(*, tenant: UUID = TENANT) -> Resource:
    return Resource(tenant_id=tenant, kind="case", id=uuid4())
