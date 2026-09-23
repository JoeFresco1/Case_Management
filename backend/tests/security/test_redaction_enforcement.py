"""Redaction enforcement across responses, logs, events, and snippets (FM-011).

Covers P11-US6 / P05-US3 / P05-US7: unauthorized actors see reference-id
markers (``ref`` = field key), never sensitive values, on every surface:

- case responses (``build_summary`` + list-view redaction path),
- logs (``redact_sensitive`` static list, extended for PHI needles),
- audit/event payloads (``redact_event_payload`` + ``edit_case`` diff),
- search snippets (``redact_search_snippet`` + SQL adapter projection).

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5. Structure is ``// given`` / ``// when`` / ``// then``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.application.audit.view_timeline import _redact_payload
from app.application.cases.edit_case import _redacted_diff_payload
from app.application.cases.summary import build_summary, summary_to_dict
from app.domain.case import Case, CaseReference, CaseStatus, FieldDiff, FieldSnapshot
from app.domain.case_type import CaseTypeField, CaseTypeSchema
from app.domain.events import validate_payload
from app.domain.sensitivity import (
    classify,
    redact_event_payload,
    redact_log_payload,
    redact_search_snippet,
)
from app.infrastructure.persistence.search.sql_search_provider import _row_to_hit
from app.infrastructure.telemetry.structured_logging import _REDACT_KEYS, redact_sensitive
from app.interfaces.http.errors import error_response

SECRET_SSN = "123-45-6789"
SECRET_MEDICAL = "patient-stage-3-hypertension"
SECRET_DOB = "1980-04-12"
PUBLIC_NOTES = "Routine intake, no special handling."

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def _schema() -> CaseTypeSchema:
    """Schema with public, restricted, and phi fields."""
    return CaseTypeSchema(
        fields=(
            CaseTypeField(key="title", label="Title", type="text", required=True),
            CaseTypeField(
                key="ssn", label="SSN", type="text", required=False, sensitivity="restricted"
            ),
            CaseTypeField(
                key="medical_record",
                label="Medical record",
                type="text",
                required=False,
                sensitivity="phi",
            ),
            CaseTypeField(key="dob", label="DOB", type="text", required=False, sensitivity="phi"),
            CaseTypeField(
                key="notes", label="Notes", type="text", required=False, sensitivity="internal"
            ),
        )
    )


def _data() -> dict[str, Any]:
    return {
        "title": "Title",
        "ssn": SECRET_SSN,
        "medical_record": SECRET_MEDICAL,
        "dob": SECRET_DOB,
        "notes": PUBLIC_NOTES,
    }


def _case(**overrides: Any) -> Case:
    kwargs: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "reference": CaseReference(value="C-ACME-2026-000001"),
        "case_type_version_id": uuid4(),
        "status": CaseStatus.OPEN,
        "priority": "normal",
        "title": "Intake",
        "description": "",
        "data": _data(),
        "current_workflow_state": None,
        "version_token": 1,
        "created_at": FIXED_NOW,
        "updated_at": FIXED_NOW,
        "created_by": uuid4(),
        "closed_at": None,
        "sensitivity": "internal",
    }
    kwargs.update(overrides)
    return Case(**kwargs)


# ---------------------------------------------------------------------------
# Case responses
# ---------------------------------------------------------------------------


def test_build_summary__unauthorized__masks_values_by_key__no_leak() -> None:
    """Unauthorized wire responses carry markers keyed by field, no values."""
    # // given a case with secrets and a schema declaring sensitivity
    case = _case()
    # // when an unauthorized actor reads the summary
    summary = build_summary(
        case=case,
        case_type_key="ServiceRequest",
        sensitive=False,
        schema=_schema(),
        can_see_sensitive=False,
    )
    wire = summary_to_dict(summary)
    # // then each restricted key is present (reference id = the key)
    # // and no secret content appears anywhere in the wire shape
    assert set(wire["data"]) == set(_data())
    assert wire["data"]["ssn"] == {"redacted": True, "reason": "sensitive"}
    assert wire["data"]["medical_record"] == {"redacted": True, "reason": "sensitive"}
    assert wire["data"]["dob"] == {"redacted": True, "reason": "sensitive"}
    assert wire["data"]["notes"] == PUBLIC_NOTES
    blob = json.dumps(wire, default=str)
    assert SECRET_SSN not in blob
    assert SECRET_MEDICAL not in blob
    assert SECRET_DOB not in blob


def test_build_summary__restricted_case__collapses_payload__no_leak() -> None:
    """A restricted case collapses structured data to one marker."""
    # // given a restricted-sensitivity case
    case = _case(sensitivity="restricted")
    # // when any actor reads it through the coarse flag
    wire = summary_to_dict(build_summary(case=case, case_type_key="ServiceRequest", sensitive=True))
    # // then no field value survives; the case is still referenceable
    assert wire["data"] == {"redacted": True, "reason": "sensitive"}
    assert wire["sensitivity"] == "restricted"
    assert SECRET_SSN not in json.dumps(wire, default=str)


def test_build_summary__authorized__sees_values_verbatim() -> None:
    """The sensitive-read grant restores verbatim values (no downgrade)."""
    # // given an authorized actor
    case = _case()
    # // when redacting with the grant
    wire = summary_to_dict(
        build_summary(
            case=case,
            case_type_key="ServiceRequest",
            sensitive=False,
            schema=_schema(),
            can_see_sensitive=True,
        )
    )
    # // then values pass through (the grant is authoritative)
    assert wire["data"]["ssn"] == SECRET_SSN
    assert wire["data"]["medical_record"] == SECRET_MEDICAL


# ---------------------------------------------------------------------------
# Logs (static key list, extended for PHI needles)
# ---------------------------------------------------------------------------


def test_redaction_list__covers_phi_needles__no_drift() -> None:
    """The telemetry list covers the domain PHI needles (FM-011)."""
    # // given the static redaction list
    lowered = tuple(key.lower() for key in _REDACT_KEYS)
    # // when checking the classification/PHI vocabulary
    # // then every needle matches by substring
    for needle in (
        "ssn",
        "dob",
        "birth",
        "address",
        "phone",
        "email",
        "passport",
        "license",
        "tax_id",
        "medical",
        "token",
        "secret",
        "password",
    ):
        assert any(needle in entry for entry in lowered), f"missing '{needle}'"


def test_log_redaction__phi_keys__replaced_without_content_leak() -> None:
    """Log lines mask PHI keys at any depth; values never render."""
    # // given a log payload nesting PHI-shaped keys
    payload = {
        "event": "case.viewed",
        "nested": {"dob": SECRET_DOB, "phone": "+1-555-0100", "ok": "keep"},
        "medical_record": SECRET_MEDICAL,
    }
    # // when redacting for logs
    redacted = redact_sensitive(payload)
    # // then markers replace values and content never renders
    blob = json.dumps(redacted, default=str)
    assert SECRET_DOB not in blob
    assert SECRET_MEDICAL not in blob
    assert "+1-555-0100" not in blob
    assert redacted["nested"]["ok"] == "keep"


def test_error_response__sensitive_details__redacted_without_leak() -> None:
    """Error bodies redact sensitive detail keys before leaving the process."""
    # // given error details echoing a rejected payload
    response = error_response(
        code="validation_failed",
        message="invalid",
        status_code=422,
        correlation_id="corr-1",
        details={"ssn": SECRET_SSN, "field": "ssn"},
    )
    # // when rendering the wire body
    blob = response.body.decode()
    # // then the value is masked and only the key identity survives
    assert SECRET_SSN not in blob
    assert "[REDACTED]" in blob


# ---------------------------------------------------------------------------
# Events / audit payloads
# ---------------------------------------------------------------------------


def test_event_payload__sensitive_keys__replaced_by_ref__no_leak() -> None:
    """Audit/event payloads carry ``ref`` markers, never content."""
    # // given an event payload embedding case data
    classification = classify(_schema())
    # // when redacting for the event store
    redacted = redact_event_payload({"event_type": "CaseUpdated", "data": _data()}, classification)
    # // then sensitive entries are ref markers and secrets never render
    assert redacted["data"]["ssn"] == {"redacted": True, "reason": "sensitive", "ref": "ssn"}
    assert redacted["data"]["medical_record"]["ref"] == "medical_record"
    assert redacted["data"]["notes"] == PUBLIC_NOTES
    blob = json.dumps(redacted, default=str)
    assert SECRET_SSN not in blob
    assert SECRET_MEDICAL not in blob


def test_log_payload__public_key_with_sensitive_value__passes_through() -> None:
    """Redaction is key-based: public keys pass through untouched."""
    # // given a public key holding a sensitive-looking value
    classification = classify(_schema())
    # // when redacting for logs
    out = redact_log_payload({"notes": SECRET_SSN}, classification)
    # // then the value survives (keys, not content, drive redaction)
    assert out == {"notes": SECRET_SSN}


def test_edit_audit_diff__sensitive_data__redacted_by_ref__no_leak() -> None:
    """The stored ``CaseUpdated`` diff masks sensitive data by ref."""
    # // given a before/after diff touching restricted fields
    before = FieldSnapshot(title="T", description="", priority="normal", data=_data())
    after_data = dict(_data())
    after_data["notes"] = "edited"
    after = FieldSnapshot(title="T", description="", priority="normal", data=after_data)
    diff = FieldDiff(before=before, after=after, changed_fields=frozenset({"data"}))
    # // when rendering the durable audit payload
    payload = _redacted_diff_payload(diff, _schema())
    # // then sensitive entries are ref markers on both snapshots
    for section in ("before", "after"):
        assert payload[section]["data"]["ssn"]["ref"] == "ssn"
        assert payload[section]["data"]["medical_record"]["ref"] == "medical_record"
        assert payload[section]["data"]["notes"] in (PUBLIC_NOTES, "edited")
    assert payload["changed_fields"] == ["data"]
    blob = json.dumps(payload, default=str)
    assert SECRET_SSN not in blob
    assert SECRET_MEDICAL not in blob


def test_domain_event__phi_key__rejected_before_outbox() -> None:
    """PHI-named keys never reach the outbox (fail-closed at the boundary)."""
    # // given a payload key that looks like PHI
    # // when validating against the catalog
    # // then validation rejects it before any row is staged
    with pytest.raises(ValueError, match="PHI"):
        validate_payload("CaseCreated", {"case_id": "c-1", "ssn": SECRET_SSN})


def test_timeline_payload__unsafe_keys__masked__no_leak() -> None:
    """Timeline reads mask non-allowlisted keys so values never render."""
    # // given a stored payload that accidentally embedded case data
    stored = {"case_reference": "C-ACME-2026-000001", "data": {"ssn": SECRET_SSN}}
    # // when projecting the timeline entry
    redacted, was_redacted = _redact_payload(stored)
    # // then the unsafe key is masked and the leak is flagged
    assert was_redacted is True
    assert redacted["case_reference"] == "C-ACME-2026-000001"
    assert redacted["data"] == "***"
    assert SECRET_SSN not in json.dumps(redacted, default=str)


# ---------------------------------------------------------------------------
# Search snippets
# ---------------------------------------------------------------------------


def test_search_snippet__unauthorized__masked_by_ref__no_leak() -> None:
    """Search excerpts for restricted keys become ref markers."""
    # // given a snippet with excerpts per field
    classification = classify(_schema())
    # // when an unauthorized actor receives it
    out = redact_search_snippet(
        {"title": "Title match", "ssn": SECRET_SSN, "notes": PUBLIC_NOTES},
        classification,
        can_see_sensitive=False,
    )
    # // then the sensitive excerpt is a ref marker; content never renders
    assert out["ssn"] == {"redacted": True, "reason": "sensitive", "ref": "ssn"}
    assert out["title"] == "Title match"
    assert SECRET_SSN not in json.dumps(out, default=str)


def test_sql_snippet__unauthorized__masked_by_ref__no_leak() -> None:
    """The SQL adapter projects ref markers for sensitive snippet keys."""
    # // given a row whose data carries a restricted value
    row = SimpleNamespace(
        id=uuid4(),
        reference="C-TEST-2026-000001",
        priority="normal",
        status="open",
        title="T",
        data=json.dumps({"notes": PUBLIC_NOTES, "ssn": SECRET_SSN}),
    )
    spec = SimpleNamespace(
        searchable_field_keys=("notes", "ssn"),
        sensitive_field_keys=("ssn",),
        can_see_sensitive=False,
    )
    # // when projecting the hit
    hit = _row_to_hit(row, spec)  # type: ignore[arg-type]
    # // then the marker names the field and the value never renders
    assert hit.data_snippet["ssn"] == {"redacted": True, "reason": "sensitive", "ref": "ssn"}
    assert hit.data_snippet["notes"] == PUBLIC_NOTES
    assert SECRET_SSN not in json.dumps(hit.data_snippet, default=str)
