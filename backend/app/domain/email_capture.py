"""Captured email aggregate (P04-US6 / FM-028).

Inbound email is approved at the channel boundary (``ChannelKind.EMAIL``)
and becomes a document or case note with channel + message provenance.
The idempotency contract is keyed on the RFC 5322 ``Message-Id``: the
same Message-Id submitted twice produces one ``CapturedEmail`` row and
the second submission returns the prior result without a duplicate
document. This is a *content* idempotency, distinct from the HTTP
``Idempotency-Key`` header replay layer (AGENTS.md §10).

# Why a separate aggregate

A re-submitted Message-Id is a feature of the channel, not of any
single HTTP request. Keeping the dedup state on the
``CapturedEmailRepository`` port lets the same row survive across
``Idempotency-Key`` rotations and across re-ingestion from the Graph /
IMAP connector (P14 seam). The HTTP middleware still applies on top;
both layers must agree.

# Synthetic posture

The aggregate carries ``message_id`` (the RFC 5322 Message-Id, opaque
string), the channel id, and the ``tenant_id``; the email body is
never persisted on this row — it lives on the ``Document`` /
``DocumentVersion`` rows it created (or on the case-note path which is
deferred). This is the AGENTS.md §18 + Constitution V redaction
posture: provenance metadata only on the durable row.

# Tenant isolation

``tenant_id`` is mandatory and every read/write is tenant-scoped. A
cross-tenant Message-Id lookup returns ``None`` so the API never
confirms the existence of another tenant's captured email
(Constitution V).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

# Conservative cap on a Message-Id length. RFC 5322 §3.6.4 imposes no
# hard limit but real-world transports enforce <998; we keep an
# explicit cap so the audit payload stays bounded.
MAX_MESSAGE_ID_LEN: int = 998


def is_valid_message_id(value: str) -> bool:
    """Return True iff ``value`` looks like a usable Message-Id.

    The check is intentionally loose: we accept any non-empty trimmed
    string within the length cap. RFC 5322 syntax (angle brackets,
    msg-id tokens) is left to the upstream mail server; the platform
    stores what the server accepted. Empty / whitespace-only / overlong
    values are refused at the application boundary so a malformed
    capture never lands.
    """
    if not value:
        return False
    trimmed = value.strip()
    return bool(trimmed) and len(trimmed) <= MAX_MESSAGE_ID_LEN


@dataclass(frozen=True, slots=True)
class CapturedEmail:
    """The provenance record for one captured inbound email.

    The aggregate carries the *identity* (Message-Id) and the
    *artifact* (the document or case-note id it produced). It does
    NOT carry the email body, headers, or attachments — those live on
    the document storage path or the case-note path (deferred) and
    are redaction-handled at the read side (Constitution V).

    ``status`` distinguishes "captured as a document" from "captured
    as a case note" so the operator console can show the channel's
    destination mix without a second query.
    """

    id: UUID
    tenant_id: UUID
    channel_id: UUID
    channel_key: str
    message_id: str
    sender: str
    recipients: tuple[str, ...]
    subject: str
    captured_at: datetime
    captured_by: UUID
    destination: str  # "document" | "case_note"
    artifact_id: UUID  # the document or case-note id
    artifact_kind: str  # e.g. "document"
    correlation_id: str
    case_id: UUID | None = None


__all__ = [
    "CapturedEmail",
    "MAX_MESSAGE_ID_LEN",
    "is_valid_message_id",
]
