"""HTTP schemas for the inbound email capture endpoint (P04-US6 / FM-028).

The wire shape is the OpenAPI contract (AGENTS.md §4.R7). The request
carries the RFC 5322 ``Message-Id`` (idempotency anchor), the channel
identifier, and the email body as a base64-encoded UTF-8 string (JSON
body shape matches the first-slice transport; the Graph / IMAP
connector in P14 will feed the application layer directly).

The response shape mirrors the domain ``CapturedEmail`` aggregate
plus the artifact it produced (a ``Document`` today, a case note in
the deferred path). The ``replayed`` flag tells the caller whether
the Message-Id was already on file so the UI can show "duplicate
message, no action taken" without a second round trip.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.domain.errors import ValidationFailed


class CaptureEmailRequest(BaseModel):
    """Body of ``POST /api/v1/capture/email``."""

    channel_id: str = Field(
        description="UUID of the email channel (ChannelKind.EMAIL)",
    )
    message_id: str = Field(
        min_length=1,
        max_length=998,
        description="RFC 5322 Message-Id; idempotency anchor",
    )
    sender: str = Field(min_length=1, max_length=320)
    recipients: list[str] = Field(default_factory=list, max_length=64)
    subject: str = Field(default="", max_length=998)
    case_id: str | None = Field(
        default=None,
        description="UUID of the case the email is filed against",
    )
    body_b64: str = Field(
        description="RFC 5322 message bytes, base64-encoded UTF-8",
    )

    @field_validator("recipients")
    @classmethod
    def _strip_recipients(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]


class CapturedEmailResponse(BaseModel):
    """The captured email provenance row (P04-US6 / FM-028)."""

    id: str
    tenant_id: str
    channel_id: str
    channel_key: str
    message_id: str
    sender: str
    recipients: list[str]
    subject: str
    captured_at: str
    captured_by: str
    destination: str
    artifact_id: str
    artifact_kind: str
    correlation_id: str
    case_id: str | None = None


class CaptureEmailResponse(BaseModel):
    """Wire shape returned by ``POST /api/v1/capture/email``.

    ``replayed=True`` means the Message-Id was already on file; the
    artifact details below are the *original* capture's and no new
    document was written. ``document`` mirrors the documents API
    shape so the operator UI can render the same panel as the
    direct-upload path.
    """

    captured: CapturedEmailResponse
    replayed: bool
    document: dict[str, Any] | None = None


def decode_body_b64(value: str) -> bytes:
    """Decode a base64 string into raw bytes for the use case.

    Raises ``ValidationFailed`` on a malformed value so the router
    can translate it to a clean 422. The use case itself does not
    re-validate — the bytes are opaque to it.
    """
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationFailed(
            f"body_b64 is not valid base64: {exc}",
            field="body_b64",
        ) from exc


# ---------------------------------------------------------------------------
# Scan capture wire shape (P04-US7 / FM-029)
# ---------------------------------------------------------------------------


_VALID_SCAN_RESULTS: frozenset[str] = frozenset({"clean", "infected", "inconclusive"})


class CaptureScanRequest(BaseModel):
    """Body of ``POST /api/v1/capture/scan``.

    Mirrors the use case's :class:`ScanCaptureCommand`. The scan
    verdict is supplied by the P14 scan connector (or by the operator
    UI replaying a synthetic scan in this slice); the platform never
    scans the bytes itself.
    """

    channel_id: str = Field(
        description="UUID of the scan channel (ChannelKind.SCAN)",
    )
    device_id: str = Field(
        min_length=1,
        max_length=128,
        description="Stable identifier of the scanning device (provenance)",
    )
    source_label: str = Field(
        min_length=1,
        max_length=256,
        description="Human-readable label for the operator console",
    )
    scan_result: str = Field(
        description="clean | infected | inconclusive",
    )
    scan_detail: str = Field(
        default="",
        max_length=1024,
        description="Free-text finding from the scanner (virus name, OCR confidence, etc.)",
    )
    content_type: str = Field(
        default="application/octet-stream",
        max_length=128,
    )
    file_name: str = Field(default="", max_length=256)
    case_id: str | None = Field(
        default=None,
        description="UUID of the case the scan is filed against when clean",
    )
    content_b64: str = Field(
        description="Raw scan bytes (image / PDF), base64-encoded",
    )

    @field_validator("scan_result")
    @classmethod
    def _validate_scan_result(cls, value: str) -> str:
        if value not in _VALID_SCAN_RESULTS:
            raise ValueError(f"scan_result must be one of {sorted(_VALID_SCAN_RESULTS)}")
        return value


class CaptureScanResponse(BaseModel):
    """Wire shape returned by ``POST /api/v1/capture/scan``.

    ``released=True`` means the scan was clean and a document was
    produced; ``document`` is populated in that case (same wire shape
    as ``POST /api/v1/cases/{id}/documents``). ``released=False`` means
    the scan was infected / inconclusive: the quarantine record is held
    and ``document`` is None so the operator UI can route the user to
    the quarantine console.
    """

    quarantine_id: str
    channel_id: str
    channel_key: str
    scan_result: str
    error_code: str
    released: bool
    document: dict[str, Any] | None = None


__all__ = [
    "CaptureEmailRequest",
    "CaptureEmailResponse",
    "CaptureScanRequest",
    "CaptureScanResponse",
    "CapturedEmailResponse",
    "decode_body_b64",
]
