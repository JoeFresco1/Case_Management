"""Capture persistence parity: SQL repos for T1 surfaces (D-3).

Covers the four durable surfaces from ``specs/002-leftovers/spec.md`` §5
(``ImportPackage`` / ``MappingVersion`` / ``CaptureChannel`` /
``QuarantineRecord``) plus the two inbound idempotency ledgers
(``CapturedEmail`` / ``CapturedWebhook``) that enforce source-record
uniqueness at the database level.

Naming follows ``test_<unit>_<scenario>__<expectation>`` (AGENTS.md R5).
Structure is ``// given`` / ``// when`` / ``// then``. Tests run on the
real SQL adapters against sqlite ``create_all`` (no Docker) with the
tenant listener disabled, so the explicit ``WHERE tenant_id`` filter is
proven sufficient on its own. All fixtures use ``example.test`` domain
values only (no PHI).
"""

from __future__ import annotations

import io
import re
import tokenize
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.channel import (
    Channel,
    ChannelKind,
    QuarantineRecord,
    QuarantineStatus,
    ValidationPolicy,
)
from app.domain.email_capture import CapturedEmail
from app.domain.errors import Conflict
from app.domain.import_job import (
    FieldMapping,
    ImportJob,
    MappingVersion,
)
from app.domain.import_mapping import ImportMapping
from app.domain.webhook_capture import CapturedWebhook, WebhookCommandKind
from app.infrastructure.persistence.imports_http_dependencies import (
    SqlImportJobRecorder,
)
from app.infrastructure.persistence.repositories.captured_email_repository import (
    SqlCapturedEmailRepository,
)
from app.infrastructure.persistence.repositories.captured_webhook_repository import (
    SqlCapturedWebhookRepository,
)
from app.infrastructure.persistence.repositories.channel_repository import (
    SqlChannelRepository,
    SqlQuarantineRepository,
)
from app.infrastructure.persistence.repositories.import_mapping_repository import (
    SqlImportMappingRepository,
)

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ACTOR_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ACTOR_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
FIXED_NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)

_MAPPING_DOC: dict[str, object] = {
    "version": 1,
    "case_type_key": "ServiceRequest",
    "fields": [{"source": "title", "target": "title"}],
    "title_field": "title",
}


def _channel(
    tenant_id: UUID = TENANT_A,
    key: str = "billing-import",
) -> Channel:
    """Build a deterministic import channel for ``tenant_id``."""
    return Channel.create(
        tenant_id=tenant_id,
        key=key,
        kind=ChannelKind.IMPORT,
        policy=ValidationPolicy(required_fields=("title",)),
        created_by=ACTOR_A,
        now=FIXED_NOW,
    )


def _quarantine(
    channel: Channel,
    tenant_id: UUID = TENANT_A,
    source_label: str = "row-7@example.test",
) -> QuarantineRecord:
    """Build a held quarantine record for ``channel``."""
    return QuarantineRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        channel_id=channel.id,
        channel_key=channel.key,
        source_label=source_label,
        captured_at=FIXED_NOW,
        captured_by=ACTOR_A,
        error_code="missing_required_field",
        error_message="required fields missing",
        correlation_id="corr-quarantine-1",
        status=QuarantineStatus.HELD,
        context={"missing": ["title"]},
    )


def _mapping(
    tenant_id: UUID = TENANT_A,
    key: str = "hr_csv",
    version: int = 1,
) -> ImportMapping:
    """Build a draft mapping version for ``tenant_id``."""
    return ImportMapping.draft(
        tenant_id=tenant_id,
        key=key,
        version_number=version,
        mapping_json=dict(_MAPPING_DOC),
        now=FIXED_NOW,
    )


def _job(tenant_id: UUID = TENANT_A) -> ImportJob:
    """Build a pending import package (``ImportPackage`` alias)."""
    mapping = MappingVersion(
        version=1,
        case_type_key="ServiceRequest",
        fields=(FieldMapping(source="title", target="title"),),
        title_field="title",
    )
    return ImportJob.create(
        tenant_id=tenant_id,
        source_label="package-1@example.test",
        source_format="csv",
        mapping=mapping,
        created_by=ACTOR_A,
        now=FIXED_NOW,
        correlation_id="corr-job-1",
    )


def _email(
    channel: Channel,
    tenant_id: UUID = TENANT_A,
    message_id: str = "<row-7@example.test>",
) -> CapturedEmail:
    """Build a captured-email row for ``channel``."""
    return CapturedEmail(
        id=uuid4(),
        tenant_id=tenant_id,
        channel_id=channel.id,
        channel_key=channel.key,
        message_id=message_id,
        sender="sender@example.test",
        recipients=("ops@example.test",),
        subject="invoice 7",
        captured_at=FIXED_NOW,
        captured_by=ACTOR_A,
        destination="document",
        artifact_id=uuid4(),
        artifact_kind="document",
        correlation_id="corr-email-1",
        case_id=None,
    )


def _webhook(
    channel: Channel,
    tenant_id: UUID = TENANT_A,
    webhook_id: str = "evt_2026_09_15_0001",
) -> CapturedWebhook:
    """Build a captured-webhook row for ``channel``."""
    return CapturedWebhook.create(
        tenant_id=tenant_id,
        channel_id=channel.id,
        channel_key=channel.key,
        webhook_id=webhook_id,
        source_system="billing.example.test",
        command_kind=WebhookCommandKind.CASE_NOTE,
        case_id=uuid4(),
        payload={"note": "hello"},
        captured_at=FIXED_NOW,
        captured_by=ACTOR_A,
        correlation_id="corr-webhook-1",
    )


def _webhook_channel(
    tenant_id: UUID = TENANT_A,
    key: str = "billing-events",
) -> Channel:
    """Build a webhook-kind channel for webhook capture tests."""
    return Channel.create(
        tenant_id=tenant_id,
        key=key,
        kind=ChannelKind.WEBHOOK,
        policy=ValidationPolicy(),
        created_by=ACTOR_A,
        now=FIXED_NOW,
    )


@pytest.mark.asyncio
async def test_capture_channel__create_and_get__tenant_scoped(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Channel rows round-trip within the owning tenant."""
    # // given a channel staged for tenant A
    channel = _channel()
    # // when it is added and read back in the same tenant
    async with sql_session_factory() as session:
        await SqlChannelRepository(session).add(channel)
        await session.commit()
    async with sql_session_factory() as session:
        found = await SqlChannelRepository(session).get(TENANT_A, channel.id)
    # // then the row matches and the key lookup resolves
    assert found is not None
    assert found.key == channel.key
    assert found.tenant_id == TENANT_A
    async with sql_session_factory() as session:
        by_key = await SqlChannelRepository(session).get_by_key(TENANT_A, channel.key)
    assert by_key is not None
    assert by_key.id == channel.id


@pytest.mark.asyncio
async def test_capture_channel__cross_tenant__returns_none(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Channel lookups from another tenant return None (no leak)."""
    # // given a channel owned by tenant A
    channel = _channel()
    async with sql_session_factory() as session:
        await SqlChannelRepository(session).add(channel)
        await session.commit()
    # // when tenant B reads by id and by key
    async with sql_session_factory() as session:
        repo = SqlChannelRepository(session)
        by_id = await repo.get(TENANT_B, channel.id)
        by_key = await repo.get_by_key(TENANT_B, channel.key)
        listed = await repo.list(TENANT_B)
    # // then nothing is visible across the boundary
    assert by_id is None
    assert by_key is None
    assert listed == []


@pytest.mark.asyncio
async def test_capture_channel__duplicate_key__violates_uniqueness(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Source-record uniqueness: (tenant_id, key) rejects a second row."""
    # // given one channel with key K in tenant A
    first = _channel(key="dup-key")
    second = _channel(key="dup-key")
    # // when a second row with the same (tenant, key) is flushed
    async with sql_session_factory() as session:
        await SqlChannelRepository(session).add(first)
        await session.commit()
        await SqlChannelRepository(session).add(second)
        with pytest.raises(IntegrityError):
            await session.commit()
    # // then the first row is still the only visible one
    async with sql_session_factory() as session:
        listed = await SqlChannelRepository(session).list(TENANT_A)
    assert [c.key for c in listed].count("dup-key") == 1


@pytest.mark.asyncio
async def test_import_mapping__add_and_get__tenant_scoped(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mapping versions round-trip within the owning tenant."""
    # // given a draft mapping version for tenant A
    mapping = _mapping()
    # // when it is added and read back
    async with sql_session_factory() as session:
        await SqlImportMappingRepository(session).add(mapping)
        await session.commit()
    async with sql_session_factory() as session:
        found = await SqlImportMappingRepository(session).get(TENANT_A, mapping.id)
    # // then the version identity is preserved
    assert found is not None
    assert found.key == mapping.key
    assert found.version_number == 1
    assert found.tenant_id == TENANT_A


@pytest.mark.asyncio
async def test_import_mapping__cross_tenant__returns_none(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mapping lookups from another tenant return None (no leak)."""
    # // given a mapping owned by tenant A
    mapping = _mapping()
    async with sql_session_factory() as session:
        await SqlImportMappingRepository(session).add(mapping)
        await session.commit()
    # // when tenant B reads it
    async with sql_session_factory() as session:
        found = await SqlImportMappingRepository(session).get(TENANT_B, mapping.id)
        listed = await SqlImportMappingRepository(session).list_for_tenant(TENANT_B)
    # // then nothing is visible
    assert found is None
    assert listed == []


@pytest.mark.asyncio
async def test_import_mapping__duplicate_version__raises_conflict(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Source-record uniqueness: (tenant, key, version) is Conflict."""
    # // given version 1 of key K committed for tenant A
    first = _mapping(key="payroll", version=1)
    second = _mapping(key="payroll", version=1)
    async with sql_session_factory() as session:
        await SqlImportMappingRepository(session).add(first)
        await session.commit()
    # // when the same (tenant, key, version) is added again
    async with sql_session_factory() as session:
        with pytest.raises(Conflict):
            await SqlImportMappingRepository(session).add(second)
    # // then only the first version lists for the key
    async with sql_session_factory() as session:
        listed = await SqlImportMappingRepository(session).list_for_key(TENANT_A, "payroll")
    assert len(listed) == 1


@pytest.mark.asyncio
async def test_import_package__record_and_get__tenant_scoped(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Import packages (jobs) round-trip within the owning tenant."""
    # // given a pending import package for tenant A
    job = _job()
    # // when it is recorded and read back
    async with sql_session_factory() as session:
        await SqlImportJobRecorder(session).record(job, dict(_MAPPING_DOC))
        await session.commit()
    async with sql_session_factory() as session:
        found = await SqlImportJobRecorder(session).get(TENANT_A, job.id)
        listed = await SqlImportJobRecorder(session).list_for_tenant(TENANT_A)
    # // then the package identity and snapshot survive
    assert found is not None
    assert found.id == job.id
    assert found.source_label == "package-1@example.test"
    assert [j.id for j in listed] == [job.id]


@pytest.mark.asyncio
async def test_import_package__cross_tenant__returns_none(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Import package reads from another tenant return None/empty."""
    # // given a package owned by tenant A
    job = _job()
    async with sql_session_factory() as session:
        await SqlImportJobRecorder(session).record(job, dict(_MAPPING_DOC))
        await session.commit()
    # // when tenant B reads it
    async with sql_session_factory() as session:
        found = await SqlImportJobRecorder(session).get(TENANT_B, job.id)
        listed = await SqlImportJobRecorder(session).list_for_tenant(TENANT_B)
    # // then nothing is visible
    assert found is None
    assert listed == []


@pytest.mark.asyncio
async def test_quarantine_record__add_and_get__tenant_scoped(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Quarantine rows round-trip within the owning tenant."""
    # // given a channel and one held record for tenant A
    channel = _channel()
    record = _quarantine(channel)
    async with sql_session_factory() as session:
        await SqlChannelRepository(session).add(channel)
        await SqlQuarantineRepository(session).add(record)
        await session.commit()
    # // when the record is read back in the same tenant
    async with sql_session_factory() as session:
        found = await SqlQuarantineRepository(session).get(TENANT_A, record.id)
        listed = await SqlQuarantineRepository(session).list(TENANT_A)
    # // then the hold is visible with its diagnostic context
    assert found is not None
    assert found.status == QuarantineStatus.HELD
    assert found.context == {"missing": ["title"]}
    assert [r.id for r in listed] == [record.id]


@pytest.mark.asyncio
async def test_quarantine_record__cross_tenant__returns_none(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Quarantine reads from another tenant return None/empty."""
    # // given a held record owned by tenant A
    channel = _channel()
    record = _quarantine(channel)
    async with sql_session_factory() as session:
        await SqlChannelRepository(session).add(channel)
        await SqlQuarantineRepository(session).add(record)
        await session.commit()
    # // when tenant B reads it
    async with sql_session_factory() as session:
        repo = SqlQuarantineRepository(session)
        found = await repo.get(TENANT_B, record.id)
        listed = await repo.list(TENANT_B)
        filtered = await repo.list(TENANT_B, status=QuarantineStatus.HELD)
    # // then nothing leaks across the boundary
    assert found is None
    assert listed == []
    assert filtered == []


@pytest.mark.asyncio
async def test_quarantine_record__release__persists_status(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Releasing a held record persists the terminal status for the tenant."""
    # // given a held record for tenant A
    channel = _channel()
    record = _quarantine(channel)
    async with sql_session_factory() as session:
        await SqlChannelRepository(session).add(channel)
        await SqlQuarantineRepository(session).add(record)
        await session.commit()
    # // when the operator releases it
    async with sql_session_factory() as session:
        repo = SqlQuarantineRepository(session)
        held = await repo.get(TENANT_A, record.id)
        assert held is not None
        await repo.update(held.release(by=ACTOR_A, at=FIXED_NOW, note="fixed"))
        await session.commit()
    # // then the released status survives a fresh read
    async with sql_session_factory() as session:
        found = await SqlQuarantineRepository(session).get(TENANT_A, record.id)
    assert found is not None
    assert found.status == QuarantineStatus.RELEASED


@pytest.mark.asyncio
async def test_captured_email__duplicate_message_id__raises_conflict(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Source-record uniqueness: (tenant, channel, message_id) is Conflict."""
    # // given one captured email for (tenant A, channel, Message-Id)
    channel = _channel()
    first = _email(channel)
    second = _email(channel)
    async with sql_session_factory() as session:
        await SqlChannelRepository(session).add(channel)
        await SqlCapturedEmailRepository(session).add(first)
        await session.commit()
    # // when the same Message-Id is captured again on the same channel
    async with sql_session_factory() as session:
        with pytest.raises(Conflict):
            await SqlCapturedEmailRepository(session).add(second)
    # // then the prior row replays by lookup
    async with sql_session_factory() as session:
        found = await SqlCapturedEmailRepository(session).get_by_message_id(
            TENANT_A, channel.id, first.message_id
        )
    assert found is not None
    assert found.id == first.id


@pytest.mark.asyncio
async def test_captured_email__cross_tenant__returns_none(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Captured-email lookups from another tenant return None."""
    # // given a capture owned by tenant A
    channel = _channel()
    record = _email(channel)
    async with sql_session_factory() as session:
        await SqlChannelRepository(session).add(channel)
        await SqlCapturedEmailRepository(session).add(record)
        await session.commit()
    # // when tenant B looks up the same (channel, message_id)
    async with sql_session_factory() as session:
        found = await SqlCapturedEmailRepository(session).get_by_message_id(
            TENANT_B, channel.id, record.message_id
        )
    # // then nothing is visible
    assert found is None


@pytest.mark.asyncio
async def test_captured_webhook__add_and_get__tenant_scoped(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Captured webhooks round-trip within the owning tenant."""
    # // given a webhook channel and one capture for tenant A
    channel = _webhook_channel()
    record = _webhook(channel)
    async with sql_session_factory() as session:
        await SqlChannelRepository(session).add(channel)
        await SqlCapturedWebhookRepository(session).add(record)
        await session.commit()
    # // when the capture is read back in the same tenant
    async with sql_session_factory() as session:
        found = await SqlCapturedWebhookRepository(session).get_by_webhook_id(
            TENANT_A, channel.id, record.webhook_id
        )
    # // then the provenance survives with its digest
    assert found is not None
    assert found.id == record.id
    assert found.payload_digest == record.payload_digest
    assert found.payload == {"note": "hello"}


@pytest.mark.asyncio
async def test_captured_webhook__cross_tenant__returns_none(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Captured-webhook lookups from another tenant return None."""
    # // given a capture owned by tenant A
    channel = _webhook_channel()
    record = _webhook(channel)
    async with sql_session_factory() as session:
        await SqlChannelRepository(session).add(channel)
        await SqlCapturedWebhookRepository(session).add(record)
        await session.commit()
    # // when tenant B looks up the same (channel, webhook_id)
    async with sql_session_factory() as session:
        found = await SqlCapturedWebhookRepository(session).get_by_webhook_id(
            TENANT_B, channel.id, record.webhook_id
        )
    # // then nothing is visible
    assert found is None


@pytest.mark.asyncio
async def test_captured_webhook__duplicate_webhook_id__raises_conflict(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Source-record uniqueness: (tenant, channel, webhook_id) is Conflict."""
    # // given one capture for (tenant A, channel, webhook id)
    channel = _webhook_channel()
    first = _webhook(channel)
    second = _webhook(channel)
    async with sql_session_factory() as session:
        await SqlChannelRepository(session).add(channel)
        await SqlCapturedWebhookRepository(session).add(first)
        await session.commit()
    # // when the same webhook id is captured again
    async with sql_session_factory() as session:
        with pytest.raises(Conflict):
            await SqlCapturedWebhookRepository(session).add(second)
    # // then the prior row replays and the tenant-scoped twin does not collide
    async with sql_session_factory() as session:
        found = await SqlCapturedWebhookRepository(session).get_by_webhook_id(
            TENANT_A, channel.id, first.webhook_id
        )
    assert found is not None
    assert found.id == first.id
    # // given the same webhook id under tenant B is a different ledger
    other_channel = _webhook_channel(tenant_id=TENANT_B, key="other-chan")
    other = _webhook(other_channel, tenant_id=TENANT_B)
    async with sql_session_factory() as session:
        await SqlChannelRepository(session).add(other_channel)
        # // when recorded under tenant B it must not collide
        await SqlCapturedWebhookRepository(session).add(other)
        await session.commit()
    async with sql_session_factory() as session:
        other_found = await SqlCapturedWebhookRepository(session).get_by_webhook_id(
            TENANT_B, other_channel.id, other.webhook_id
        )
    assert other_found is not None


def test_capture_migration__portable_sql__no_forbidden_tokens() -> None:
    """The new migration uses portable types only and is reversible."""
    # // given the D-3 migration source, linted like alembic env.py
    # // (tokenize: prose in the module docstring does not count as DDL)
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0030_capture_quarantine_webhook.py"
    )
    source = path.read_text(encoding="utf-8")
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    significant = [
        tok
        for tok in tokens
        if tok.type
        not in {
            tokenize.COMMENT,
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.ENDMARKER,
        }
        and not (tok.type == tokenize.STRING and "\n" in tok.string)
    ]
    code = "\n".join(tok.string for tok in significant)
    # // when scanned for dialect-specific tokens
    forbidden = ("JSONB", "GIN", "ILIKE", "tsvector", "gen_random_uuid")
    # // then none appear in executable code and upgrade/downgrade both exist
    for token in forbidden:
        assert re.search(rf"\b{re.escape(token)}\b", code) is None
    assert "def upgrade" in source
    assert "def downgrade" in source
    assert "quarantine_records" in source
    assert "captured_webhooks" in source
    assert "uq_captured_webhooks_tenant_channel_webhook" in source
