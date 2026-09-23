"""Tests for sensitivity classification + reference-by-id redaction (FM-011).

Covers P11-US6, P05-US3/US7: administrators classify cases and fields
as sensitive; unauthorized responses, logs, events, and search snippets
redact values. Sensitive values are referenced by id (the ``ref`` field
key) never by content, and redaction is key-based, never content-based.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.case_type import CaseTypeField, CaseTypeSchema
from app.domain.errors import InvariantViolation
from app.domain.sensitivity import (
    SensitivityClassification,
    SensitivityLevel,
    classify,
    classify_keys,
    parse_level,
    redact_by_keys,
    redact_case_fields,
    redact_event_payload,
    redact_log_payload,
    redact_search_snippet,
    redacted_refs,
)

SECRET_SSN = "123-45-6789"
SECRET_MEDICAL = "patient-stage-3-hypertension"
PUBLIC_NOTES = "Routine intake, no special handling."


def _schema() -> CaseTypeSchema:
    """Schema with public, restricted, and phi fields."""
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


def _data() -> dict[str, Any]:
    return {
        "title": "Title",
        "ssn": SECRET_SSN,
        "medical_record": SECRET_MEDICAL,
        "notes": PUBLIC_NOTES,
    }


# ---------------------------------------------------------------------------
# Classification value objects
# ---------------------------------------------------------------------------


def test_parse_level__known_levels__returns_member() -> None:
    """Every ladder label parses to its enum member."""
    assert parse_level("public") is SensitivityLevel.PUBLIC
    assert parse_level("internal") is SensitivityLevel.INTERNAL
    assert parse_level("confidential") is SensitivityLevel.CONFIDENTIAL
    assert parse_level("restricted") is SensitivityLevel.RESTRICTED
    assert parse_level("phi") is SensitivityLevel.PHI


def test_parse_level__unknown_label__fails_closed() -> None:
    """Unknown labels raise instead of downgrading silently."""
    with pytest.raises(InvariantViolation):
        parse_level("top-secret")


def test_level__is_restricted__threshold_is_restricted_and_phi() -> None:
    """Only restricted/phi require the sensitive-read grant."""
    assert SensitivityLevel.RESTRICTED.is_restricted() is True
    assert SensitivityLevel.PHI.is_restricted() is True
    assert SensitivityLevel.CONFIDENTIAL.is_restricted() is False
    assert SensitivityLevel.INTERNAL.is_restricted() is False
    assert SensitivityLevel.PUBLIC.is_restricted() is False


def test_classify__schema_levels__maps_keys_without_values() -> None:
    """Classification maps key -> level from declarations, not values."""
    classification = classify(_schema())
    assert classification.level_for("ssn") is SensitivityLevel.RESTRICTED
    assert classification.level_for("medical_record") is SensitivityLevel.PHI
    assert classification.level_for("notes") is SensitivityLevel.INTERNAL
    assert classification.level_for("undeclared_legacy") is SensitivityLevel.INTERNAL
    assert set(classification.sensitive_keys()) == {"ssn", "medical_record"}


def test_classify_keys__explicit_map__builds_classification() -> None:
    """Tenants can seal a key -> level map without reopening schemas."""
    classification = classify_keys({"ssn": "restricted", "notes": "internal"})
    assert classification.is_sensitive("ssn") is True
    assert classification.is_sensitive("notes") is False


def test_classify__restricted_case_default__marks_all_keys_sensitive() -> None:
    """A restricted case default (P01-US4) treats every key as sensitive."""
    classification = classify(_schema(), case_sensitivity="restricted")
    assert classification.is_sensitive("notes") is True
    assert set(classification.sensitive_keys()) == {"title", "ssn", "medical_record", "notes"}


# ---------------------------------------------------------------------------
# Case fields
# ---------------------------------------------------------------------------


def test_redact_case_fields__unauthorized__replaces_values_with_ref_markers() -> None:
    """Restricted values become ref markers; public values pass through."""
    # // given a classification and case data with secrets
    classification = classify(_schema())
    # // when an unauthorized actor reads the fields
    out = redact_case_fields(_data(), classification, can_see_sensitive=False)
    # // then secrets are replaced by id-referencing markers
    assert out["title"] == "Title"
    assert out["notes"] == PUBLIC_NOTES
    assert out["ssn"] == {"redacted": True, "reason": "sensitive", "ref": "ssn"}
    assert out["medical_record"] == {
        "redacted": True,
        "reason": "sensitive",
        "ref": "medical_record",
    }
    blob = repr(out)
    assert SECRET_SSN not in blob
    assert SECRET_MEDICAL not in blob


def test_redact_case_fields__authorized__returns_values_verbatim() -> None:
    """A granted read never downgrades values."""
    # // given an authorized actor
    classification = classify(_schema())
    # // when redacting with the grant
    out = redact_case_fields(_data(), classification, can_see_sensitive=True)
    # // then values pass through verbatim
    assert out == _data()
    assert out is not _data()


def test_redact_case_fields__does_not_mutate_input__returns_copy() -> None:
    """The helper never mutates the input dict."""
    classification = classify(_schema())
    data = _data()
    out = redact_case_fields(data, classification, can_see_sensitive=False)
    assert out is not data
    out["title"] = "MUTATED"
    assert data["title"] == "Title"


def test_redact_case_fields__restricted_case__masks_every_key_by_ref() -> None:
    """A restricted case masks even public-labelled keys, by ref."""
    classification = classify(_schema(), case_sensitivity="phi")
    out = redact_case_fields(_data(), classification, can_see_sensitive=False)
    for key in _data():
        assert out[key] == {"redacted": True, "reason": "sensitive", "ref": key}
    assert SECRET_SSN not in repr(out)


# ---------------------------------------------------------------------------
# Log payloads
# ---------------------------------------------------------------------------


def test_redact_log_payload__nested_sensitive_keys__replaced_by_ref() -> None:
    """Log payloads mask sensitive keys at any depth, by ref."""
    # // given a log payload nesting case data
    classification = classify(_schema())
    payload = {
        "event": "case.viewed",
        "case_id": "case-1",
        "data": {"title": "T", "ssn": SECRET_SSN},
    }
    # // when redacting for logs
    redacted = redact_log_payload(payload, classification)
    # // then the value is a ref marker and non-sensitive keys survive
    assert redacted["data"]["ssn"] == {"redacted": True, "reason": "sensitive", "ref": "ssn"}
    assert redacted["data"]["title"] == "T"
    assert redacted["case_id"] == "case-1"
    assert SECRET_SSN not in repr(redacted)


def test_redact_log_payload__public_key_with_sensitive_looking_value__passes_through() -> None:
    """Redaction is by key, never by content: a public key passes through."""
    classification = classify(_schema())
    payload = {"notes": SECRET_SSN}
    assert redact_log_payload(payload, classification) == {"notes": SECRET_SSN}


def test_redact_log_payload__sensitive_key_with_innocuous_value__still_redacted() -> None:
    """A restricted key is redacted even when its value looks harmless."""
    classification = classify(_schema())
    payload = {"ssn": "not-a-secret"}
    redacted = redact_log_payload(payload, classification)
    assert redacted["ssn"] == {"redacted": True, "reason": "sensitive", "ref": "ssn"}


# ---------------------------------------------------------------------------
# Event payloads
# ---------------------------------------------------------------------------


def test_redact_event_payload__audit_data__references_values_by_id() -> None:
    """Audit/event payloads carry refs, never sensitive content."""
    # // given an audit payload embedding case data
    classification = classify(_schema())
    payload = {
        "event_type": "CaseUpdated",
        "case_id": "case-1",
        "data": {"ssn": SECRET_SSN, "medical_record": SECRET_MEDICAL, "notes": PUBLIC_NOTES},
    }
    # // when redacting for the event store
    redacted = redact_event_payload(payload, classification)
    # // then each sensitive entry is a ref marker
    assert redacted["data"]["ssn"]["ref"] == "ssn"
    assert redacted["data"]["medical_record"]["ref"] == "medical_record"
    assert redacted["data"]["notes"] == PUBLIC_NOTES
    blob = repr(redacted)
    assert SECRET_SSN not in blob
    assert SECRET_MEDICAL not in blob


def test_redact_event_payload__lists_walked__nested_refs() -> None:
    """Lists inside event payloads are walked for sensitive keys."""
    classification = classify(_schema())
    payload: dict[str, Any] = {"changes": [{"ssn": SECRET_SSN}, {"notes": PUBLIC_NOTES}]}
    redacted = redact_event_payload(payload, classification)
    assert redacted["changes"][0] == {
        "ssn": {"redacted": True, "reason": "sensitive", "ref": "ssn"}
    }
    assert redacted["changes"][1] == {"notes": PUBLIC_NOTES}


# ---------------------------------------------------------------------------
# Search snippets
# ---------------------------------------------------------------------------


def test_redact_search_snippet__unauthorized__masks_sensitive_excerpts() -> None:
    """Search snippets (P05-US3 E3) mask restricted excerpts by ref."""
    # // given a snippet with excerpts per field
    classification = classify(_schema())
    snippet = {"title": "Title match", "ssn": SECRET_SSN, "notes": PUBLIC_NOTES}
    # // when an unauthorized actor receives the snippet
    out = redact_search_snippet(snippet, classification, can_see_sensitive=False)
    # // then the sensitive excerpt is a ref marker; case data untouched
    assert out["ssn"] == {"redacted": True, "reason": "sensitive", "ref": "ssn"}
    assert out["title"] == "Title match"
    assert out["notes"] == PUBLIC_NOTES
    assert SECRET_SSN not in repr(out)


def test_redact_search_snippet__authorized__preserves_excerpts() -> None:
    """A granted search read keeps excerpts verbatim."""
    classification = classify(_schema())
    snippet = {"ssn": SECRET_SSN}
    assert redact_search_snippet(snippet, classification, can_see_sensitive=True) == snippet


# ---------------------------------------------------------------------------
# Observability (refs, never values)
# ---------------------------------------------------------------------------


def test_redacted_refs__lists_only_masked_keys__never_values() -> None:
    """Forensics log the ref list; values never appear."""
    classification = classify(_schema())
    refs = redacted_refs(_data(), classification)
    assert set(refs) == {"ssn", "medical_record"}
    assert SECRET_SSN not in repr(refs)
    assert SECRET_MEDICAL not in repr(refs)


def test_redact_by_keys__generic_mapping__redacts_by_key_identity() -> None:
    """The generic helper redacts nested mappings by key identity."""
    out = redact_by_keys(
        {"outer": {"ssn": SECRET_SSN, "ok": "keep"}},
        frozenset({"ssn"}),
    )
    assert out["outer"]["ssn"] == {"redacted": True, "reason": "sensitive", "ref": "ssn"}
    assert out["outer"]["ok"] == "keep"


def test_classification__is_immutable_value_object() -> None:
    """Classifications are frozen dataclasses (no post-hoc mutation)."""
    classification = classify(_schema())
    assert isinstance(classification, SensitivityClassification)
    with pytest.raises(AttributeError):
        classification.case_level = SensitivityLevel.PUBLIC  # type: ignore[misc]
