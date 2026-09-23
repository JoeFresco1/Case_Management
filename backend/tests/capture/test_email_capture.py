"""Tests for inbound email capture (P04-US6 / FM-028).

Acceptance scenarios from ``specs/pillars/P04-capture-ingestion/pillar.md``
US6:

1. **Email creates a document with channel + message provenance** —
   an authorized caller posts an email to ``POST /api/v1/capture/email``
   and the use case persists one ``Document`` (and its initial
   ``DocumentVersion``) plus one ``CapturedEmail`` row plus one
   ``EmailCaptured`` audit + outbox event in the same transaction.
2. **Duplicate Message-Id is idempotent** — submitting the same
   ``Message-Id`` twice returns the prior result without writing a
   second document or audit row.
3. **Authorization** — the ``intake`` role lacks ``document.upload``
   and is denied before any storage write.
4. **Disabled / wrong-kind channel** — a channel of kind
   ``ChannelKind.IMPORT`` or a disabled email channel refuses the
   capture (fail-closed).
5. **Tenant isolation** — a cross-tenant Message-Id lookup never
   returns another tenant's row.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per
AGENTS.md §4.R5. Tests are deterministic: a fixed clock, fixed UUIDs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.application.capture import email_capture as email_capture_uc
from app.domain.case import Case, CaseReference
from app.domain.case_type import (
    CaseType,
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.channel import Channel, ChannelKind, ValidationPolicy
from app.domain.document import DocumentSensitivity
from app.domain.email_capture import CapturedEmail
from app.domain.errors import Conflict, Forbidden, NotFound, ValidationFailed
from tests.application.cases._fakes import (
    FixedClock,
    InMemoryAuditStore,
    InMemoryCaseRepository,
    InMemoryCaseTypeRepository,
    InMemoryCaseTypeVersionRepository,
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
REVIEWER_USER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
INTAKE_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ADMIN_USER = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

FIXED_NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)

MESSAGE_ID = "<abc.123@example.test>"
SENDER = "alice@example.test"
RECIPIENTS = ("bob@example.test",)
SUBJECT = "Hello world"
EMAIL_BODY = (
    b"From: alice@example.test\r\n"
    b"To: bob@example.test\r\n"
    b"Subject: Hello world\r\n"
    b"Message-Id: <abc.123@example.test>\r\n"
    b"\r\n"
    b"Body of the email.\r\n"
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class InMemoryCapturedEmailRepository:
    """In-memory ``CapturedEmailRepository``.

    Mirrors the SQL adapter's ``(tenant_id, channel_id, message_id)``
    uniqueness — a second ``add`` with the same key raises
    ``Conflict`` so the use case can detect the race.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, UUID, str], CapturedEmail] = {}

    async def get_by_message_id(
        self,
        tenant_id: UUID,
        channel_id: UUID,
        message_id: str,
    ) -> CapturedEmail | None:
        return self._rows.get((tenant_id, channel_id, message_id))

    async def add(self, record: CapturedEmail) -> None:
        key = (record.tenant_id, record.channel_id, record.message_id)
        if key in self._rows:
            raise Conflict("captured email already exists for this channel and Message-Id")
        self._rows[key] = record


class InMemoryChannelRepository:
    """Minimal in-memory ``ChannelRepository`` for the email-capture tests."""

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


@pytest.fixture
def case_type_repo() -> InMemoryCaseTypeRepository:
    return InMemoryCaseTypeRepository()


@pytest.fixture
def case_type_version_repo() -> InMemoryCaseTypeVersionRepository:
    return InMemoryCaseTypeVersionRepository()


def _build_case_type(
    *, tenant_id: UUID, classifications: tuple[str, ...] = ("Correspondence",)
) -> tuple[CaseType, CaseTypeVersion]:
    schema = CaseTypeSchema(
        fields=(CaseTypeField(key="title", label="Title", type="text", required=True),),
        document_classifications=classifications,
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
        title="Need onboarding help",
        description="",
        data={"title": "Need onboarding help"},
        priority="normal",
        created_by=ADMIN_USER,
        now=FIXED_NOW,
    )
    case_repo._cases.append(case)  # noqa: SLF001
    case_repo.set_tenant_short(tenant_id, "ACME")
    return case


def _seed_channel(
    *,
    tenant_id: UUID,
    channel_repo: InMemoryChannelRepository,
    kind: ChannelKind = ChannelKind.EMAIL,
    key: str = "email-main",
    enabled: bool = True,
) -> Channel:
    """Sync helper used inside a synchronous fixture.

    The test cases below are async; use ``_seed_channel_async`` to
    await the repository's ``add`` call.
    """
    return Channel.create(
        tenant_id=tenant_id,
        key=key,
        kind=kind,
        policy=ValidationPolicy(),
        created_by=ADMIN_USER,
        now=FIXED_NOW,
        enabled=enabled,
    )


async def _seed_channel_async(
    *,
    tenant_id: UUID,
    channel_repo: InMemoryChannelRepository,
    kind: ChannelKind = ChannelKind.EMAIL,
    key: str = "email-main",
    enabled: bool = True,
) -> Channel:
    channel = Channel.create(
        tenant_id=tenant_id,
        key=key,
        kind=kind,
        policy=ValidationPolicy(),
        created_by=ADMIN_USER,
        now=FIXED_NOW,
        enabled=enabled,
    )
    await channel_repo.add(channel)
    return channel


def _cmd(
    *,
    tenant_id: UUID = ACME_TENANT,
    actor_id: UUID = REVIEWER_USER,
    actor_roles: tuple[str, ...] = ("reviewer",),
    channel_id: UUID | None = None,
    case_id: UUID | None = None,
    message_id: str = MESSAGE_ID,
    sender: str = SENDER,
    recipients: tuple[str, ...] = RECIPIENTS,
    subject: str = SUBJECT,
    body: bytes = EMAIL_BODY,
) -> email_capture_uc.CaptureEmailCommand:
    return email_capture_uc.CaptureEmailCommand(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_roles=actor_roles,
        channel_id=channel_id or uuid4(),
        message_id=message_id,
        sender=sender,
        recipients=recipients,
        subject=subject,
        body=body,
        case_id=case_id,
        correlation_id="corr-email-001",
    )


# ---------------------------------------------------------------------------
# 1. Happy path — email creates a document with provenance
# ---------------------------------------------------------------------------


async def test_capture_email__happy_path__creates_document_and_provenance(
    clock: FixedClock,
    case_type_repo: InMemoryCaseTypeRepository,
    case_type_version_repo: InMemoryCaseTypeVersionRepository,
) -> None:
    """A first-time Message-Id produces a document, an initial version,
    a captured-email provenance row, an ``EmailCaptured`` audit event,
    and an ``EmailCaptured`` outbox event — all in the same transaction."""
    case_type, version = _build_case_type(tenant_id=ACME_TENANT)
    case_type_repo.add(case_type)
    case_type_version_repo.add(version)

    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case = _seed_case(
        tenant_id=ACME_TENANT,
        case_type_version=version,
        case_repo=case_repo,
    )

    channel_repo = InMemoryChannelRepository()
    channel = await _seed_channel_async(tenant_id=ACME_TENANT, channel_repo=channel_repo)

    document_repo = InMemoryDocumentRepository()
    document_store = InMemoryDocumentStore()
    captured_repo = InMemoryCapturedEmailRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _cmd(channel_id=channel.id, case_id=case.id)
    result = await email_capture_uc.capture_email(
        cmd,
        channel_repo=channel_repo,
        case_repo=case_repo,
        document_repo=document_repo,
        document_store=document_store,
        captured_email_repo=captured_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # Document + initial version staged exactly once.
    assert len(document_repo.added) == 1
    document, document_version = document_repo.added[0]
    assert document.tenant_id == ACME_TENANT
    assert document.case_id == case.id
    assert document.content_type == "message/rfc822"
    assert document.classification == "Correspondence"
    assert document.sensitivity is DocumentSensitivity.INTERNAL
    assert document.uploaded_by == REVIEWER_USER
    assert document.size == len(EMAIL_BODY)
    assert document.current_version == 1
    assert document_version.document_id == document.id
    assert document_version.version_number == 1

    # Bytes landed at the storage adapter exactly once.
    assert len(document_store.puts) == 1
    put = document_store.puts[0]
    assert put.tenant_id == ACME_TENANT
    assert put.content == EMAIL_BODY

    # CapturedEmail provenance row stored.
    assert len(captured_repo._rows) == 1  # noqa: SLF001
    captured = next(iter(captured_repo._rows.values()))  # noqa: SLF001
    assert captured.tenant_id == ACME_TENANT
    assert captured.channel_id == channel.id
    assert captured.channel_key == channel.key
    assert captured.message_id == MESSAGE_ID
    assert captured.destination == "document"
    assert captured.artifact_id == document.id
    assert captured.artifact_kind == "document"
    assert captured.case_id == case.id
    assert captured.captured_by == REVIEWER_USER
    assert captured.correlation_id == "corr-email-001"

    # Audit row carries channel + message provenance.
    assert len(audit.events) == 1
    audit_event = audit.events[0]
    assert audit_event.event_type == email_capture_uc.EVENT_EMAIL_CAPTURED
    assert audit_event.tenant_id == ACME_TENANT
    assert audit_event.subject_type == email_capture_uc.SUBJECT_TYPE_EMAIL
    assert audit_event.subject_id == captured.id
    assert audit_event.case_id == case.id
    assert audit_event.payload["message_id"] == MESSAGE_ID
    assert audit_event.payload["channel_id"] == str(channel.id)
    assert audit_event.payload["channel_key"] == channel.key
    assert audit_event.payload["artifact_id"] == str(document.id)
    assert audit_event.payload["case_id"] == str(case.id)

    # Outbox row mirrors the audit payload.
    assert len(outbox.events) == 1
    outbox_event = outbox.events[0]
    assert outbox_event.event_type == email_capture_uc.EVENT_EMAIL_CAPTURED
    assert outbox_event.tenant_id == ACME_TENANT
    assert outbox_event.payload["message_id"] == MESSAGE_ID
    assert outbox_event.payload["artifact_id"] == str(document.id)

    # Result wraps the freshly captured row.
    assert result.replayed is False
    assert result.captured.id == captured.id
    assert result.document is not None
    assert result.document.id == document.id


# ---------------------------------------------------------------------------
# 2. Duplicate Message-Id is idempotent
# ---------------------------------------------------------------------------


async def test_capture_email__duplicate_message_id__replays_prior_result(
    clock: FixedClock,
    case_type_repo: InMemoryCaseTypeRepository,
    case_type_version_repo: InMemoryCaseTypeVersionRepository,
) -> None:
    """A second submission with the same Message-Id returns the prior
    ``CapturedEmail`` row, writes no new metadata/version/captured row,
    and emits no new audit / outbox events."""
    case_type, version = _build_case_type(tenant_id=ACME_TENANT)
    case_type_repo.add(case_type)
    case_type_version_repo.add(version)

    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case = _seed_case(
        tenant_id=ACME_TENANT,
        case_type_version=version,
        case_repo=case_repo,
    )

    channel_repo = InMemoryChannelRepository()
    channel = await _seed_channel_async(tenant_id=ACME_TENANT, channel_repo=channel_repo)

    document_repo = InMemoryDocumentRepository()
    document_store = InMemoryDocumentStore()
    captured_repo = InMemoryCapturedEmailRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _cmd(channel_id=channel.id, case_id=case.id)

    # First capture.
    first = await email_capture_uc.capture_email(
        cmd,
        channel_repo=channel_repo,
        case_repo=case_repo,
        document_repo=document_repo,
        document_store=document_store,
        captured_email_repo=captured_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert first.replayed is False

    # Second capture with the same Message-Id.
    second = await email_capture_uc.capture_email(
        cmd,
        channel_repo=channel_repo,
        case_repo=case_repo,
        document_repo=document_repo,
        document_store=document_store,
        captured_email_repo=captured_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert second.replayed is True
    assert second.captured.id == first.captured.id
    assert second.document is None
    assert second.document_version is None

    # Only one document was written; only one audit + outbox event.
    assert len(document_repo.added) == 1
    assert len(document_store.puts) == 1
    assert len(captured_repo._rows) == 1  # noqa: SLF001
    assert len(audit.events) == 1
    assert len(outbox.events) == 1


# ---------------------------------------------------------------------------
# 3. Authorization — non-DOCUMENT_UPLOAD actor is denied
# ---------------------------------------------------------------------------


async def test_capture_email__actor_without_document_upload__raises_forbidden(
    clock: FixedClock,
    case_type_repo: InMemoryCaseTypeRepository,
    case_type_version_repo: InMemoryCaseTypeVersionRepository,
) -> None:
    """The ``intake`` role lacks ``document.upload`` and is refused
    before any storage write is attempted."""
    case_type, version = _build_case_type(tenant_id=ACME_TENANT)
    case_type_repo.add(case_type)
    case_type_version_repo.add(version)

    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case = _seed_case(
        tenant_id=ACME_TENANT,
        case_type_version=version,
        case_repo=case_repo,
    )

    channel_repo = InMemoryChannelRepository()
    channel = await _seed_channel_async(tenant_id=ACME_TENANT, channel_repo=channel_repo)

    document_repo = InMemoryDocumentRepository()
    document_store = InMemoryDocumentStore()
    captured_repo = InMemoryCapturedEmailRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _cmd(
        actor_id=INTAKE_USER,
        actor_roles=("intake",),
        channel_id=channel.id,
        case_id=case.id,
    )

    with pytest.raises(Forbidden):
        await email_capture_uc.capture_email(
            cmd,
            channel_repo=channel_repo,
            case_repo=case_repo,
            document_repo=document_repo,
            document_store=document_store,
            captured_email_repo=captured_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )

    # Nothing was written.
    assert document_store.puts == []
    assert document_repo.added == []
    assert captured_repo._rows == {}  # noqa: SLF001
    assert audit.events == []
    assert outbox.events == []


# ---------------------------------------------------------------------------
# 4. Wrong-kind / disabled channel
# ---------------------------------------------------------------------------


async def test_capture_email__non_email_channel__rejected(
    clock: FixedClock,
    case_type_repo: InMemoryCaseTypeRepository,
    case_type_version_repo: InMemoryCaseTypeVersionRepository,
) -> None:
    """A channel whose ``kind`` is not ``ChannelKind.EMAIL`` is refused."""
    case_type, version = _build_case_type(tenant_id=ACME_TENANT)
    case_type_repo.add(case_type)
    case_type_version_repo.add(version)

    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case = _seed_case(
        tenant_id=ACME_TENANT,
        case_type_version=version,
        case_repo=case_repo,
    )

    channel_repo = InMemoryChannelRepository()
    import_channel = await _seed_channel_async(
        tenant_id=ACME_TENANT,
        channel_repo=channel_repo,
        kind=ChannelKind.IMPORT,
        key="import-main",
    )

    cmd = _cmd(channel_id=import_channel.id, case_id=case.id)
    with pytest.raises(ValidationFailed):
        await email_capture_uc.capture_email(
            cmd,
            channel_repo=channel_repo,
            case_repo=case_repo,
            document_repo=InMemoryDocumentRepository(),
            document_store=InMemoryDocumentStore(),
            captured_email_repo=InMemoryCapturedEmailRepository(),
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=clock,
        )


async def test_capture_email__disabled_channel__rejected(
    clock: FixedClock,
    case_type_repo: InMemoryCaseTypeRepository,
    case_type_version_repo: InMemoryCaseTypeVersionRepository,
) -> None:
    """An email channel that has been disabled refuses new captures
    (fail-closed)."""
    case_type, version = _build_case_type(tenant_id=ACME_TENANT)
    case_type_repo.add(case_type)
    case_type_version_repo.add(version)

    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case = _seed_case(
        tenant_id=ACME_TENANT,
        case_type_version=version,
        case_repo=case_repo,
    )

    channel_repo = InMemoryChannelRepository()
    disabled_channel = await _seed_channel_async(
        tenant_id=ACME_TENANT,
        channel_repo=channel_repo,
        enabled=False,
    )

    cmd = _cmd(channel_id=disabled_channel.id, case_id=case.id)
    with pytest.raises(ValidationFailed):
        await email_capture_uc.capture_email(
            cmd,
            channel_repo=channel_repo,
            case_repo=case_repo,
            document_repo=InMemoryDocumentRepository(),
            document_store=InMemoryDocumentStore(),
            captured_email_repo=InMemoryCapturedEmailRepository(),
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 5. Tenant isolation — cross-tenant Message-Id lookup never leaks
# ---------------------------------------------------------------------------


async def test_capture_email__cross_tenant_message_id__does_not_leak(
    clock: FixedClock,
    case_type_repo: InMemoryCaseTypeRepository,
    case_type_version_repo: InMemoryCaseTypeVersionRepository,
) -> None:
    """A second tenant re-submitting the same Message-Id under a
    different tenant must NOT see the first tenant's row; the lookup
    is tenant-scoped."""
    case_type, version = _build_case_type(tenant_id=ACME_TENANT)
    case_type_repo.add(case_type)
    case_type_version_repo.add(version)

    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case_repo.set_tenant_short(OTHER_TENANT, "OTHER")
    case = _seed_case(
        tenant_id=ACME_TENANT,
        case_type_version=version,
        case_repo=case_repo,
    )

    # Seed a second tenant + case for the cross-tenant lookup.
    _, other_version = _build_case_type(tenant_id=OTHER_TENANT)
    other_case = Case.create(
        tenant_id=OTHER_TENANT,
        reference=CaseReference("C-OTHER-2026-000001"),
        case_type_version=other_version,
        title="Other tenant case",
        description="",
        data={"title": "Other tenant case"},
        priority="normal",
        created_by=ADMIN_USER,
        now=FIXED_NOW,
    )
    case_repo._cases.append(other_case)  # noqa: SLF001

    channel_repo = InMemoryChannelRepository()
    acme_channel = await _seed_channel_async(
        tenant_id=ACME_TENANT,
        channel_repo=channel_repo,
        key="email-acme",
    )
    other_channel = await _seed_channel_async(
        tenant_id=OTHER_TENANT,
        channel_repo=channel_repo,
        key="email-other",
    )

    document_repo = InMemoryDocumentRepository()
    document_store = InMemoryDocumentStore()
    captured_repo = InMemoryCapturedEmailRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    # First tenant captures the Message-Id.
    await email_capture_uc.capture_email(
        _cmd(channel_id=acme_channel.id, case_id=case.id),
        channel_repo=channel_repo,
        case_repo=case_repo,
        document_repo=document_repo,
        document_store=document_store,
        captured_email_repo=captured_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # Second tenant tries the SAME Message-Id on its own channel —
    # the use case must NOT short-circuit to the first tenant's row.
    second = await email_capture_uc.capture_email(
        _cmd(
            tenant_id=OTHER_TENANT,
            channel_id=other_channel.id,
            case_id=other_case.id,
        ),
        channel_repo=channel_repo,
        case_repo=case_repo,
        document_repo=document_repo,
        document_store=document_store,
        captured_email_repo=captured_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert second.replayed is False
    # Two CapturedEmail rows, one per tenant + channel.
    assert len(captured_repo._rows) == 2  # noqa: SLF001
    # Two documents written, two audit events.
    assert len(document_repo.added) == 2
    assert len(audit.events) == 2


# ---------------------------------------------------------------------------
# 6. Unknown case — surfaces as NotFound
# ---------------------------------------------------------------------------


async def test_capture_email__unknown_case__raises_not_found(
    clock: FixedClock,
) -> None:
    """A case id that does not exist within the actor's tenant raises
    ``NotFound`` and writes nothing."""
    channel_repo = InMemoryChannelRepository()
    channel = await _seed_channel_async(tenant_id=ACME_TENANT, channel_repo=channel_repo)

    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")

    document_store = InMemoryDocumentStore()
    document_repo = InMemoryDocumentRepository()
    captured_repo = InMemoryCapturedEmailRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _cmd(channel_id=channel.id, case_id=uuid4())
    with pytest.raises(NotFound):
        await email_capture_uc.capture_email(
            cmd,
            channel_repo=channel_repo,
            case_repo=case_repo,
            document_repo=document_repo,
            document_store=document_store,
            captured_email_repo=captured_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )

    assert document_store.puts == []
    assert document_repo.added == []
    assert captured_repo._rows == {}  # noqa: SLF001
    assert audit.events == []
    assert outbox.events == []


__all__ = []
