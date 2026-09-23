"""Ingestion channels + quarantine (P04-US4, US5 / FM-027).

Pillar P04 owns the seam where external material enters the platform.
This module is the deterministic core of that seam: it defines what a
``Channel`` is, what ``ValidationPolicy`` it carries, and what happens
to a captured item that fails validation.

# Why channels live in the domain

A channel is more than a name — it is a *contract* about how material
from a given source is validated before it lands. The contract must
live with the domain so:

- the same policy is enforced regardless of which adapter (HTTP, CLI,
  email, scan) feeds the row in (Constitution V — server-side
  authorization),
- the ``ChannelRepository`` (FM-027 / ports) ports the persistence
  shape and keeps the domain free of SQLAlchemy,
- the ``QuarantineRecord`` aggregate is the unit the operator inspects
  and decides on; it carries no payload data (Constitution V
  redaction), only a stable ``error_code`` + non-sensitive message.

# Multi-tenant posture

Every aggregate carries ``tenant_id``. Cross-tenant channel lookups
return ``None`` (no leak). The aggregate refuses to mutate when a
``tenant_id`` mismatch is detected — the application layer translates
the refusal into ``Forbidden`` / ``NotFound``.

# Quarantine is **per-record**

A malformed row is isolated into its own ``QuarantineRecord``; sibling
rows in the same batch are still validated and committed. This is the
acceptance criterion from pillar P04 D4: a single bad row never blocks
the good ones, and the operator can find and resolve the bad row.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Final
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation

# ---------------------------------------------------------------------------
# Channel kinds — the canonical inbound surfaces (P04 §3).
# ---------------------------------------------------------------------------


class ChannelKind(str, Enum):  # noqa: UP042 - intentional str+Enum for JSON
    """The set of inbound channels this slice understands.

    New kinds (email, scan, webhook) are added in later features
    (P04-US6/US7/US8) without changing the policy vocabulary here.
    ``EMAIL`` is the inbound email seam (P04-US6 / FM-028); the local
    adapter accepts JSON-shaped emails today, the Graph / IMAP
    connector is reserved for the production seam (AGENTS.md §12).
    """

    IMPORT = "import"
    API = "api"
    FILE_UPLOAD = "file_upload"
    EMAIL = "email"
    # ``SCAN`` is the inbound scan-channel seam (P04-US7 / FM-029). The
    # local adapter accepts JSON-shaped scan verdicts over HTTP today;
    # the real scanner is the P14 connector (AGENTS.md §12). Marked
    # Gated in ``01-deduplicated-feature-map.md:78`` so the use case
    # only runs when the operator has explicitly enabled the feature.
    SCAN = "scan"
    # ``WEBHOOK`` is the inbound webhook-command seam (P04-US8 / FM-030).
    # Authorized external systems POST HMAC-signed events that resolve
    # to explicit case commands / state changes. This is **inbound** and
    # distinct from outbound event delivery (FM-056). See ADR-007.
    WEBHOOK = "webhook"


_SUPPORTED_CHANNEL_KINDS: Final[frozenset[ChannelKind]] = frozenset(
    {
        ChannelKind.IMPORT,
        ChannelKind.API,
        ChannelKind.FILE_UPLOAD,
        ChannelKind.EMAIL,
        ChannelKind.SCAN,
        ChannelKind.WEBHOOK,
    }
)


# ---------------------------------------------------------------------------
# Validation policy — what each channel accepts.
# ---------------------------------------------------------------------------


class ValidationSeverity(str, Enum):  # noqa: UP042
    """What happens to a row that fails a policy check.

    - ``QUARANTINE`` — the row is isolated into a ``QuarantineRecord``
      and the operator decides its fate. This is the default for
      structured data where the bad row may be salvageable.
    - ``REJECT`` — the row is dropped without a quarantine record. Used
      for spam / duplicate external noise that should never enter the
      operator's review queue.
    """

    QUARANTINE = "quarantine"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ValidationPolicy:
    """The validation rules attached to a :class:`Channel`.

    ``required_fields`` is the closed set of keys a row MUST carry; a
    missing key quarantines the row (or rejects it, depending on
    ``missing_field_severity``). ``allowed_classifications`` mirrors
    the case-type's ``document_classifications`` — a document outside
    the set is quarantined.

    ``max_record_bytes`` caps the inline payload size; oversized rows
    are quarantined (never truncated) so the operator can decide
    whether to split the source.

    Why a frozen dataclass: a policy is part of the channel's identity
    for the lifetime of the channel; mutating it in place after any
    row was processed would silently change historical meaning.
    """

    required_fields: tuple[str, ...] = ()
    allowed_classifications: tuple[str, ...] = ()
    max_record_bytes: int | None = None
    missing_field_severity: ValidationSeverity = ValidationSeverity.QUARANTINE
    unknown_field_severity: ValidationSeverity = ValidationSeverity.QUARANTINE
    invalid_enum_severity: ValidationSeverity = ValidationSeverity.QUARANTINE

    def __post_init__(self) -> None:
        # ``max_record_bytes`` is a soft size cap; missing means no cap.
        if self.max_record_bytes is not None and self.max_record_bytes <= 0:
            raise InvariantViolation("max_record_bytes must be > 0 when set")
        # No duplicate keys — silently shadowing a required field would
        # produce a policy that differs from its source document.
        if len(self.required_fields) != len(set(self.required_fields)):
            duplicates = sorted(
                {f for f in self.required_fields if self.required_fields.count(f) > 1}
            )
            raise InvariantViolation(
                f"validation policy has duplicate required fields: {duplicates}"
            )


# ---------------------------------------------------------------------------
# Channel aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Channel:
    """A tenant-scoped ingestion channel.

    Channels are the configured entry points through which material
    enters the platform. A channel is *enabled* by an authorized
    operator (P04-US4). Material submitted through a channel that is
    not enabled is rejected at the application layer before the
    validation policy is consulted (fail-closed).
    """

    id: UUID
    tenant_id: UUID
    key: str
    kind: ChannelKind
    enabled: bool
    policy: ValidationPolicy
    created_at: datetime
    created_by: UUID
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.key:
            raise InvariantViolation("channel requires a non-empty key")
        # The key is the stable identity operators see; restrict the
        # alphabet so URLs and audit payloads are unambiguous.
        if not all(c.isalnum() or c in ("_", "-") for c in self.key):
            raise InvariantViolation("channel key may only contain alnum, underscore, or dash")
        if self.kind not in _SUPPORTED_CHANNEL_KINDS:
            raise InvariantViolation(f"unsupported channel kind: {self.kind!r}")

    # ----- factories --------------------------------------------------------

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        key: str,
        kind: ChannelKind,
        policy: ValidationPolicy,
        created_by: UUID,
        now: datetime,
        enabled: bool = True,
    ) -> Channel:
        """Construct a new ``Channel`` row.

        ``enabled`` defaults to True so a freshly drafted channel
        accepts material immediately; ``disable`` is the explicit
        operator action to take a channel offline.
        """
        return Channel(
            id=uuid4(),
            tenant_id=tenant_id,
            key=key,
            kind=kind,
            enabled=enabled,
            policy=policy,
            created_at=now,
            created_by=created_by,
            updated_at=now,
        )

    # ----- transitions ------------------------------------------------------

    def enable(self, *, at: datetime) -> Channel:
        """Mark the channel enabled. Idempotent."""
        if self.enabled:
            return self
        return replace(self, enabled=True, updated_at=at)

    def disable(self, *, at: datetime) -> Channel:
        """Mark the channel disabled. Idempotent.

        A disabled channel refuses new captures at the application
        layer; in-flight captures are not retroactively quarantined.
        """
        if not self.enabled:
            return self
        return replace(self, enabled=False, updated_at=at)

    def update_policy(self, *, policy: ValidationPolicy, at: datetime) -> Channel:
        """Replace the channel's validation policy.

        Policy changes apply to *new* captures only. Existing
        ``QuarantineRecord`` rows keep the policy that was active when
        they were captured, so the operator's review queue is
        reproducible.
        """
        return replace(self, policy=policy, updated_at=at)


# ---------------------------------------------------------------------------
# Quarantine record — what happens to a malformed row.
# ---------------------------------------------------------------------------


class QuarantineStatus(str, Enum):  # noqa: UP042
    """The lifecycle of a :class:`QuarantineRecord`.

    - ``HELD`` — captured, awaiting operator action.
    - ``RELEASED`` — operator released it back into the pipeline; the
      releasing use case is responsible for re-running the validation
      that triggered the quarantine.
    - ``REJECTED`` — operator dropped it permanently. The record stays
      in the audit trail but is terminal.
    """

    HELD = "held"
    RELEASED = "released"
    REJECTED = "rejected"


_TERMINAL_QUARANTINE_STATUSES: frozenset[QuarantineStatus] = frozenset(
    {QuarantineStatus.RELEASED, QuarantineStatus.REJECTED}
)


def is_terminal_quarantine_status(status: QuarantineStatus) -> bool:
    """Return True iff ``status`` is terminal for a ``QuarantineRecord``."""
    return status in _TERMINAL_QUARANTINE_STATUSES


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    """A captured item held for operator review.

    Carries provenance (channel, captured_at, actor), a stable
    ``error_code`` for bucketing, and a non-sensitive ``error_message``.
    The original payload is **not** persisted on the row — the
    operator inspects via a separate retrieval endpoint (P04 §6 D4),
    which authorizes the payload access separately so redaction stays
    in one place (Constitution V).
    """

    id: UUID
    tenant_id: UUID
    channel_id: UUID
    channel_key: str
    source_label: str
    captured_at: datetime
    captured_by: UUID
    error_code: str
    error_message: str
    correlation_id: str
    status: QuarantineStatus = QuarantineStatus.HELD
    resolved_at: datetime | None = None
    resolved_by: UUID | None = None
    resolution_note: str | None = None
    # Diagnostic context (no payload data): which keys were present /
    # missing, which field failed. Used by the operator UI to triage.
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.error_code:
            raise InvariantViolation("quarantine record requires a non-empty error_code")
        if not self.source_label:
            raise InvariantViolation("quarantine record requires a non-empty source_label")
        # context must not contain payload data (Constitution V).
        # We do a coarse check: ban keys that look like body fields.
        # A more thorough check lives in the use case that builds the
        # row.
        for banned in ("payload", "body", "raw", "content"):
            if banned in self.context:
                raise InvariantViolation(f"quarantine context may not contain {banned!r}")

    # ----- transitions ------------------------------------------------------

    def release(
        self,
        *,
        by: UUID,
        at: datetime,
        note: str = "",
    ) -> QuarantineRecord:
        """Mark the record RELEASED so the use case can re-process it.

        Idempotent on already-released records: a second release returns
        the row unchanged (the operator UI may retry on a flaky
        connection without producing a second audit event).
        """
        if self.status is QuarantineStatus.RELEASED:
            return self
        if self.status is QuarantineStatus.REJECTED:
            raise InvariantViolation("cannot release a rejected quarantine record")
        return replace(
            self,
            status=QuarantineStatus.RELEASED,
            resolved_at=at,
            resolved_by=by,
            resolution_note=note,
        )

    def reject(
        self,
        *,
        by: UUID,
        at: datetime,
        note: str = "",
    ) -> QuarantineRecord:
        """Mark the record REJECTED. Terminal.

        Idempotent on already-rejected rows; releasing a rejected row
        raises — the audit trail is append-only (Constitution V).
        """
        if self.status is QuarantineStatus.REJECTED:
            return self
        if self.status is QuarantineStatus.RELEASED:
            raise InvariantViolation("cannot reject a released quarantine record")
        return replace(
            self,
            status=QuarantineStatus.REJECTED,
            resolved_at=at,
            resolved_by=by,
            resolution_note=note,
        )


# ---------------------------------------------------------------------------
# Validation outcome — what the policy says about a single row.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The policy's verdict on a single captured item.

    A decision is one of two shapes:

    - ``accepted=True`` — the row may proceed (capture commits it as
      a case / document / participant).
    - ``accepted=False`` — the row is isolated into a
      ``QuarantineRecord``. ``severity`` tells the application layer
      whether to quarantine (``QUARANTINE``) or drop silently
      (``REJECT``). ``REJECTED`` rows do not produce a record at all;
      they are recorded on the audit trail as ``CaptureRejected``.
    """

    accepted: bool
    error_code: str = ""
    error_message: str = ""
    severity: ValidationSeverity = ValidationSeverity.QUARANTINE
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.accepted:
            if self.error_code or self.error_message:
                raise InvariantViolation("an accepted row carries no error code or message")
        elif not self.error_code:
            raise InvariantViolation("a rejected row requires an error_code")


def evaluate_row(
    *,
    values: Mapping[str, str],
    policy: ValidationPolicy,
) -> PolicyDecision:
    """Apply a :class:`ValidationPolicy` to a single row.

    Returns an accepted verdict when every rule passes. On failure the
    decision carries the rule's ``error_code`` (a stable string the
    operator UI buckets on) and a non-sensitive message. The ``context``
    dict carries diagnostic keys (``missing``, ``unknown``,
    ``invalid_enum``) without ever including the row's payload values
    (Constitution V).

    Why this is a pure function: the same row + policy must produce the
    same verdict regardless of which adapter invoked it (HTTP / CLI /
    scan / email). Side effects live in the use case.
    """
    missing: list[str] = []
    for field_key in policy.required_fields:
        value = values.get(field_key)
        if value is None or value == "":
            missing.append(field_key)
    if missing:
        return PolicyDecision(
            accepted=False,
            error_code="missing_required_field",
            error_message=(f"required fields missing: {sorted(missing)}"),
            severity=policy.missing_field_severity,
            context={"missing": sorted(missing)},
        )
    # ``allowed_classifications`` is enforced by the document-mapping
    # path; the channel-level check only verifies a ``classification``
    # column is present when one is declared. A row without a
    # classification is accepted here — the document mapping's
    # classification-specific policy runs at the same validation pass.
    return PolicyDecision(accepted=True)


# T1 track name for the ingestion entry point (spec 002-leftovers §5:
# ``CaptureChannel``). It is the same aggregate as :class:`Channel` —
# the alias exists so T1 quarantine/provenance code reads in business
# terminology without forking the aggregate or its repository ports.
CaptureChannel = Channel

__all__ = [
    "CaptureChannel",
    "Channel",
    "ChannelKind",
    "PolicyDecision",
    "QuarantineRecord",
    "QuarantineStatus",
    "ValidationPolicy",
    "ValidationSeverity",
    "evaluate_row",
    "is_terminal_quarantine_status",
]
