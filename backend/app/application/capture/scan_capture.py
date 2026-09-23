"""Scan capture + safety quarantine (P04-US7 / FM-029).

Scanned material enters the platform through a configured scan channel
and is held in a :class:`QuarantineRecord` until the scan adapter's
result is known. A clean result releases the record and produces a
case document; an infected result keeps the record held with an
explicit ``scan_infected`` error code and never produces a document.
This module is the deterministic core of FM-029 — the actual virus /
OCR / PII scanner is the P14 seam (AGENTS.md §12); the local adapter
accepts the scan result as part of the input so the operator UI can
replay synthetic scans without a live scanner.

# Why quarantine before artifact

A scanned page is not safe to file against a case until the configured
scan result says it is. Filing first and quarantining second would let
a viewer open the document between the two writes; filing it last
keeps the ``documents`` table consistent with the operator's
expectation that a visible case document was scanned clean.

# Flow

1. **Gated authorization** — the use case consults the injected
   :class:`ScanAuthorizationProvider` *before* any other work. The
   feature is marked **Gated** in ``01-deduplicated-feature-map.md:78``
   because implementing a real scanner is a deliberate operational
   decision; the seam is here so the first slice can be wired up
   behind an explicit ``scan_capture_enabled`` flag without granting
   it implicitly.
2. **Authorize** the actor against ``ADMIN_TENANT_CONFIG`` (P11-US2).
   Scan channels are operator-managed.
3. **Resolve the channel** — must be of kind ``SCAN`` and enabled. A
   disabled / wrong-kind channel refuses the capture (fail-closed,
   mirrors ``email_capture`` and ``quarantine_capture``).
4. **Resolve the case** if supplied — same tenant, ``NotFound`` on a
   cross-tenant / unknown id so the API never confirms cross-tenant
   existence (Constitution V).
5. **Hash + store** the scan bytes through ``DocumentStore`` so the
   quarantine record and (clean) document share the same content
   reference and ``sha256``.
6. **Branch on scan result** — see :func:`_branch_on_scan_result`.
   Both branches append a ``ScanCaptured`` audit + outbox row plus a
   ``ScanReleased`` (clean) or no-release (infected) companion event,
   all in the same transaction as the quarantine record.

# Failure behavior

- Auth / validation / not-found errors raise before any DB write.
- A storage ``DependencyUnavailable`` propagates; the caller's
  transaction rollback keeps partial state out of the database.
- An infected result keeps the ``QuarantineRecord`` in ``HELD`` so
  the operator can release or reject via the existing quarantine
  endpoints (``/api/v1/admin/quarantine``).
- The Gated flag is read before any tenant work; a missing flag is
  not silently defaulted to enabled — the use case raises
  :class:`Forbidden` so a misconfigured deployment is loud.

# INTEGRATION_SEAM: scan adapter

The ``scan_result`` field on the command is the seam to the P14 scan
connector. The connector (a future vendor / in-house adapter) reports
``CLEAN`` / ``INFECTED`` plus optional ``detail`` (virus name, OCR
confidence, etc.); this use case consumes that decision verbatim. No
scanning happens inside the platform.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.domain.channel import (
    ChannelKind,
    QuarantineRecord,
    QuarantineStatus,
)
from app.domain.document import Document, DocumentSensitivity, DocumentVersion
from app.domain.errors import Forbidden, NotFound, ValidationFailed
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    ChannelRepository,
    DocumentRepository,
    DocumentStore,
    OutboxWriter,
    QuarantineRepository,
)

# Stable event types + subject types for the audit timeline.
EVENT_SCAN_CAPTURED = "ScanCaptured"
EVENT_SCAN_RELEASED = "ScanReleased"

SUBJECT_TYPE_SCAN = "scan_capture"
SUBJECT_TYPE_DOCUMENT = "document"
SUBJECT_TYPE_QUARANTINE = "quarantine_record"

# Error codes used by the scan-capture verdict. Stable strings — the
# operator UI buckets on them.
ERROR_SCAN_INFECTED = "scan_infected"
ERROR_SCAN_INCONCLUSIVE = "scan_inconclusive"

# Default ceiling for a single scanned page. Scanners usually emit
# images (TIFF / JPEG / PDF) — 25 MiB matches the email-body default
# and is large enough for a 600 dpi color page.
DEFAULT_MAX_SCAN_BYTES = 25 * 1024 * 1024

# Default content type when the operator UI does not specify one.
# The platform does not parse the bytes; it stores them verbatim.
DEFAULT_SCAN_CONTENT_TYPE = "application/octet-stream"


class ScanResult(str, Enum):  # noqa: UP042 - intentional str+Enum for JSON
    """The verdict the configured scan adapter reported.

    Only ``CLEAN`` and ``INFECTED`` are first-class outcomes today.
    ``INCONCLUSIVE`` is reserved for adapters that cannot reach a
    verdict (e.g. an OCR-only scan on a binary blob); the quarantine
    record is held with the ``scan_inconclusive`` error code so the
    operator can re-scan or reject.
    """

    CLEAN = "clean"
    INFECTED = "infected"
    INCONCLUSIVE = "inconclusive"


@runtime_checkable
class ScanAuthorizationProvider(Protocol):
    """Operator-controlled gate for the Gated scan-capture feature.

    AGENTS.md §3 (Issue #3) and ``01-deduplicated-feature-map.md:78``
    mark FM-029 as **Gated**: implementing a real virus / OCR / PII
    scanner is a deliberate operational decision that requires the
    operator's explicit opt-in. The platform reads this seam at the
    very first step of the use case and refuses to do any work when
    the gate is closed — there is no implicit fallback.

    The local adapter is a settings flag (``scan_capture_enabled``).
    A future Azure adapter may consult a FeatureManagement service or
    a Key-Vault-managed secret; the Protocol is the only contract the
    application layer depends on.
    """

    def is_enabled(self) -> bool:
        """Return True iff the scan-capture feature is enabled for this deployment."""


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScanCaptureCommand:
    """The input to :func:`capture_scan`.

    ``tenant_id`` is taken from the authenticated user, not the request
    body. ``case_id`` is optional: a scan without a case is allowed
    but the produced document is currently filed against the case when
    one is supplied (the documents table's ``case_id`` is ``NOT NULL``
    in this slice; a free-standing document is out of scope).
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    channel_id: UUID
    device_id: str
    source_label: str
    scan_result: ScanResult
    scan_detail: str
    content: bytes
    content_type: str = DEFAULT_SCAN_CONTENT_TYPE
    file_name: str = ""
    case_id: UUID | None = None
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class ScanCaptureResult:
    """The outcome of a successful (or infected) capture.

    ``quarantine`` is always populated: the platform quarantines
    before it produces a document so a viewer never sees a scanned
    page before the verdict (mirrors the ``QuarantineRecord`` write in
    :mod:`app.application.channels.manage_channels`). ``document`` is
    populated only when ``released=True`` (clean scan).
    """

    quarantine: QuarantineRecord
    document: Document | None
    document_version: DocumentVersion | None
    released: bool


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


async def capture_scan(
    cmd: ScanCaptureCommand,
    *,
    scan_authorization: ScanAuthorizationProvider,
    channel_repo: ChannelRepository,
    case_repo: CaseRepository,
    quarantine_repo: QuarantineRepository,
    document_repo: DocumentRepository,
    document_store: DocumentStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> ScanCaptureResult:
    """Capture one scanned page + its scan result atomically.

    Failure behavior:
    - Auth / Gated-flag / validation / not-found errors raise before
      any DB write.
    - A storage ``DependencyUnavailable`` propagates; no quarantine,
      document, audit, or outbox row is written.
    - On ``INFECTED`` / ``INCONCLUSIVE`` the ``QuarantineRecord`` is
      persisted in ``HELD``; no ``Document`` row is written; the audit
      + outbox event carries the infected error code so the timeline
      shows the blocked filing.
    - On ``CLEAN`` the ``QuarantineRecord`` is persisted then
      transitioned to ``RELEASED`` (idempotently on a retry); a fresh
      ``Document`` + ``DocumentVersion`` + ``ScanReleased`` audit row
      are committed in the same transaction.
    """
    # 1. Gated authorization. Read first so a misconfigured deployment
    # surfaces immediately — there is no implicit fallback.
    if not scan_authorization.is_enabled():
        raise Forbidden(
            "scan capture is gated and not enabled in this deployment",
        )

    # 2. Authorization (P11-US2). Scan channels are operator-managed.
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="channel"),
    )

    # 3. Pre-flight validation.
    if not cmd.content:
        raise ValidationFailed("scan content is required", field="content")
    if len(cmd.content) > DEFAULT_MAX_SCAN_BYTES:
        raise ValidationFailed(
            f"scan content exceeds configured max size ({DEFAULT_MAX_SCAN_BYTES} bytes)",
            field="content",
        )
    if not cmd.device_id:
        raise ValidationFailed(
            "device_id is required for scan provenance",
            field="device_id",
        )
    if not cmd.source_label:
        raise ValidationFailed(
            "source_label is required for scan quarantine triage",
            field="source_label",
        )

    # 4. Resolve the channel.
    channel = await channel_repo.get(cmd.tenant_id, cmd.channel_id)
    if channel is None:
        raise NotFound(f"channel {cmd.channel_id} not found in tenant")
    if channel.kind is not ChannelKind.SCAN:
        raise ValidationFailed(
            f"channel {cmd.channel_id} is not a scan channel (kind={channel.kind.value})",
            field="channel_id",
        )
    if not channel.enabled:
        raise ValidationFailed(
            f"channel {channel.key!r} is disabled",
            field="channel_id",
        )

    # 5. Resolve the case if supplied. ``NotFound`` on a cross-tenant /
    # unknown id (the API never confirms cross-tenant existence —
    # Constitution V).
    case = None
    if cmd.case_id is not None:
        case = await case_repo.get(cmd.tenant_id, cmd.case_id)
        if case is None:
            raise NotFound(f"case {cmd.case_id} not found in tenant")

    # 6. Hash + store. The DocumentStore is the canonical seam; the
    # application layer is responsible only for the digest + size.
    sha256 = hashlib.sha256(cmd.content).hexdigest()
    size = len(cmd.content)
    storage_ref = await document_store.put(
        tenant_id=cmd.tenant_id,
        content=cmd.content,
        content_type=cmd.content_type,
        sha256=sha256,
    )

    # 7. Branch on the scan result. Both branches write the
    # QuarantineRecord first so the operator's review queue reflects
    # the captured item whether or not it was released.
    now = clock.now()
    return await _branch_on_scan_result(
        cmd=cmd,
        channel=channel,
        case=case,
        sha256=sha256,
        size=size,
        storage_ref=storage_ref,
        now=now,
        quarantine_repo=quarantine_repo,
        document_repo=document_repo,
        audit=audit,
        outbox=outbox,
    )


# ---------------------------------------------------------------------------
# Branch helpers
# ---------------------------------------------------------------------------


async def _branch_on_scan_result(
    *,
    cmd: ScanCaptureCommand,
    channel: Any,
    case: Any,
    sha256: str,
    size: int,
    storage_ref: str,
    now: Any,
    quarantine_repo: QuarantineRepository,
    document_repo: DocumentRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
) -> ScanCaptureResult:
    """Persist the quarantine record + audit/outbox for the verdict.

    Clean path additionally writes the document and transitions the
    record to ``RELEASED``. Infected / inconclusive paths keep the
    record in ``HELD`` and skip the document write.
    """
    error_code = _error_code_for(cmd.scan_result, cmd.scan_detail)
    quarantine = QuarantineRecord(
        id=uuid4(),
        tenant_id=cmd.tenant_id,
        channel_id=channel.id,
        channel_key=channel.key,
        source_label=cmd.source_label,
        captured_at=now,
        captured_by=cmd.actor_id,
        error_code=error_code,
        error_message=_quarantine_message(cmd.scan_result, cmd.scan_detail),
        correlation_id=cmd.correlation_id,
        status=QuarantineStatus.HELD,
        context={
            "device_id": cmd.device_id,
            "scan_result": cmd.scan_result.value,
            "scan_detail": cmd.scan_detail,
            "sha256": sha256,
            "size": size,
            "storage_ref": storage_ref,
            "case_id": str(case.id) if case is not None else "",
        },
    )
    await quarantine_repo.add(quarantine)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_SCAN_CAPTURED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_SCAN,
        subject_id=quarantine.id,
        case_id=case.id if case is not None else None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "channel_id": str(channel.id),
            "channel_key": channel.key,
            "device_id": cmd.device_id,
            "source_label": cmd.source_label,
            "scan_result": cmd.scan_result.value,
            "scan_detail": cmd.scan_detail,
            "error_code": error_code,
            "quarantine_id": str(quarantine.id),
            "sha256": sha256,
            "size": size,
            "storage_ref": storage_ref,
            "case_id": str(case.id) if case is not None else "",
        },
        occurred_at=now,
    )

    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_SCAN_CAPTURED,
        event_id=uuid4(),
        payload={
            "quarantine_id": str(quarantine.id),
            "channel_id": str(channel.id),
            "channel_key": channel.key,
            "tenant_id": str(cmd.tenant_id),
            "device_id": cmd.device_id,
            "source_label": cmd.source_label,
            "scan_result": cmd.scan_result.value,
            "error_code": error_code,
            "occurred_at": now.isoformat(),
        },
    )

    if cmd.scan_result is not ScanResult.CLEAN:
        # Infected / inconclusive: keep the record held; the operator
        # resolves via /admin/quarantine/{id}/release|reject.
        return ScanCaptureResult(
            quarantine=quarantine,
            document=None,
            document_version=None,
            released=False,
        )

    # Clean: transition the record to RELEASED and produce a document
    # in the same transaction so the operator's view is consistent.
    released = quarantine.release(
        by=cmd.actor_id,
        at=now,
        note=f"clean scan from device {cmd.device_id}",
    )
    await quarantine_repo.update(released)

    document: Document | None = None
    document_version: DocumentVersion | None = None
    if case is not None:
        file_name = _scan_file_name(cmd.file_name, cmd.source_label, sha256)
        document = Document.create(
            tenant_id=cmd.tenant_id,
            case_id=case.id,
            file_name=file_name,
            content_type=cmd.content_type,
            classification="Evidence",
            sensitivity=DocumentSensitivity.INTERNAL,
            sha256=sha256,
            size=size,
            storage_ref=storage_ref,
            uploaded_by=cmd.actor_id,
            now=now,
        )
        document_version = DocumentVersion(
            id=uuid4(),
            tenant_id=cmd.tenant_id,
            document_id=document.id,
            version_number=1,
            sha256=sha256,
            size=size,
            storage_ref=storage_ref,
            content_type=cmd.content_type,
            uploaded_by=cmd.actor_id,
            uploaded_at=now,
        )
        await document_repo.add_document_and_version(document=document, version=document_version)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_SCAN_RELEASED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_QUARANTINE,
        subject_id=released.id,
        case_id=case.id if case is not None else None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "channel_id": str(channel.id),
            "channel_key": channel.key,
            "device_id": cmd.device_id,
            "source_label": cmd.source_label,
            "scan_result": cmd.scan_result.value,
            "document_id": str(document.id) if document is not None else "",
            "case_id": str(case.id) if case is not None else "",
            "sha256": sha256,
            "size": size,
            "storage_ref": storage_ref,
        },
        occurred_at=now,
    )

    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_SCAN_RELEASED,
        event_id=uuid4(),
        payload={
            "quarantine_id": str(released.id),
            "channel_id": str(channel.id),
            "channel_key": channel.key,
            "tenant_id": str(cmd.tenant_id),
            "device_id": cmd.device_id,
            "document_id": str(document.id) if document is not None else "",
            "case_id": str(case.id) if case is not None else "",
            "occurred_at": now.isoformat(),
        },
    )

    return ScanCaptureResult(
        quarantine=released,
        document=document,
        document_version=document_version,
        released=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_code_for(result: ScanResult, detail: str) -> str:  # noqa: ARG001
    """Pick the stable ``error_code`` the operator UI displays.

    ``detail`` (the P14 connector's free-text finding) is not stored
    in the code so bucket counts stay comparable across deployments
    (Constitution V — only non-sensitive messages leave the seam).
    """
    if result is ScanResult.INFECTED:
        return ERROR_SCAN_INFECTED
    if result is ScanResult.INCONCLUSIVE:
        return ERROR_SCAN_INCONCLUSIVE
    # CLEAN: the quarantine record exists only as the receipt of the
    # scan; we tag it with the literal ``clean_scan`` so the operator
    # console can distinguish "released after clean" from records that
    # were released by hand after a correction.
    return "clean_scan"


def _quarantine_message(result: ScanResult, detail: str) -> str:
    """Build a non-sensitive, human-readable ``error_message``.

    ``detail`` is the scanner's free-text finding (virus name, OCR
    confidence, etc.). It is allowed in the message because it does
    not carry payload data; the policy that quarantines from the
    channel level (P04-US5 / FM-027) is the unit that enforces the
    payload redaction.
    """
    prefix = result.value
    suffix = f": {detail}" if detail else ""
    return f"scan verdict {prefix}{suffix}"


def _scan_file_name(file_name: str, source_label: str, sha256: str) -> str:
    """Build a stable, safe file name for the released document.

    Mirrors the helper in :mod:`app.application.capture.email_capture`
    so storage refs are predictable across capture channels. The
    sha256 prefix guarantees uniqueness when two scans share a
    source label.
    """
    base = _sanitize_for_filename(file_name or source_label) or "scan"
    return f"{base}__sha-{sha256[:12]}.bin"


def _sanitize_for_filename(value: str) -> str:
    out: list[str] = []
    for ch in value:
        if ch.isalnum() or ch in ("-", "_", "."):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)[:200]


def _user_context(cmd: ScanCaptureCommand) -> Any:
    """Duck-typed ``UserContext`` for the policy (see email_capture)."""
    return _UserProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )


@dataclass(frozen=True, slots=True)
class _UserProxy:
    """Duck-typed ``UserContext`` for the policy."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


__all__ = [
    "DEFAULT_MAX_SCAN_BYTES",
    "DEFAULT_SCAN_CONTENT_TYPE",
    "ERROR_SCAN_INCONCLUSIVE",
    "ERROR_SCAN_INFECTED",
    "EVENT_SCAN_CAPTURED",
    "EVENT_SCAN_RELEASED",
    "SUBJECT_TYPE_DOCUMENT",
    "SUBJECT_TYPE_QUARANTINE",
    "SUBJECT_TYPE_SCAN",
    "ScanAuthorizationProvider",
    "ScanCaptureCommand",
    "ScanCaptureResult",
    "ScanResult",
    "capture_scan",
]
