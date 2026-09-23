"""Capture provenance value object (T1 / FM-027).

Every item that enters the platform through an ingestion channel carries
*provenance*: which outside system it came from, which record inside that
system it was, which channel it arrived on, when it was captured, and a
hash of the exact bytes that were received. The hash lets an operator
prove "this is the payload we received" without persisting the payload
itself next to the diagnosis (Constitution V — redaction).

# Why a value object, not a dict

Provenance used to travel as ad-hoc ``dict[str, Any]`` payloads
(``{"split_from": ...}``, ``{"source_label": ...}``). Ad-hoc dicts
cannot enforce the FM-027 contract — a missing ``source_id`` or a
truncated hash would only surface at the operator console, far from the
capture site. This frozen dataclass validates at construction so a
malformed provenance never lands:

- ``source_system`` / ``source_record_id`` / ``channel_key`` are
  non-empty, trimmed, and length-capped (bounded audit payloads);
- ``payload_hash`` is a 64-char lowercase SHA-256 hex digest;
- ``captured_at`` must be timezone-aware (naive timestamps silently
  shift meaning across workers, so they are refused).

# Relation to ``Channel`` / ``QuarantineRecord``

``Channel`` (aliased as ``CaptureChannel`` for the T1 track name) is the
*configured entry point*; ``Provenance`` is the *per-item receipt* for
one row that passed through it. A ``QuarantineRecord`` keeps the
human-readable ``source_label`` for the review queue; the matching
``Provenance`` (built via :meth:`Provenance.for_quarantine`) pins the
system-level identity + payload hash for reconciliation and audit.

# INTEGRATION_SEAM: enterprise imports

Future P14 connector adapters (mailbox, scan, watched-folder) build the
same ``Provenance`` via :meth:`Provenance.create`; only the
``source_system`` string changes. No adapter may invent a second
provenance shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Final

from app.domain.errors import InvariantViolation

if TYPE_CHECKING:  # pragma: no cover - import for typing only, no runtime cycle
    from app.domain.channel import QuarantineRecord

# Conservative caps so audit payloads stay bounded (mirrors the
# Message-Id cap in ``email_capture.py``).
MAX_SOURCE_SYSTEM_LEN: Final[int] = 64
MAX_SOURCE_RECORD_ID_LEN: Final[int] = 256
MAX_CHANNEL_KEY_LEN: Final[int] = 128
PAYLOAD_HASH_LEN: Final[int] = 64


def compute_payload_hash(payload: bytes) -> str:
    """Return the SHA-256 hex digest of ``payload``.

    Args:
        payload: The exact inbound bytes received on the channel.

    Returns:
        Lowercase 64-char hex digest identifying the payload.

    Raises:
        InvariantViolation: If ``payload`` is empty — an empty capture
            is a caller bug (the channel adapter must refuse it before
            provenance is built), never a hashable receipt.
    """
    # why: hashing b"" would mint a valid-looking receipt for "nothing
    # received", which would then reconcile as a real row downstream.
    if not payload:
        raise InvariantViolation("cannot hash an empty payload")
    return sha256(payload).hexdigest()


def is_valid_payload_hash(value: str) -> bool:
    """Return True iff ``value`` is a 64-char lowercase SHA-256 hex digest."""
    if len(value) != PAYLOAD_HASH_LEN:
        return False
    return all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True, slots=True)
class Provenance:
    """The per-item receipt for one captured row.

    Attributes:
        source_system: Outside system that produced the row
            (e.g. ``"watched_folder"``, ``"mailbox"``, ``"import"``).
        source_record_id: Record identity inside the source system
            (filename, Message-Id, CSV line key). Opaque string.
        channel_key: Key of the :class:`Channel` the row arrived on.
        captured_at: Timezone-aware capture timestamp.
        payload_hash: SHA-256 hex of the exact inbound bytes.
    """

    source_system: str
    source_record_id: str
    channel_key: str
    captured_at: datetime
    payload_hash: str

    def __post_init__(self) -> None:
        if not self.source_system or not self.source_system.strip():
            raise InvariantViolation("provenance requires a non-empty source_system")
        if len(self.source_system) > MAX_SOURCE_SYSTEM_LEN:
            raise InvariantViolation(f"source_system exceeds {MAX_SOURCE_SYSTEM_LEN} chars")
        if not self.source_record_id or not self.source_record_id.strip():
            raise InvariantViolation("provenance requires a non-empty source_record_id")
        if len(self.source_record_id) > MAX_SOURCE_RECORD_ID_LEN:
            raise InvariantViolation(f"source_record_id exceeds {MAX_SOURCE_RECORD_ID_LEN} chars")
        if not self.channel_key or not self.channel_key.strip():
            raise InvariantViolation("provenance requires a non-empty channel_key")
        if len(self.channel_key) > MAX_CHANNEL_KEY_LEN:
            raise InvariantViolation(f"channel_key exceeds {MAX_CHANNEL_KEY_LEN} chars")
        # why: naive datetimes shift meaning across workers/timezones;
        # every persisted timestamp in the platform is tz-aware (UTC).
        if self.captured_at.tzinfo is None:
            raise InvariantViolation("provenance captured_at must be timezone-aware")
        if not is_valid_payload_hash(self.payload_hash):
            raise InvariantViolation(
                "provenance payload_hash must be a 64-char lowercase SHA-256 hex digest"
            )

    @staticmethod
    def create(
        *,
        source_system: str,
        source_record_id: str,
        channel_key: str,
        payload: bytes,
        captured_at: datetime,
    ) -> Provenance:
        """Build a ``Provenance`` computing the hash from ``payload``.

        Prefer this over the raw constructor: the hash always matches
        the bytes actually received, so a caller cannot attach a stale
        or truncated digest by mistake.
        """
        return Provenance(
            source_system=source_system,
            source_record_id=source_record_id,
            channel_key=channel_key,
            captured_at=captured_at,
            payload_hash=compute_payload_hash(payload),
        )

    @staticmethod
    def for_quarantine(
        record: QuarantineRecord,
        *,
        source_system: str,
        payload: bytes,
    ) -> Provenance:
        """Build the provenance receipt for a held ``QuarantineRecord``.

        The quarantine row carries the human-readable ``source_label``
        for the review queue; this receipt pins the system-level
        identity (``source_system`` + payload hash) used for
        reconciliation and audit. ``channel_key`` and ``captured_at``
        are taken from the record so the two cannot disagree.
        """
        return Provenance.create(
            source_system=source_system,
            source_record_id=record.source_label,
            channel_key=record.channel_key,
            payload=payload,
            captured_at=record.captured_at,
        )

    def matches_payload(self, payload: bytes) -> bool:
        """Return True iff ``payload`` hashes to this receipt's digest."""
        if not payload:
            return False
        return compute_payload_hash(payload) == self.payload_hash

    def to_audit_dict(self) -> dict[str, str]:
        """Return a redaction-safe audit mapping (identity + hash only).

        Never includes payload content or payload values — only the
        source identity, channel, timestamp, and digest, so the audit
        trail can reference the row without leaking it (Constitution V).
        """
        return {
            "source_system": self.source_system,
            "source_record_id": self.source_record_id,
            "channel_key": self.channel_key,
            "captured_at": self.captured_at.isoformat(),
            "payload_hash": self.payload_hash,
        }


__all__ = [
    "MAX_CHANNEL_KEY_LEN",
    "MAX_SOURCE_RECORD_ID_LEN",
    "MAX_SOURCE_SYSTEM_LEN",
    "PAYLOAD_HASH_LEN",
    "Provenance",
    "compute_payload_hash",
    "is_valid_payload_hash",
]
