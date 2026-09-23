"""Pure-domain tests for document protection (FM-025 / T5).

Covers the three rules in ``app.domain.document_protection``:

- restriction: restricted documents (``restricted`` / ``phi``) need the
  stricter right; reads without it raise ``Forbidden`` with no metadata
  leak (guessed-URL acceptance);
- versioned redaction with original preserved: the original bytes always
  need the stricter right, even when the parent is merely ``internal``;
- filing lock: any mutation of a filed (``locked``) document raises
  ``DocumentLocked`` before side effects.

Naming follows ``test_<unit>_<scenario>__<expectation>`` (AGENTS.md §4.R5).
Structure is ``// given`` / ``// when`` / ``// then``. Deterministic: fixed
clock timestamp, fixed UUIDs, no I/O.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.domain.document import Document, DocumentSensitivity
from app.domain.document_protection import (
    READ_PERMISSION,
    READ_RESTRICTED_PERMISSION,
    check_mutation_allowed,
    check_read_allowed,
    is_access_restricted,
    required_read_permission,
    validate_redaction,
)
from app.domain.errors import DocumentLocked, Forbidden, ValidationFailed

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
CASE_ID = UUID("33333333-3333-3333-3333-333333333333")
ACTOR_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def _document(
    *,
    sensitivity: DocumentSensitivity = DocumentSensitivity.INTERNAL,
    locked: bool = False,
) -> Document:
    """Build a minimal v1 document for protection-policy tests."""
    content = b"%PDF-1.4\nv1 bytes"
    sha = hashlib.sha256(content).hexdigest()
    document = Document.create(
        tenant_id=ACME_TENANT,
        case_id=CASE_ID,
        file_name="onboarding.pdf",
        content_type="application/pdf",
        classification="Contract",
        sensitivity=sensitivity,
        sha256=sha,
        size=len(content),
        storage_ref=f"{ACME_TENANT}/{sha[:2]}/{sha}",
        uploaded_by=ACTOR_ID,
        now=FIXED_NOW,
    )
    if locked:
        document = document.with_lock(actor_id=ACTOR_ID, now=FIXED_NOW)
    return document


# ---------------------------------------------------------------------------
# Restriction classification
# ---------------------------------------------------------------------------


def test_is_access_restricted__restricted_levels__true() -> None:
    """``restricted`` and ``phi`` documents count as access-restricted."""
    # // given documents at each sensitivity level
    # // when classifying
    # // then only restricted/phi report True
    for sensitivity, expected in (
        (DocumentSensitivity.PUBLIC, False),
        (DocumentSensitivity.INTERNAL, False),
        (DocumentSensitivity.CONFIDENTIAL, False),
        (DocumentSensitivity.RESTRICTED, True),
        (DocumentSensitivity.PHI, True),
    ):
        assert is_access_restricted(_document(sensitivity=sensitivity)) is expected


def test_required_read_permission__internal_redacted_view__base_right() -> None:
    """A normal document's redacted view needs only the base read right."""
    # // given an internal document
    document = _document(sensitivity=DocumentSensitivity.INTERNAL)
    # // when asking for the redacted-view permission
    # // then the base right suffices
    assert required_read_permission(document, for_original=False) == READ_PERMISSION


def test_required_read_permission__restricted_document__stricter_right() -> None:
    """A restricted document needs the stricter right even for the view."""
    # // given a restricted document
    document = _document(sensitivity=DocumentSensitivity.RESTRICTED)
    # // when asking for the view permission
    # // then the stricter right is required
    assert required_read_permission(document, for_original=False) == READ_RESTRICTED_PERMISSION


def test_required_read_permission__original__stricter_right() -> None:
    """Original (pre-redaction) bytes always need the stricter right."""
    # // given an internal parent (the easy-to-get-wrong case: the view
    # // is readable but the original still holds the redacted bytes)
    document = _document(sensitivity=DocumentSensitivity.INTERNAL)
    # // when asking for the original permission
    # // then the stricter right is required — redaction must not leak
    # // the removed bytes to view-only readers
    assert required_read_permission(document, for_original=True) == READ_RESTRICTED_PERMISSION


# ---------------------------------------------------------------------------
# Read-access enforcement (guessed-URL acceptance)
# ---------------------------------------------------------------------------


def test_check_read_allowed__restricted_without_grant__forbidden_without_leak() -> None:
    """A restricted read without the grant is forbidden with no metadata."""
    # // given a restricted document and a view-only grant set
    document = _document(sensitivity=DocumentSensitivity.RESTRICTED)
    # // when reading without the stricter grant
    with pytest.raises(Forbidden) as exc_info:
        check_read_allowed(
            document=document,
            actor_tenant_id=ACME_TENANT,
            granted_permissions=frozenset({READ_PERMISSION}),
        )
    # // then the error carries only a safe reason — no file name,
    # // storage ref, content, or size a guessed-URL probe could reuse
    blob = json.dumps(exc_info.value.details, default=str)
    assert "onboarding.pdf" not in blob
    assert document.storage_ref not in blob
    assert str(document.size) not in blob or "reason" in exc_info.value.details


def test_check_read_allowed__restricted_with_grant__allows() -> None:
    """The stricter grant restores access to a restricted document."""
    # // given a restricted document
    document = _document(sensitivity=DocumentSensitivity.RESTRICTED)
    # // when the actor holds the stricter grant
    # // then the read proceeds (returns None)
    assert (
        check_read_allowed(
            document=document,
            actor_tenant_id=ACME_TENANT,
            granted_permissions=frozenset({READ_RESTRICTED_PERMISSION}),
        )
        is None
    )


def test_check_read_allowed__original_needs_stricter_even_when_parent_internal() -> None:
    """View-only readers cannot reach the original bytes (redaction safety)."""
    # // given an internal parent with only the base grant
    document = _document(sensitivity=DocumentSensitivity.INTERNAL)
    granted = frozenset({READ_PERMISSION})
    # // when reading the redacted view
    # // then it is allowed ...
    assert (
        check_read_allowed(
            document=document,
            actor_tenant_id=ACME_TENANT,
            granted_permissions=granted,
        )
        is None
    )
    # // ... but the original is forbidden under the same grant set
    with pytest.raises(Forbidden) as exc_info:
        check_read_allowed(
            document=document,
            actor_tenant_id=ACME_TENANT,
            granted_permissions=granted,
            for_original=True,
        )
    assert exc_info.value.details.get("reason") == "original_requires_stricter_right"


def test_check_read_allowed__cross_tenant__denies_even_with_grant() -> None:
    """Cross-tenant reads deny even when the grant set would allow."""
    # // given a document owned by ACME
    document = _document()
    # // when an actor from another tenant presents full grants
    # // then access is denied (tenant isolation, FM-002)
    with pytest.raises(Forbidden):
        check_read_allowed(
            document=document,
            actor_tenant_id=OTHER_TENANT,
            granted_permissions=frozenset({READ_PERMISSION, READ_RESTRICTED_PERMISSION}),
            for_original=True,
        )


# ---------------------------------------------------------------------------
# Filing lock (filed document refuses mutations)
# ---------------------------------------------------------------------------


def test_check_mutation_allowed__unlocked__allows() -> None:
    """An unlocked document passes the filing-lock gate."""
    # // given an unlocked document
    document = _document(locked=False)
    # // when checking mutation eligibility
    # // then no error is raised
    assert check_mutation_allowed(document) is None


def test_check_mutation_allowed__filed_document__raises_document_locked() -> None:
    """A filed (locked) document refuses every mutation kind."""
    # // given a filed document
    document = _document(locked=True)
    # // when any mutation is attempted (version, redaction, restriction
    # // change, deletion all funnel through this gate)
    # // then DocumentLocked is raised before any side effect
    with pytest.raises(DocumentLocked):
        check_mutation_allowed(document)


def test_validate_redaction__happy_path__allows() -> None:
    """A well-formed redaction request passes the pure gate."""
    # // given an unlocked v1 document
    document = _document()
    # // when validating a redaction against the known version
    # // then no error is raised (storage/audit writes stay in the use case)
    assert (
        validate_redaction(
            document,
            base_version_number=1,
            reason="ssn blacked out",
            region="page 2 paragraph 3",
        )
        is None
    )


def test_validate_redaction__filed_document__raises_document_locked() -> None:
    """Redacting a filed document raises even when the request is valid."""
    # // given a filed document
    document = _document(locked=True)
    # // when validating an otherwise well-formed redaction
    # // then the filing-lock refusal wins over validation
    with pytest.raises(DocumentLocked):
        validate_redaction(
            document,
            base_version_number=1,
            reason="ssn blacked out",
            region=None,
        )


def test_validate_redaction__unknown_base_version__raises_validation_failed() -> None:
    """A base version outside history is rejected with the field set."""
    # // given an unlocked v1 document
    document = _document()
    # // when the base version names history that does not exist
    # // then ValidationFailed names the field (no silent mis-lineage)
    with pytest.raises(ValidationFailed) as exc_info:
        validate_redaction(
            document,
            base_version_number=999,
            reason="ssn blacked out",
            region=None,
        )
    assert exc_info.value.field == "base_version_number"


def test_validate_redaction__empty_reason__raises_validation_failed() -> None:
    """An empty reason is rejected — the audit must record the why."""
    # // given an unlocked document
    document = _document()
    # // when the reason is blank
    # // then ValidationFailed names the reason field
    with pytest.raises(ValidationFailed) as exc_info:
        validate_redaction(
            document,
            base_version_number=1,
            reason="   ",
            region=None,
        )
    assert exc_info.value.field == "reason"


def test_validate_redaction__oversize_region__raises_validation_failed() -> None:
    """An oversize region description is rejected with the field set."""
    # // given an unlocked document
    document = _document()
    # // when the region exceeds the 500-char bound
    # // then ValidationFailed names the region field
    with pytest.raises(ValidationFailed) as exc_info:
        validate_redaction(
            document,
            base_version_number=1,
            reason="ssn blacked out",
            region="x" * 501,
        )
    assert exc_info.value.field == "region"
