"""Tests for scan capture + safety quarantine (P04-US7 / FM-029).

Acceptance scenarios from ``specs/pillars/P04-capture-ingestion/pillar.md``
US7 and ``specs/pillars/01-deduplicated-feature-map.md`` row FM-029:

1. **Scan enters quarantine flagged** — a scanned page first lands as
   a ``QuarantineRecord`` carrying device + source provenance; the
   audit + outbox row ``ScanCaptured`` is appended in the same
   transaction.
2. **Clean scan releases to document** — a clean verdict transitions
   the ``QuarantineRecord`` to ``RELEASED`` and writes a
   ``Document`` + ``DocumentVersion`` in the same transaction; the
   audit + outbox row ``ScanReleased`` is appended.
3. **Infected scan blocks and audit** — an infected verdict keeps
   the ``QuarantineRecord`` in ``HELD``, no ``Document`` is written,
   and the audit row carries the ``scan_infected`` error code.
4. **Gated authorization** — when the operator has not opted in via
   the ``ScanAuthorizationProvider``, the use case refuses with
   ``Forbidden`` *before* any DB write.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per
AGENTS.md §4.R5. Tests are deterministic: a fixed clock, fixed UUIDs.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.application.capture import scan_capture as scan_capture_uc
from app.domain.case import Case, CaseReference
from app.domain.case_type import (
    CaseType,
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.channel import (
    Channel,
    ChannelKind,
    QuarantineRecord,
    QuarantineStatus,
    ValidationPolicy,
)
from app.domain.document import DocumentSensitivity
from app.domain.errors import Forbidden, NotFound, ValidationFailed
from app.domain.scan_batch import ScanBatch, ScanBatchStatus
from tests.application.cases._fakes import (
    FixedClock,
    InMemoryAuditStore,
    InMemoryCaseRepository,
    InMemoryOutbox,
)
from tests.application.documents._fakes import (
    InMemoryDocumentRepository,
    InMemoryDocumentStore,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
ADMIN_USER = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
REVIEWER_USER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

FIXED_NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)

DEVICE_ID = "scanner-floor-2"
SOURCE_LABEL = "Intake counter — 2026-09-02"
SCAN_BYTES = b"%PDF-1.4\n% synthetic scan bytes\n"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class InMemoryChannelRepository:
    """Minimal in-memory ``ChannelRepository`` for scan-capture tests."""

    def __init__(self) -> None:
        self._rows: dict[UUID, Channel] = {}

    async def get(self, tenant_id: UUID, channel_id: UUID) -> Channel | None:
        row = self._rows.get(channel_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def get_by_key(self, tenant_id: UUID, key: str) -> Channel | None:
        for row in self._rows.values():
            if row.tenant_id == tenant_id and row.key == key:
                return row
        return None

    async def list(self, tenant_id: UUID, *, enabled_only: bool = False) -> list[Channel]:
        out = [r for r in self._rows.values() if r.tenant_id == tenant_id]
        if enabled_only:
            out = [r for r in out if r.enabled]
        return out

    async def add(self, channel: Channel) -> None:
        self._rows[channel.id] = channel

    async def update(self, channel: Channel) -> None:
        self._rows[channel.id] = channel


class InMemoryQuarantineRepository:
    """In-memory ``QuarantineRepository`` (mirrors ``test_quarantine.py``)."""

    def __init__(self) -> None:
        self._rows: dict[UUID, QuarantineRecord] = {}
        self._order: list[UUID] = []

    async def get(self, tenant_id: UUID, quarantine_id: UUID) -> QuarantineRecord | None:
        row = self._rows.get(quarantine_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def list(
        self,
        tenant_id: UUID,
        *,
        channel_id: UUID | None = None,
        status: QuarantineStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[QuarantineRecord]:
        out = [self._rows[i] for i in reversed(self._order) if self._rows[i].tenant_id == tenant_id]
        if channel_id is not None:
            out = [r for r in out if r.channel_id == channel_id]
        if status is not None:
            out = [r for r in out if r.status is status]
        return out[offset : offset + limit]

    async def add(self, record: QuarantineRecord) -> None:
        self._rows[record.id] = record
        self._order.append(record.id)

    async def update(self, record: QuarantineRecord) -> None:
        self._rows[record.id] = record


class StaticScanAuthorization:
    """In-memory :class:`ScanAuthorizationProvider` for tests.

    Default is enabled so the positive-path tests run. The gated-off
    test instantiates ``StaticScanAuthorization(enabled=False)`` to
    verify the refusal path.
    """

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self.calls: int = 0

    def is_enabled(self) -> bool:
        self.calls += 1
        return self._enabled


# ---------------------------------------------------------------------------
# Fixtures + builders
# ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


def _build_case_type(*, tenant_id: UUID) -> tuple[CaseType, CaseTypeVersion]:
    schema = CaseTypeSchema(
        fields=(CaseTypeField(key="text", label="Text", type="text", required=True),),
        document_classifications=("Document", "Evidence"),
        rules=(),
    )
    version = CaseTypeVersion(
        id=uuid4(),
        tenant_id=tenant_id,
        case_type_id=uuid4(),
        version_number=1,
        schema=schema,
        status=CaseTypeVersionStatus.PUBLISHED,
        published_at=FIXED_NOW,
        published_by=uuid4(),
    )
    case_type = CaseType(
        id=version.case_type_id,
        tenant_id=tenant_id,
        key="ServiceRequest",
        name="Service Request",
        description="",
        sensitivity_default="internal",
        current_version_id=version.id,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
    return case_type, version


def _seed_case(
    *,
    tenant_id: UUID,
    case_type_version: CaseTypeVersion,
    case_repo: InMemoryCaseRepository,
) -> Case:
    case = Case.create(
        tenant_id=tenant_id,
        reference=CaseReference("C-ACME-2026-000001"),
        case_type_version=case_type_version,
        title="Scan target case",
        description="",
        data={"text": "Scan target"},
        priority="normal",
        created_by=ADMIN_USER,
        now=FIXED_NOW,
    )
    case_repo._cases.append(case)  # noqa: SLF001
    case_repo.set_tenant_short(tenant_id, "ACME")
    return case


def _seed_scan_channel(
    *,
    tenant_id: UUID,
    channel_repo: InMemoryChannelRepository,
    key: str = "scan-main",
    enabled: bool = True,
) -> Channel:
    return Channel.create(
        tenant_id=tenant_id,
        key=key,
        kind=ChannelKind.SCAN,
        policy=ValidationPolicy(),
        created_by=ADMIN_USER,
        now=FIXED_NOW,
        enabled=enabled,
    )


def _cmd(
    *,
    tenant_id: UUID = ACME_TENANT,
    actor_id: UUID = ADMIN_USER,
    actor_roles: tuple[str, ...] = ("process_admin",),
    channel_id: UUID | None = None,
    case_id: UUID | None = None,
    scan_result: scan_capture_uc.ScanResult = scan_capture_uc.ScanResult.CLEAN,
    scan_detail: str = "",
    content: bytes = SCAN_BYTES,
) -> scan_capture_uc.ScanCaptureCommand:
    return scan_capture_uc.ScanCaptureCommand(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_roles=actor_roles,
        channel_id=channel_id or uuid4(),
        device_id=DEVICE_ID,
        source_label=SOURCE_LABEL,
        scan_result=scan_result,
        scan_detail=scan_detail,
        content=content,
        case_id=case_id,
        correlation_id="corr-scan-001",
    )


# ---------------------------------------------------------------------------
# 1. Scan enters quarantine flagged (clean → released + document)
# ---------------------------------------------------------------------------


async def test_capture_scan__clean_result__quarantines_then_releases_with_document() -> None:
    """A clean scan first stages a ``QuarantineRecord``, transitions it
    to ``RELEASED``, writes a ``Document`` + ``DocumentVersion``, and
    appends the ``ScanCaptured`` + ``ScanReleased`` audit / outbox
    events — atomically."""
    case_type, version = _build_case_type(tenant_id=ACME_TENANT)
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case = _seed_case(
        tenant_id=ACME_TENANT,
        case_type_version=version,
        case_repo=case_repo,
    )

    channel_repo = InMemoryChannelRepository()
    channel = _seed_scan_channel(tenant_id=ACME_TENANT, channel_repo=channel_repo)
    await channel_repo.add(channel)

    quarantine_repo = InMemoryQuarantineRepository()
    document_repo = InMemoryDocumentRepository()
    document_store = InMemoryDocumentStore()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    authz = StaticScanAuthorization(enabled=True)

    cmd = _cmd(channel_id=channel.id, case_id=case.id)
    result = await scan_capture_uc.capture_scan(
        cmd,
        scan_authorization=authz,
        channel_repo=channel_repo,
        case_repo=case_repo,
        quarantine_repo=quarantine_repo,
        document_repo=document_repo,
        document_store=document_store,
        audit=audit,
        outbox=outbox,
        clock=FixedClock(FIXED_NOW),
    )

    # Gated check ran exactly once and was honored.
    assert authz.calls == 1

    # Bytes landed at the storage adapter.
    assert len(document_store.puts) == 1
    put = document_store.puts[0]
    assert put.tenant_id == ACME_TENANT
    assert put.content == SCAN_BYTES

    # Quarantine record is RELEASED with device + scan provenance.
    assert len(quarantine_repo._order) == 1  # noqa: SLF001
    quarantine = next(iter(quarantine_repo._rows.values()))  # noqa: SLF001
    assert quarantine.status is QuarantineStatus.RELEASED
    assert quarantine.tenant_id == ACME_TENANT
    assert quarantine.channel_id == channel.id
    assert quarantine.channel_key == channel.key
    assert quarantine.source_label == SOURCE_LABEL
    assert quarantine.error_code == "clean_scan"
    assert quarantine.context["device_id"] == DEVICE_ID
    assert quarantine.context["scan_result"] == "clean"
    assert quarantine.resolution_note is not None
    assert "clean scan" in quarantine.resolution_note

    # Document + version committed.
    assert len(document_repo.added) == 1
    document, document_version = document_repo.added[0]
    assert document.tenant_id == ACME_TENANT
    assert document.case_id == case.id
    assert document.classification == "Evidence"
    assert document.sensitivity is DocumentSensitivity.INTERNAL
    assert document.uploaded_by == ADMIN_USER
    assert document.current_version == 1
    assert document_version.document_id == document.id
    assert document_version.version_number == 1

    # Audit: ScanCaptured + ScanReleased in order.
    assert len(audit.events) == 2
    assert audit.events[0].event_type == scan_capture_uc.EVENT_SCAN_CAPTURED
    assert audit.events[0].subject_type == scan_capture_uc.SUBJECT_TYPE_SCAN
    assert audit.events[0].subject_id == quarantine.id
    assert audit.events[0].case_id == case.id
    assert audit.events[0].payload["device_id"] == DEVICE_ID
    assert audit.events[0].payload["scan_result"] == "clean"
    assert audit.events[0].payload["quarantine_id"] == str(quarantine.id)

    assert audit.events[1].event_type == scan_capture_uc.EVENT_SCAN_RELEASED
    assert audit.events[1].subject_type == scan_capture_uc.SUBJECT_TYPE_QUARANTINE
    assert audit.events[1].subject_id == quarantine.id
    assert audit.events[1].payload["document_id"] == str(document.id)
    assert audit.events[1].payload["case_id"] == str(case.id)

    # Outbox: both events mirror the audit payloads.
    assert len(outbox.events) == 2
    assert outbox.events[0].event_type == scan_capture_uc.EVENT_SCAN_CAPTURED
    assert outbox.events[0].payload["quarantine_id"] == str(quarantine.id)
    assert outbox.events[1].event_type == scan_capture_uc.EVENT_SCAN_RELEASED
    assert outbox.events[1].payload["document_id"] == str(document.id)

    # Result wraps the released quarantine + document.
    assert result.released is True
    assert result.quarantine.id == quarantine.id
    assert result.quarantine.status is QuarantineStatus.RELEASED
    assert result.document is not None
    assert result.document.id == document.id


# ---------------------------------------------------------------------------
# 2. Infected scan blocks + audit
# ---------------------------------------------------------------------------


async def test_capture_scan__infected_result__holds_quarantine_and_audits_block() -> None:
    """An infected scan keeps the ``QuarantineRecord`` in ``HELD``,
    writes no document, and appends a ``ScanCaptured`` audit row
    carrying the ``scan_infected`` error code."""
    case_type, version = _build_case_type(tenant_id=ACME_TENANT)
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case = _seed_case(
        tenant_id=ACME_TENANT,
        case_type_version=version,
        case_repo=case_repo,
    )

    channel_repo = InMemoryChannelRepository()
    channel = _seed_scan_channel(tenant_id=ACME_TENANT, channel_repo=channel_repo)
    await channel_repo.add(channel)

    quarantine_repo = InMemoryQuarantineRepository()
    document_repo = InMemoryDocumentRepository()
    document_store = InMemoryDocumentStore()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    authz = StaticScanAuthorization(enabled=True)

    cmd = _cmd(
        channel_id=channel.id,
        case_id=case.id,
        scan_result=scan_capture_uc.ScanResult.INFECTED,
        scan_detail="EICAR-Test-Signature",
    )
    result = await scan_capture_uc.capture_scan(
        cmd,
        scan_authorization=authz,
        channel_repo=channel_repo,
        case_repo=case_repo,
        quarantine_repo=quarantine_repo,
        document_repo=document_repo,
        document_store=document_store,
        audit=audit,
        outbox=outbox,
        clock=FixedClock(FIXED_NOW),
    )

    # Quarantine is held with the infected error code + provenance.
    assert len(quarantine_repo._order) == 1  # noqa: SLF001
    quarantine = next(iter(quarantine_repo._rows.values()))  # noqa: SLF001
    assert quarantine.status is QuarantineStatus.HELD
    assert quarantine.error_code == scan_capture_uc.ERROR_SCAN_INFECTED
    assert "infected" in quarantine.error_message
    assert quarantine.context["device_id"] == DEVICE_ID
    assert quarantine.context["scan_result"] == "infected"

    # No document was written — the infected page is blocked.
    assert document_repo.added == []
    assert result.document is None
    assert result.document_version is None
    assert result.released is False

    # Audit + outbox carry exactly one ScanCaptured event.
    assert len(audit.events) == 1
    assert audit.events[0].event_type == scan_capture_uc.EVENT_SCAN_CAPTURED
    assert audit.events[0].payload["error_code"] == scan_capture_uc.ERROR_SCAN_INFECTED
    assert audit.events[0].payload["scan_result"] == "infected"
    assert audit.events[0].payload["scan_detail"] == "EICAR-Test-Signature"

    assert len(outbox.events) == 1
    assert outbox.events[0].event_type == scan_capture_uc.EVENT_SCAN_CAPTURED
    assert outbox.events[0].payload["error_code"] == scan_capture_uc.ERROR_SCAN_INFECTED


async def test_capture_scan__inconclusive_result__holds_quarantine_with_inconclusive_code() -> None:
    """An inconclusive verdict holds the record with the
    ``scan_inconclusive`` error code so the operator can re-scan or
    reject via the existing quarantine endpoints."""
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")

    channel_repo = InMemoryChannelRepository()
    channel = _seed_scan_channel(tenant_id=ACME_TENANT, channel_repo=channel_repo)
    await channel_repo.add(channel)

    quarantine_repo = InMemoryQuarantineRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    authz = StaticScanAuthorization(enabled=True)

    cmd = _cmd(
        channel_id=channel.id,
        case_id=None,
        scan_result=scan_capture_uc.ScanResult.INCONCLUSIVE,
        scan_detail="OCR confidence below threshold",
    )
    result = await scan_capture_uc.capture_scan(
        cmd,
        scan_authorization=authz,
        channel_repo=channel_repo,
        case_repo=case_repo,
        quarantine_repo=quarantine_repo,
        document_repo=InMemoryDocumentRepository(),
        document_store=InMemoryDocumentStore(),
        audit=audit,
        outbox=outbox,
        clock=FixedClock(FIXED_NOW),
    )

    assert result.released is False
    assert result.document is None
    assert result.quarantine.status is QuarantineStatus.HELD
    assert result.quarantine.error_code == scan_capture_uc.ERROR_SCAN_INCONCLUSIVE
    # Only ScanCaptured — no ScanReleased for an inconclusive verdict.
    assert [e.event_type for e in audit.events] == [scan_capture_uc.EVENT_SCAN_CAPTURED]


# ---------------------------------------------------------------------------
# 3. Gated authorization refuses when disabled
# ---------------------------------------------------------------------------


async def test_capture_scan__gated_disabled__raises_forbidden_before_any_write() -> None:
    """When the ``ScanAuthorizationProvider`` reports disabled, the
    use case raises ``Forbidden`` and writes nothing — no quarantine,
    no audit, no outbox, no document, no storage write."""
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")

    channel_repo = InMemoryChannelRepository()
    quarantine_repo = InMemoryQuarantineRepository()
    document_repo = InMemoryDocumentRepository()
    document_store = InMemoryDocumentStore()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    authz = StaticScanAuthorization(enabled=False)

    cmd = _cmd()
    with pytest.raises(Forbidden):
        await scan_capture_uc.capture_scan(
            cmd,
            scan_authorization=authz,
            channel_repo=channel_repo,
            case_repo=case_repo,
            quarantine_repo=quarantine_repo,
            document_repo=document_repo,
            document_store=document_store,
            audit=audit,
            outbox=outbox,
            clock=FixedClock(FIXED_NOW),
        )

    assert authz.calls == 1
    assert document_store.puts == []
    assert document_repo.added == []
    assert quarantine_repo._rows == {}  # noqa: SLF001
    assert audit.events == []
    assert outbox.events == []


# ---------------------------------------------------------------------------
# 4. Authorization — non-admin actor is denied (clean path)
# ---------------------------------------------------------------------------


async def test_capture_scan__actor_without_admin_tenant_config__raises_forbidden() -> None:
    """The ``reviewer`` role lacks ``admin.tenant_config`` and is
    refused before any storage write — the same gate that guards
    ``/api/v1/admin/channels`` guards this endpoint."""
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")

    channel_repo = InMemoryChannelRepository()
    channel = _seed_scan_channel(tenant_id=ACME_TENANT, channel_repo=channel_repo)
    await channel_repo.add(channel)

    quarantine_repo = InMemoryQuarantineRepository()
    document_repo = InMemoryDocumentRepository()
    document_store = InMemoryDocumentStore()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    authz = StaticScanAuthorization(enabled=True)

    cmd = _cmd(
        actor_id=REVIEWER_USER,
        actor_roles=("reviewer",),
        channel_id=channel.id,
        case_id=uuid4(),
    )
    with pytest.raises(Forbidden):
        await scan_capture_uc.capture_scan(
            cmd,
            scan_authorization=authz,
            channel_repo=channel_repo,
            case_repo=case_repo,
            quarantine_repo=quarantine_repo,
            document_repo=document_repo,
            document_store=document_store,
            audit=audit,
            outbox=outbox,
            clock=FixedClock(FIXED_NOW),
        )

    assert document_store.puts == []
    assert document_repo.added == []
    assert quarantine_repo._rows == {}  # noqa: SLF001
    assert audit.events == []
    assert outbox.events == []


# ---------------------------------------------------------------------------
# 5. Wrong-kind / disabled channel
# ---------------------------------------------------------------------------


async def test_capture_scan__non_scan_channel__rejected() -> None:
    """A channel whose ``kind`` is not ``ChannelKind.SCAN`` is refused
    with ``ValidationFailed``."""
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")

    channel_repo = InMemoryChannelRepository()
    import_channel = _seed_scan_channel(
        tenant_id=ACME_TENANT,
        channel_repo=channel_repo,
        key="import-main",
    )
    import_channel = Channel.create(
        tenant_id=ACME_TENANT,
        key="import-main",
        kind=ChannelKind.IMPORT,
        policy=ValidationPolicy(),
        created_by=ADMIN_USER,
        now=FIXED_NOW,
        enabled=True,
    )
    await channel_repo.add(import_channel)

    quarantine_repo = InMemoryQuarantineRepository()
    authz = StaticScanAuthorization(enabled=True)

    cmd = _cmd(channel_id=import_channel.id)
    with pytest.raises(ValidationFailed):
        await scan_capture_uc.capture_scan(
            cmd,
            scan_authorization=authz,
            channel_repo=channel_repo,
            case_repo=case_repo,
            quarantine_repo=quarantine_repo,
            document_repo=InMemoryDocumentRepository(),
            document_store=InMemoryDocumentStore(),
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=FixedClock(FIXED_NOW),
        )


async def test_capture_scan__disabled_channel__rejected() -> None:
    """A scan channel that has been disabled refuses new captures
    (fail-closed) and writes nothing."""
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")

    channel_repo = InMemoryChannelRepository()
    disabled = _seed_scan_channel(
        tenant_id=ACME_TENANT,
        channel_repo=channel_repo,
        enabled=False,
    )
    await channel_repo.add(disabled)

    quarantine_repo = InMemoryQuarantineRepository()
    document_store = InMemoryDocumentStore()
    authz = StaticScanAuthorization(enabled=True)

    cmd = _cmd(channel_id=disabled.id)
    with pytest.raises(ValidationFailed):
        await scan_capture_uc.capture_scan(
            cmd,
            scan_authorization=authz,
            channel_repo=channel_repo,
            case_repo=case_repo,
            quarantine_repo=quarantine_repo,
            document_repo=InMemoryDocumentRepository(),
            document_store=document_store,
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=FixedClock(FIXED_NOW),
        )

    assert document_store.puts == []
    assert quarantine_repo._rows == {}  # noqa: SLF001


# ---------------------------------------------------------------------------
# 6. Unknown case — surfaces as NotFound
# ---------------------------------------------------------------------------


async def test_capture_scan__unknown_case__raises_not_found() -> None:
    """A case id that does not exist within the actor's tenant raises
    ``NotFound`` and writes nothing."""
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")

    channel_repo = InMemoryChannelRepository()
    channel = _seed_scan_channel(tenant_id=ACME_TENANT, channel_repo=channel_repo)
    await channel_repo.add(channel)

    quarantine_repo = InMemoryQuarantineRepository()
    document_store = InMemoryDocumentStore()
    authz = StaticScanAuthorization(enabled=True)

    cmd = _cmd(channel_id=channel.id, case_id=uuid4())
    with pytest.raises(NotFound):
        await scan_capture_uc.capture_scan(
            cmd,
            scan_authorization=authz,
            channel_repo=channel_repo,
            case_repo=case_repo,
            quarantine_repo=quarantine_repo,
            document_repo=InMemoryDocumentRepository(),
            document_store=document_store,
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=FixedClock(FIXED_NOW),
        )

    assert document_store.puts == []
    assert quarantine_repo._rows == {}  # noqa: SLF001


# ---------------------------------------------------------------------------
# 7. Tenant isolation — cross-tenant channel does not leak
# ---------------------------------------------------------------------------


async def test_capture_scan__cross_tenant_channel__raises_not_found() -> None:
    """A scan attempt on a channel that belongs to a different tenant
    surfaces as ``NotFound`` so the API never confirms cross-tenant
    existence (Constitution V)."""
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case_repo.set_tenant_short(OTHER_TENANT, "OTHER")

    channel_repo = InMemoryChannelRepository()
    other_channel = _seed_scan_channel(
        tenant_id=OTHER_TENANT,
        channel_repo=channel_repo,
        key="scan-other",
    )
    await channel_repo.add(other_channel)

    quarantine_repo = InMemoryQuarantineRepository()
    document_store = InMemoryDocumentStore()
    authz = StaticScanAuthorization(enabled=True)

    cmd = _cmd(channel_id=other_channel.id)
    with pytest.raises(NotFound):
        await scan_capture_uc.capture_scan(
            cmd,
            scan_authorization=authz,
            channel_repo=channel_repo,
            case_repo=case_repo,
            quarantine_repo=quarantine_repo,
            document_repo=InMemoryDocumentRepository(),
            document_store=document_store,
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=FixedClock(FIXED_NOW),
        )

    assert document_store.puts == []
    assert quarantine_repo._rows == {}  # noqa: SLF001


# ---------------------------------------------------------------------------
# 8. Empty content / oversized payload — validation failures
# ---------------------------------------------------------------------------


async def test_capture_scan__empty_content__raises_validation_failed() -> None:
    """An empty payload raises ``ValidationFailed`` before any write."""
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")

    channel_repo = InMemoryChannelRepository()
    channel = _seed_scan_channel(tenant_id=ACME_TENANT, channel_repo=channel_repo)
    await channel_repo.add(channel)

    quarantine_repo = InMemoryQuarantineRepository()
    document_store = InMemoryDocumentStore()
    authz = StaticScanAuthorization(enabled=True)

    cmd = _cmd(channel_id=channel.id, content=b"")
    with pytest.raises(ValidationFailed):
        await scan_capture_uc.capture_scan(
            cmd,
            scan_authorization=authz,
            channel_repo=channel_repo,
            case_repo=case_repo,
            quarantine_repo=quarantine_repo,
            document_repo=InMemoryDocumentRepository(),
            document_store=document_store,
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=FixedClock(FIXED_NOW),
        )

    assert document_store.puts == []
    assert quarantine_repo._rows == {}  # noqa: SLF001


async def test_capture_scan__missing_device_id__raises_validation_failed() -> None:
    """``device_id`` is mandatory provenance (the operator UI must
    supply the scanning device id even for synthetic replays)."""
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")

    channel_repo = InMemoryChannelRepository()
    channel = _seed_scan_channel(tenant_id=ACME_TENANT, channel_repo=channel_repo)
    await channel_repo.add(channel)

    quarantine_repo = InMemoryQuarantineRepository()
    document_store = InMemoryDocumentStore()
    authz = StaticScanAuthorization(enabled=True)

    cmd = scan_capture_uc.ScanCaptureCommand(
        tenant_id=ACME_TENANT,
        actor_id=ADMIN_USER,
        actor_roles=("process_admin",),
        channel_id=channel.id,
        device_id="",
        source_label=SOURCE_LABEL,
        scan_result=scan_capture_uc.ScanResult.CLEAN,
        scan_detail="",
        content=SCAN_BYTES,
        correlation_id="corr-scan-001",
    )
    with pytest.raises(ValidationFailed):
        await scan_capture_uc.capture_scan(
            cmd,
            scan_authorization=authz,
            channel_repo=channel_repo,
            case_repo=case_repo,
            quarantine_repo=quarantine_repo,
            document_repo=InMemoryDocumentRepository(),
            document_store=document_store,
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=FixedClock(FIXED_NOW),
        )

    assert document_store.puts == []
    assert quarantine_repo._rows == {}  # noqa: SLF001


# ---------------------------------------------------------------------------
# 9. Provenance: quarantine + document share sha256; separation preserves it
# ---------------------------------------------------------------------------


async def test_capture_scan__clean_result__quarantine_and_document_share_sha256() -> None:
    """A clean scan's quarantine record and document share the same
    content address, and the batch separation projection preserves it."""
    case_type, version = _build_case_type(tenant_id=ACME_TENANT)
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case = _seed_case(
        tenant_id=ACME_TENANT,
        case_type_version=version,
        case_repo=case_repo,
    )

    channel_repo = InMemoryChannelRepository()
    channel = _seed_scan_channel(tenant_id=ACME_TENANT, channel_repo=channel_repo)
    await channel_repo.add(channel)

    quarantine_repo = InMemoryQuarantineRepository()
    document_repo = InMemoryDocumentRepository()
    document_store = InMemoryDocumentStore()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    authz = StaticScanAuthorization(enabled=True)

    cmd = _cmd(channel_id=channel.id, case_id=case.id)
    result = await scan_capture_uc.capture_scan(
        cmd,
        scan_authorization=authz,
        channel_repo=channel_repo,
        case_repo=case_repo,
        quarantine_repo=quarantine_repo,
        document_repo=document_repo,
        document_store=document_store,
        audit=audit,
        outbox=outbox,
        clock=FixedClock(FIXED_NOW),
    )

    # // then quarantine context sha256 equals the document sha256.
    expected_sha = hashlib.sha256(SCAN_BYTES).hexdigest()
    assert result.quarantine.context["sha256"] == expected_sha
    assert result.document is not None
    assert result.document.sha256 == expected_sha
    assert result.document_version is not None
    assert result.document_version.sha256 == expected_sha

    # // and a scan batch carrying the same page preserves provenance
    # into its separation descriptors.
    batch = ScanBatch.open(
        tenant_id=ACME_TENANT,
        channel_id=channel.id,
        channel_key=channel.key,
        device_id=DEVICE_ID,
        source_label=SOURCE_LABEL,
        created_by=ADMIN_USER,
        now=FIXED_NOW,
        case_id=case.id,
    )
    batch = batch.add_page(
        tenant_id=ACME_TENANT,
        sha256=expected_sha,
        size=len(SCAN_BYTES),
        storage_ref="ref-scan-1",
        device_id=DEVICE_ID,
        now=FIXED_NOW,
    )
    batch = batch.mark_ready(tenant_id=ACME_TENANT, now=FIXED_NOW)
    assert batch.status is not ScanBatchStatus.VERIFIED
    verified = batch.verify(tenant_id=ACME_TENANT, by=ADMIN_USER, now=FIXED_NOW)
    sources = verified.to_separation_source()
    assert len(sources) == 1
    assert sources[0].sha256 == expected_sha
    assert sources[0].device_id == DEVICE_ID
    assert sources[0].batch_id == batch.id


__all__ = []
