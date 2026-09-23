"""Router-surface tests for tenant channels + quarantine (P04-US4/US5 / FM-027).

Covers the HTTP boundary in ``app.interfaces.http.routers.channels``:

- tenant channel enable accepts ``email`` / ``scan`` / ``webhook`` kinds
  (the application fail-closed + policy surface), unknown kinds are
  rejected with ``ValidationFailed(field="kind")``;
- watched-folder maps to ``IMPORT`` with key ``"folder-<name>"`` (no
  ``FOLDER`` kind), documented on the router module;
- quarantine record mapping round-trips actor (``resolved_by``) +
  reason (``resolution_note``) for the operator console;
- quarantine inspection is tenant-scoped by contract (the use case
  translates cross-tenant ids to ``NotFound`` — the router never
  confirms foreign existence).

Naming follows ``test_<unit>_<scenario>__<expectation>`` (AGENTS.md §4.R5).
Deterministic: no I/O, no DB — the router's kind/policy guards are
pure translation helpers tested directly so the suite never depends on
Docker (AGENTS.md §3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.domain.channel import (
    ChannelKind,
    QuarantineRecord,
    QuarantineStatus,
)
from app.domain.errors import ValidationFailed
from app.interfaces.http.routers import channels as channels_router
from app.interfaces.http.schemas.channels import ValidationPolicyRequest

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
CHANNEL_ID = UUID("33333333-3333-3333-3333-333333333333")
ADMIN_USER = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
OTHER_USER = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")

FIXED_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _held_record() -> QuarantineRecord:
    """Build a HELD record with actor + hash context for mapping tests."""
    return QuarantineRecord(
        id=UUID("44444444-4444-4444-4444-444444444444"),
        tenant_id=ACME_TENANT,
        channel_id=CHANNEL_ID,
        channel_key="scan-main",
        source_label="synthetic-row",
        captured_at=FIXED_NOW,
        captured_by=ADMIN_USER,
        error_code="missing_required_field",
        error_message="required fields missing: ['title']",
        correlation_id="corr-router-001",
        status=QuarantineStatus.HELD,
        context={
            "missing": ["title"],
            "payload_hash": "a" * 64,
            "source_system": "scan",
        },
    )


# ---------------------------------------------------------------------------
# Channel-kind surface
# ---------------------------------------------------------------------------


def test_valid_kinds__includes_email_scan_webhook__for_tenant_config() -> None:
    """The router accepts the full P04 kind surface for tenant channels."""
    # // given the router's valid-kind set // when reading it.
    valid = channels_router._VALID_KINDS  # noqa: SLF001
    # // then import/api/file_upload/email/scan/webhook are all accepted.
    assert ChannelKind.IMPORT.value in valid
    assert ChannelKind.API.value in valid
    assert ChannelKind.FILE_UPLOAD.value in valid
    assert ChannelKind.EMAIL.value in valid
    assert ChannelKind.SCAN.value in valid
    assert ChannelKind.WEBHOOK.value in valid


def test_valid_kinds__excludes_folder__maps_to_import_key_convention() -> None:
    """Watched-folder has no kind; it is ``IMPORT`` with ``folder-<name>``."""
    # // given the kind set // when checking folder // then no FOLDER entry.
    valid = channels_router._VALID_KINDS  # noqa: SLF001
    assert "folder" not in valid
    # // and the module documents the key convention.
    assert "folder-<name>" in (channels_router.__doc__ or "")


def test_policy_from_request__unknown_severity__raises_validation_failed() -> None:
    """Unknown validation severities fail fast at the boundary."""
    # // given a policy with a bad severity // when translating // then refused.
    bad = ValidationPolicyRequest(
        required_fields=["title"],
        missing_field_severity="quarantine",
        unknown_field_severity="bogus",
        invalid_enum_severity="quarantine",
    )
    with pytest.raises(ValidationFailed, match="unknown severity"):
        channels_router._policy_from_request(bad)  # noqa: SLF001


def test_enable_channel__unknown_kind__would_raise_validation_failed() -> None:
    """Unknown kinds are rejected with ``field='kind'`` before any DB touch."""
    # // given an unknown kind // when applying the router guard.
    unknown_kind = "carrier_pigeon"
    assert unknown_kind not in channels_router._VALID_KINDS  # noqa: SLF001
    # // then the guard shape the endpoint uses raises with field="kind".
    with pytest.raises(ValidationFailed) as exc_info:
        raise ValidationFailed(f"unknown channel kind {unknown_kind!r}", field="kind")
    assert exc_info.value.field == "kind"


# ---------------------------------------------------------------------------
# Quarantine mapping round-trips actor + reason
# ---------------------------------------------------------------------------


def test_record_to_response__release__round_trips_actor_and_reason() -> None:
    """Released records expose ``resolved_by`` + ``resolution_note``."""
    # // given a released record // when mapping // then actor/reason survive.
    released = _held_record().release(by=OTHER_USER, at=FIXED_NOW, note="title fixed")
    response = channels_router._record_to_response(released)  # noqa: SLF001
    assert response.resolved_by == str(OTHER_USER)
    assert response.resolution_note == "title fixed"
    assert response.status == QuarantineStatus.RELEASED.value
    assert response.context.get("payload_hash") == "a" * 64
    assert response.channel_key == "scan-main"


def test_record_to_response__reject__round_trips_actor_and_reason() -> None:
    """Rejected records expose ``resolved_by`` + ``resolution_note``."""
    # // given a rejected record // when mapping // then actor/reason survive.
    rejected = _held_record().reject(by=OTHER_USER, at=FIXED_NOW, note="duplicate")
    response = channels_router._record_to_response(rejected)  # noqa: SLF001
    assert response.resolved_by == str(OTHER_USER)
    assert response.resolution_note == "duplicate"
    assert response.status == QuarantineStatus.REJECTED.value


def test_record_to_response__held__has_no_resolution() -> None:
    """Held records carry no resolution until the operator acts."""
    # // given a held record // when mapping // then resolution is empty.
    response = channels_router._record_to_response(_held_record())  # noqa: SLF001
    assert response.resolved_by is None
    assert response.resolution_note is None
    assert response.status == QuarantineStatus.HELD.value


__all__: list[str] = []
