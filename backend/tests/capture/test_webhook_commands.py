"""Tests for inbound webhook commands (P04-US8 / FM-030, ADR-007).

Acceptance scenarios from ``specs/pillars/P04-capture-ingestion/pillar.md``
US8:

1. **Authorized webhook POST creates a case state change with
   provenance** — an authorized caller POSTs an HMAC-signed webhook
   to ``POST /api/v1/webhooks/{channel}`` and the use case persists
   one ``CapturedWebhook`` row plus one ``WebhookCaptured`` audit
   event plus one ``CaseTransitioned`` audit + outbox event in the
   same transaction.
2. **Duplicate webhook id is idempotent** — submitting the same
   ``X-Webhook-Id`` twice returns the prior result without writing a
   second command or audit row.
3. **Invalid HMAC is rejected with 401** — a request whose
   ``X-Webhook-Signature`` does not match the channel's shared
   secret is refused at the router before any use case is invoked.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per
AGENTS.md §4.R5. Tests are deterministic: a fixed clock, fixed UUIDs.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.application.capture import webhook_commands as webhook_uc
from app.domain.case import Case, CaseReference
from app.domain.case_type import (
    CaseType,
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.channel import Channel, ChannelKind, ValidationPolicy
from app.domain.errors import Forbidden, NotFound, ValidationFailed
from app.domain.webhook_capture import (
    CapturedWebhook,
    WebhookCommandKind,
    is_valid_webhook_id,
    payload_digest,
)
from app.interfaces.http.routers.webhooks import (
    compute_signature,
)
from tests.application.cases._fakes import (
    FixedClock,
    InMemoryAuditStore,
    InMemoryCaseRepository,
    InMemoryCaseTypeRepository,
    InMemoryCaseTypeVersionRepository,
    InMemoryOutbox,
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

CHANNEL_KEY = "billing-events"
SOURCE_SYSTEM = "billing.example.test"
WEBHOOK_ID = "evt_2026_09_02_0001"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class InMemoryCapturedWebhookRepository:
    """In-memory ``CapturedWebhookRepository``.

    Mirrors the SQL adapter's ``(tenant_id, channel_id, webhook_id)``
    uniqueness — a second ``add`` with the same key raises
    ``Conflict`` so the use case can detect the race.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, UUID, str], CapturedWebhook] = {}

    async def get_by_webhook_id(
        self,
        tenant_id: UUID,
        channel_id: UUID,
        webhook_id: str,
    ) -> CapturedWebhook | None:
        return self._rows.get((tenant_id, channel_id, webhook_id))

    async def add(self, record: CapturedWebhook) -> None:
        key = (record.tenant_id, record.channel_id, record.webhook_id)
        if key in self._rows:
            raise RuntimeError("captured webhook already exists for this channel and webhook_id")
        self._rows[key] = record


class InMemoryChannelRepository:
    """Minimal in-memory ``ChannelRepository`` for the webhook tests."""

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


class _StaticSecretProvider:
    """In-memory ``SecretProvider`` that returns a single fixed secret.

    Implements just the surface the router needs (``get(name)``) and
    raises ``KeyError`` on unknown names so the test can verify the
    "unknown channel secret" 401 branch.
    """

    def __init__(self, secret: str) -> None:
        self._secret = secret

    async def get(self, name: str) -> str:  # type: ignore[override]
        if name == f"webhook_secret_{CHANNEL_KEY}":
            return self._secret
        raise KeyError(f"unknown secret: {name}")


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


def _build_case_type(*, tenant_id: UUID) -> tuple[CaseType, CaseTypeVersion]:
    schema = CaseTypeSchema(
        fields=(CaseTypeField(key="title", label="Title", type="text", required=True),),
        document_classifications=("Correspondence",),
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


async def _seed_webhook_channel(
    *,
    tenant_id: UUID,
    channel_repo: InMemoryChannelRepository,
    key: str = CHANNEL_KEY,
    enabled: bool = True,
) -> Channel:
    channel = Channel.create(
        tenant_id=tenant_id,
        key=key,
        kind=ChannelKind.WEBHOOK,
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
    webhook_id: str = WEBHOOK_ID,
    source_system: str = SOURCE_SYSTEM,
    command_kind: WebhookCommandKind = WebhookCommandKind.CASE_TRANSITION,
    payload: dict | None = None,
) -> webhook_uc.WebhookCommand:
    return webhook_uc.WebhookCommand(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_roles=actor_roles,
        channel_id=channel_id or uuid4(),
        source_system=source_system,
        webhook_id=webhook_id,
        command_kind=command_kind,
        case_id=case_id or uuid4(),
        payload=payload
        or {
            "transition_key": "submit_for_review",
            "target_state": "in_review",
        },
        correlation_id="corr-wh-001",
    )


# ---------------------------------------------------------------------------
# 1. Happy path — valid webhook creates a case state change with provenance
# ---------------------------------------------------------------------------


async def test_apply_webhook_command__happy_path__creates_provenance_and_state_change(
    clock: FixedClock,
    case_type_repo: InMemoryCaseTypeRepository,
    case_type_version_repo: InMemoryCaseTypeVersionRepository,
) -> None:
    """A first-time webhook id produces a ``CapturedWebhook`` row,
    a ``WebhookCaptured`` audit event, a ``CaseTransitioned`` audit
    event, and a ``CaseTransitioned`` outbox event — all in the
    same transaction."""
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
    channel = await _seed_webhook_channel(tenant_id=ACME_TENANT, channel_repo=channel_repo)

    captured_repo = InMemoryCapturedWebhookRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _cmd(channel_id=channel.id, case_id=case.id)
    result = await webhook_uc.apply_webhook_command(
        cmd,
        channel_repo=channel_repo,
        case_repo=case_repo,
        captured_webhook_repo=captured_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # CapturedWebhook provenance row stored exactly once.
    assert len(captured_repo._rows) == 1  # noqa: SLF001
    captured = next(iter(captured_repo._rows.values()))  # noqa: SLF001
    assert captured.tenant_id == ACME_TENANT
    assert captured.channel_id == channel.id
    assert captured.channel_key == CHANNEL_KEY
    assert captured.webhook_id == WEBHOOK_ID
    assert captured.source_system == SOURCE_SYSTEM
    assert captured.command_kind is WebhookCommandKind.CASE_TRANSITION
    assert captured.case_id == case.id
    assert captured.captured_by == REVIEWER_USER
    assert captured.correlation_id == "corr-wh-001"
    # The aggregate computes the digest itself.
    assert captured.payload_digest == payload_digest(cmd.payload)

    # Two audit rows: WebhookCaptured + CaseTransitioned.
    assert len(audit.events) == 2
    webhook_event = next(
        e for e in audit.events if e.event_type == webhook_uc.EVENT_WEBHOOK_CAPTURED
    )
    transition_event = next(
        e for e in audit.events if e.event_type == webhook_uc.EVENT_CASE_TRANSITIONED
    )

    # WebhookCaptured audit row carries webhook provenance.
    assert webhook_event.tenant_id == ACME_TENANT
    assert webhook_event.actor_id == REVIEWER_USER
    assert webhook_event.actor_type == "webhook"
    assert webhook_event.subject_type == webhook_uc.SUBJECT_TYPE_WEBHOOK
    assert webhook_event.subject_id == captured.id
    assert webhook_event.case_id == case.id
    assert webhook_event.payload["channel_id"] == str(channel.id)
    assert webhook_event.payload["channel_key"] == CHANNEL_KEY
    assert webhook_event.payload["webhook_id"] == WEBHOOK_ID
    assert webhook_event.payload["source_system"] == SOURCE_SYSTEM
    assert webhook_event.payload["command_kind"] == "case.transition"
    assert webhook_event.payload["case_id"] == str(case.id)
    assert webhook_event.payload["payload_digest"] == captured.payload_digest

    # CaseTransitioned audit row is causally linked to the webhook
    # and carries the transition key + target state.
    assert transition_event.tenant_id == ACME_TENANT
    assert transition_event.subject_type == webhook_uc.SUBJECT_TYPE_CASE
    assert transition_event.subject_id == case.id
    assert transition_event.case_id == case.id
    assert transition_event.causation_id == str(captured.id)
    assert transition_event.payload["webhook_capture_id"] == str(captured.id)
    assert transition_event.payload["webhook_id"] == WEBHOOK_ID
    assert transition_event.payload["transition_key"] == "submit_for_review"
    assert transition_event.payload["target_state"] == "in_review"
    assert transition_event.payload["command_kind"] == "case.transition"

    # One outbox row mirrors the CaseTransitioned payload so the
    # workflow engine (P02) can pick it up.
    assert len(outbox.events) == 1
    outbox_event = outbox.events[0]
    assert outbox_event.event_type == webhook_uc.EVENT_CASE_TRANSITIONED
    assert outbox_event.tenant_id == ACME_TENANT
    assert outbox_event.payload["case_id"] == str(case.id)
    assert outbox_event.payload["webhook_capture_id"] == str(captured.id)
    assert outbox_event.payload["transition_key"] == "submit_for_review"
    assert outbox_event.payload["target_state"] == "in_review"

    # Result wraps the freshly captured row.
    assert result.replayed is False
    assert result.captured.id == captured.id
    assert result.applied_command["command_kind"] == "case.transition"
    assert result.applied_command["case_id"] == str(case.id)
    assert result.applied_command["transition_key"] == "submit_for_review"
    assert result.applied_command["target_state"] == "in_review"


# ---------------------------------------------------------------------------
# 2. Duplicate webhook id is idempotent
# ---------------------------------------------------------------------------


async def test_apply_webhook_command__duplicate_webhook_id__replays_prior_result(
    clock: FixedClock,
    case_type_repo: InMemoryCaseTypeRepository,
    case_type_version_repo: InMemoryCaseTypeVersionRepository,
) -> None:
    """A second submission with the same ``X-Webhook-Id`` returns the
    prior ``CapturedWebhook`` row, writes no new audit/outbox rows,
    and does NOT apply a second case command."""
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
    channel = await _seed_webhook_channel(tenant_id=ACME_TENANT, channel_repo=channel_repo)

    captured_repo = InMemoryCapturedWebhookRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _cmd(channel_id=channel.id, case_id=case.id)

    first = await webhook_uc.apply_webhook_command(
        cmd,
        channel_repo=channel_repo,
        case_repo=case_repo,
        captured_webhook_repo=captured_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert first.replayed is False

    second = await webhook_uc.apply_webhook_command(
        cmd,
        channel_repo=channel_repo,
        case_repo=case_repo,
        captured_webhook_repo=captured_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert second.replayed is True
    assert second.captured.id == first.captured.id
    assert second.applied_command["command_kind"] == "case.transition"
    assert second.applied_command["case_id"] == str(case.id)

    # Exactly one captured row, two audit rows (one webhook + one
    # transition), and one outbox row.
    assert len(captured_repo._rows) == 1  # noqa: SLF001
    assert len(audit.events) == 2
    assert len(outbox.events) == 1


# ---------------------------------------------------------------------------
# 3. Authorization — actor without case.transition is denied
# ---------------------------------------------------------------------------


async def test_apply_webhook_command__actor_without_case_transition__raises_forbidden(
    clock: FixedClock,
    case_type_repo: InMemoryCaseTypeRepository,
    case_type_version_repo: InMemoryCaseTypeVersionRepository,
) -> None:
    """The ``intake`` role lacks ``case.transition`` and is refused
    before any DB write is attempted."""
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
    channel = await _seed_webhook_channel(tenant_id=ACME_TENANT, channel_repo=channel_repo)

    captured_repo = InMemoryCapturedWebhookRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _cmd(
        actor_id=INTAKE_USER,
        actor_roles=("intake",),
        channel_id=channel.id,
        case_id=case.id,
    )

    with pytest.raises(Forbidden):
        await webhook_uc.apply_webhook_command(
            cmd,
            channel_repo=channel_repo,
            case_repo=case_repo,
            captured_webhook_repo=captured_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )

    assert captured_repo._rows == {}  # noqa: SLF001
    assert audit.events == []
    assert outbox.events == []


# ---------------------------------------------------------------------------
# 4. Wrong-kind / disabled channel
# ---------------------------------------------------------------------------


async def test_apply_webhook_command__non_webhook_channel__rejected(
    clock: FixedClock,
    case_type_repo: InMemoryCaseTypeRepository,
    case_type_version_repo: InMemoryCaseTypeVersionRepository,
) -> None:
    """A channel whose ``kind`` is not ``ChannelKind.WEBHOOK`` is refused."""
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
    import_channel = Channel.create(
        tenant_id=ACME_TENANT,
        key="import-main",
        kind=ChannelKind.IMPORT,
        policy=ValidationPolicy(),
        created_by=ADMIN_USER,
        now=FIXED_NOW,
    )
    await channel_repo.add(import_channel)

    cmd = _cmd(channel_id=import_channel.id, case_id=case.id)
    with pytest.raises(ValidationFailed):
        await webhook_uc.apply_webhook_command(
            cmd,
            channel_repo=channel_repo,
            case_repo=case_repo,
            captured_webhook_repo=InMemoryCapturedWebhookRepository(),
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=clock,
        )


async def test_apply_webhook_command__disabled_channel__rejected(
    clock: FixedClock,
    case_type_repo: InMemoryCaseTypeRepository,
    case_type_version_repo: InMemoryCaseTypeVersionRepository,
) -> None:
    """A webhook channel that has been disabled refuses new captures
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
    disabled_channel = await _seed_webhook_channel(
        tenant_id=ACME_TENANT, channel_repo=channel_repo, enabled=False
    )

    cmd = _cmd(channel_id=disabled_channel.id, case_id=case.id)
    with pytest.raises(ValidationFailed):
        await webhook_uc.apply_webhook_command(
            cmd,
            channel_repo=channel_repo,
            case_repo=case_repo,
            captured_webhook_repo=InMemoryCapturedWebhookRepository(),
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 5. Tenant isolation — cross-tenant webhook id lookup never leaks
# ---------------------------------------------------------------------------


async def test_apply_webhook_command__cross_tenant_webhook_id__does_not_leak(
    clock: FixedClock,
    case_type_repo: InMemoryCaseTypeRepository,
    case_type_version_repo: InMemoryCaseTypeVersionRepository,
) -> None:
    """A second tenant re-submitting the same webhook id under a
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

    # Seed a second tenant + case + channel.
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
    acme_channel = await _seed_webhook_channel(
        tenant_id=ACME_TENANT, channel_repo=channel_repo, key="webhook-acme"
    )
    other_channel = await _seed_webhook_channel(
        tenant_id=OTHER_TENANT, channel_repo=channel_repo, key="webhook-other"
    )

    captured_repo = InMemoryCapturedWebhookRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    # First tenant captures the webhook id.
    await webhook_uc.apply_webhook_command(
        _cmd(channel_id=acme_channel.id, case_id=case.id),
        channel_repo=channel_repo,
        case_repo=case_repo,
        captured_webhook_repo=captured_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # Second tenant tries the SAME webhook id on its own channel —
    # the use case must NOT short-circuit to the first tenant's row.
    second = await webhook_uc.apply_webhook_command(
        _cmd(
            tenant_id=OTHER_TENANT,
            channel_id=other_channel.id,
            case_id=other_case.id,
        ),
        channel_repo=channel_repo,
        case_repo=case_repo,
        captured_webhook_repo=captured_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert second.replayed is False
    # Two CapturedWebhook rows, one per tenant + channel.
    assert len(captured_repo._rows) == 2  # noqa: SLF001
    # Four audit events (2 tenants x 2 events each).
    assert len(audit.events) == 4
    # Two outbox events (one per tenant).
    assert len(outbox.events) == 2


# ---------------------------------------------------------------------------
# 6. Unknown case — surfaces as NotFound
# ---------------------------------------------------------------------------


async def test_apply_webhook_command__unknown_case__raises_not_found(
    clock: FixedClock,
) -> None:
    """A case id that does not exist within the actor's tenant raises
    ``NotFound`` and writes nothing."""
    channel_repo = InMemoryChannelRepository()
    channel = await _seed_webhook_channel(tenant_id=ACME_TENANT, channel_repo=channel_repo)

    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")

    cmd = _cmd(channel_id=channel.id, case_id=uuid4())
    with pytest.raises(NotFound):
        await webhook_uc.apply_webhook_command(
            cmd,
            channel_repo=channel_repo,
            case_repo=case_repo,
            captured_webhook_repo=InMemoryCapturedWebhookRepository(),
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 7. HMAC verification — invalid signature is rejected with 401
# ---------------------------------------------------------------------------


def test_compute_signature__is_deterministic_and_distinguishes_secret() -> None:
    """The HMAC scheme is deterministic for the same (body, timestamp,
    secret) triple and produces a different digest for a different
    secret — the two invariants the router relies on."""
    body = b'{"command_kind":"case.transition","case_id":"abc","payload":{}}'
    timestamp = "1700000000"
    secret = "shhh"
    expected = compute_signature(body=body, timestamp=timestamp, secret=secret)
    assert expected == compute_signature(body=body, timestamp=timestamp, secret=secret)
    # Different secret -> different signature.
    assert expected != compute_signature(body=body, timestamp=timestamp, secret="different")
    # Different body -> different signature.
    assert expected != compute_signature(body=b"tampered", timestamp=timestamp, secret=secret)
    # Different timestamp -> different signature.
    assert expected != compute_signature(body=body, timestamp="1700000001", secret=secret)
    # Signature is shaped ``sha256=<64 hex chars>``.
    assert expected.startswith("sha256=")
    assert len(expected) == len("sha256=") + 64


def _sign(body: bytes, secret: str, timestamp: str) -> str:
    return compute_signature(body=body, timestamp=timestamp, secret=secret)


async def test_router__invalid_hmac__rejected_with_401(
    monkeypatch: pytest.MonkeyPatch, clock: FixedClock
) -> None:
    """The router refuses a webhook whose ``X-Webhook-Signature`` does
    not match the channel's shared secret. The 401 is raised at the
    transport layer (before any use case is invoked) so a probing
    caller cannot enumerate channels or case ids."""
    # Import the module under test here so the rest of the suite
    # does not pay the FastAPI import cost.
    from app.interfaces.http.routers import webhooks as webhooks_router  # noqa: PLC0415

    secret = "shhh-channel-secret"
    body = json.dumps(
        {
            "command_kind": "case.transition",
            "case_id": str(uuid4()),
            "payload": {
                "transition_key": "submit_for_review",
                "target_state": "in_review",
            },
        }
    ).encode("utf-8")
    timestamp = str(int(clock.now().timestamp()))
    # Signature is over the WRONG secret.
    bad_signature = _sign(body, secret="wrong-secret", timestamp=timestamp)

    secret_provider = _StaticSecretProvider(secret=secret)
    now_epoch = int(clock.now().timestamp())
    with pytest.raises(HTTPException) as exc_info:
        await webhooks_router._verify_hmac(
            body=body,
            signature=bad_signature,
            timestamp=timestamp,
            secret=secret_provider,  # type: ignore[arg-type]
            channel_key=CHANNEL_KEY,
            now_epoch=now_epoch,
        )
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "invalid_webhook_signature"


async def test_router__missing_signature_header__rejected_with_401(
    clock: FixedClock,
) -> None:
    """A request that omits ``X-Webhook-Signature`` (or
    ``X-Webhook-Timestamp``) is refused with 401."""
    from app.interfaces.http.routers import webhooks as webhooks_router  # noqa: PLC0415

    secret_provider = _StaticSecretProvider(secret="shhh")
    with pytest.raises(HTTPException) as exc_info:
        await webhooks_router._verify_hmac(
            body=b"{}",
            signature=None,
            timestamp=None,
            secret=secret_provider,  # type: ignore[arg-type]
            channel_key=CHANNEL_KEY,
            now_epoch=int(clock.now().timestamp()),
        )
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "missing_webhook_signature"


async def test_router__stale_timestamp__rejected_with_401(
    clock: FixedClock,
) -> None:
    """A request whose ``X-Webhook-Timestamp`` is outside the replay
    window is refused with 401 so a captured signature cannot be
    replayed indefinitely."""
    from app.interfaces.http.routers import webhooks as webhooks_router  # noqa: PLC0415

    secret_provider = _StaticSecretProvider(secret="shhh")
    stale_timestamp = str(int(clock.now().timestamp()) - 10 * 60)
    with pytest.raises(HTTPException) as exc_info:
        await webhooks_router._verify_hmac(
            body=b"{}",
            signature="sha256=doesnotmatter",
            timestamp=stale_timestamp,
            secret=secret_provider,  # type: ignore[arg-type]
            channel_key=CHANNEL_KEY,
            now_epoch=int(clock.now().timestamp()),
        )
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "webhook_timestamp_out_of_window"


async def test_router__unknown_channel_secret__rejected_with_401(
    clock: FixedClock,
) -> None:
    """A request whose channel key has no configured secret is
    refused with the same ``invalid_webhook_signature`` code as a
    bad signature — the API never confirms channel existence to a
    non-authenticated caller (Constitution V)."""
    from app.interfaces.http.routers import webhooks as webhooks_router  # noqa: PLC0415

    secret_provider = _StaticSecretProvider(secret="shhh")
    with pytest.raises(HTTPException) as exc_info:
        await webhooks_router._verify_hmac(
            body=b"{}",
            signature="sha256=deadbeef",
            timestamp=str(int(clock.now().timestamp())),
            secret=secret_provider,  # type: ignore[arg-type]
            channel_key="not-a-known-channel",
            now_epoch=int(clock.now().timestamp()),
        )
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "invalid_webhook_signature"


async def test_router__valid_hmac__passes() -> None:
    """The happy path: the same secret + body + timestamp produce a
    signature that verifies. The function returns the channel key
    as the verified source."""
    from app.interfaces.http.routers import webhooks as webhooks_router  # noqa: PLC0415

    secret = "shhh-channel-secret"
    body = b'{"command_kind":"case.transition"}'
    timestamp = str(int(time.time()))
    signature = _sign(body, secret=secret, timestamp=timestamp)

    secret_provider = _StaticSecretProvider(secret=secret)
    source = await webhooks_router._verify_hmac(
        body=body,
        signature=signature,
        timestamp=timestamp,
        secret=secret_provider,  # type: ignore[arg-type]
        channel_key=CHANNEL_KEY,
        now_epoch=int(time.time()),
    )
    assert source == CHANNEL_KEY


# ---------------------------------------------------------------------------
# 8. Webhook id + payload digest invariants
# ---------------------------------------------------------------------------


def test_is_valid_webhook_id__rejects_empty_and_control_chars() -> None:
    """Empty / whitespace / overlong / non-ASCII webhook ids are
    refused at the domain boundary so a poisoned value cannot
    smuggle a second field through the unique index."""
    assert is_valid_webhook_id("evt_001")
    assert is_valid_webhook_id("a" * 998)
    assert not is_valid_webhook_id("")
    assert not is_valid_webhook_id("a" * 999)
    assert not is_valid_webhook_id("evt\t001")
    assert not is_valid_webhook_id("evt\x00001")


def test_payload_digest__is_stable_across_key_order() -> None:
    """The payload digest is the SHA-256 of the canonical JSON
    representation (sort_keys, no whitespace) so re-submitting the
    same payload with different key order produces the same digest."""
    a = payload_digest({"a": 1, "b": 2, "c": {"d": 3, "e": 4}})
    b = payload_digest({"c": {"e": 4, "d": 3}, "b": 2, "a": 1})
    assert a == b
    # Different content -> different digest.
    assert a != payload_digest({"a": 1, "b": 2, "c": {"d": 3, "e": 5}})


def test_webhook_command_kind__closed_enum_rejects_unknown_value() -> None:
    """The command vocabulary is a closed enum; an unknown value
    surfaces as a :class:`ValueError` from the enum cast so the
    router can return 422 (validation_failed) without reaching the
    use case."""
    # Valid: the three supported command kinds.
    for kind in ("case.transition", "case.comment", "case.note"):
        assert WebhookCommandKind(kind) is not None
    # Invalid: any other string.
    with pytest.raises(ValueError, match="case.delete"):
        WebhookCommandKind("case.delete")
    with pytest.raises(ValueError, match="not-a-kind"):
        WebhookCommandKind("not-a-kind")


__all__ = []
