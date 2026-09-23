"""Inbound webhook capture aggregate (P04-US8 / FM-030, ADR-007).

The :class:`CapturedWebhook` is the durable provenance record for an
inbound webhook event that has been authenticated, validated, and
applied (or replayed) as a case command. It carries the source system,
the channel, the producer's ``X-Webhook-Id`` (the resource-level
idempotency anchor), the command that was dispatched, and the case it
landed on.

This module is the **domain** half of the seam: the shape and the
invariants. The application use case in
:mod:`app.application.capture.webhook_commands` is the orchestration
half; the HTTP router in
:mod:`app.interfaces.http.routers.webhooks` is the transport half.

# Why a dedicated aggregate (and not a generic ``Captured*``)

A webhook is fundamentally an *event from outside* that the platform
*turns into a case command*. The artifact side of the seam is the
``CaseTransitioned`` (or downstream) event; the source side is a
``CapturedWebhook`` row. Keeping the source side as a first-class
aggregate (not a column on ``audit_events``) means:

- the resource-level dedup ledger (``UNIQUE (tenant_id, channel_id,
  webhook_id)``) lives next to the data, mirroring the
  ``captured_emails`` table for FM-028 (ADR-005);
- the producer's ``X-Webhook-Id`` is the durable anchor across HTTP
  retries (the HTTP ``Idempotency-Key`` middleware is request-scoped
  and cannot survive a connector-level replay — see ADR-007 §3);
- the channel's policy (``required_fields``, ``max_record_bytes``) can
  be evaluated against the webhook payload without re-parsing the
  original HTTP request.

# Multi-tenant posture

The aggregate is tenant-scoped end-to-end. ``CapturedWebhookRepository``
filters every read by ``tenant_id`` so a cross-tenant ``X-Webhook-Id``
is never observable (Constitution V).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation, ValidationFailed

# ---------------------------------------------------------------------------
# Command vocabulary
# ---------------------------------------------------------------------------


class WebhookCommandKind(str, Enum):  # noqa: UP042 — intentional str+Enum
    """The closed set of case commands a webhook may dispatch.

    The vocabulary is deliberately small and explicit:

    - ``CASE_TRANSITION`` — advance the case to a known state by
      ``transition_key``. The key matches the published
      ``WorkflowVersion`` the case is pinned to.
    - ``CASE_COMMENT`` — append a comment to the case timeline. The
      payload's ``text`` is recorded on the audit chain; the comment
      body is never logged (Constitution V — redaction).
    - ``CASE_NOTE`` — record a free-form note without a workflow
      transition. Useful for "external system observed X" signals.

    Adding a new command kind requires a superseding ADR and a new
    audit event type; the vocabulary is not extensible at runtime
    (Constitution XIV — bounded extensibility).
    """

    CASE_TRANSITION = "case.transition"
    CASE_COMMENT = "case.comment"
    CASE_NOTE = "case.note"


_SUPPORTED_COMMAND_KINDS: frozenset[WebhookCommandKind] = frozenset(
    {
        WebhookCommandKind.CASE_TRANSITION,
        WebhookCommandKind.CASE_COMMENT,
        WebhookCommandKind.CASE_NOTE,
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_valid_webhook_id(value: str) -> bool:
    """Return True iff ``value`` is a safe, non-empty ``X-Webhook-Id``.

    The producer's id is the dedup anchor; we cap its length so a
    pathological value cannot blow up the unique-index page and reject
    characters that have no business in an HTTP header value. The cap
    matches RFC 7230 §3.2.6's recommended header length (998 octets).
    """
    if not value:
        return False
    if len(value) > 998:
        return False
    # Header values are visible ASCII; reject control characters and
    # whitespace at the boundary so a poisoned value cannot smuggle
    # a second field through the unique index.
    return all(0x20 < ord(c) < 0x7F for c in value)


def payload_digest(payload: dict[str, Any]) -> str:
    """Return a stable SHA-256 of a webhook payload.

    The digest is the **provenance** fingerprint that the audit row
    records. The platform stores the original payload on the
    ``CapturedWebhook`` row but uses the digest for the audit chain
    so a tampered payload row is detectable by the hash check.

    The canonical form is ``json.dumps(payload, sort_keys=True,
    separators=(",", ":"), ensure_ascii=False).encode("utf-8")``. This
    is the same canonical form the HMAC verifier uses on the router
    side; they must agree.
    """
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


# ---------------------------------------------------------------------------
# CapturedWebhook aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapturedWebhook:
    """A durable record of one inbound webhook event.

    Carries the full provenance an operator needs to audit an external
    system's effect on a case: who sent it, through which channel, with
    which idempotency anchor, and which case command was applied.

    The original ``payload`` is stored on the row (JSON column) so the
    operator console can replay the event. The ``payload_digest`` is
    the value the audit chain records so a tampered payload row is
    detectable.
    """

    id: UUID
    tenant_id: UUID
    channel_id: UUID
    channel_key: str
    webhook_id: str
    source_system: str
    command_kind: WebhookCommandKind
    case_id: UUID
    payload: dict[str, Any]
    payload_digest: str
    captured_at: datetime
    captured_by: UUID
    correlation_id: str

    def __post_init__(self) -> None:
        if not is_valid_webhook_id(self.webhook_id):
            raise ValidationFailed(
                "webhook_id is required and must be 1..998 visible-ASCII characters",
                field="webhook_id",
            )
        if not self.source_system:
            raise InvariantViolation("captured_webhook requires a non-empty source_system")
        if not self.channel_key:
            raise InvariantViolation("captured_webhook requires a non-empty channel_key")
        if self.command_kind not in _SUPPORTED_COMMAND_KINDS:
            raise InvariantViolation(f"unsupported webhook command kind: {self.command_kind!r}")
        if not self.payload_digest or len(self.payload_digest) != 64:
            raise InvariantViolation("captured_webhook requires a 64-char sha256 payload_digest")

    # ----- factory ----------------------------------------------------------

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        channel_id: UUID,
        channel_key: str,
        webhook_id: str,
        source_system: str,
        command_kind: WebhookCommandKind,
        case_id: UUID,
        payload: dict[str, Any],
        captured_at: datetime,
        captured_by: UUID,
        correlation_id: str,
    ) -> CapturedWebhook:
        """Build a fresh :class:`CapturedWebhook` row.

        The ``payload_digest`` is computed here from the supplied
        ``payload`` so the application layer cannot forget to populate
        it. The aggregate is the only place that decides what the
        canonical form is (see :func:`payload_digest`).
        """
        return CapturedWebhook(
            id=uuid4(),
            tenant_id=tenant_id,
            channel_id=channel_id,
            channel_key=channel_key,
            webhook_id=webhook_id,
            source_system=source_system,
            command_kind=command_kind,
            case_id=case_id,
            payload=dict(payload),
            payload_digest=payload_digest(payload),
            captured_at=captured_at,
            captured_by=captured_by,
            correlation_id=correlation_id,
        )


__all__ = [
    "CapturedWebhook",
    "WebhookCommandKind",
    "is_valid_webhook_id",
    "payload_digest",
]
