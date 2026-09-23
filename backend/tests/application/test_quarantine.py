"""Tests for quarantine behavior (P04-US5 / FM-027).

Acceptance scenarios from ``specs/pillars/P04-capture-ingestion/pillar.md``
D4 and the FM-027 acceptance narrative:

1. **Malformed row is isolated, good rows continue** — a channel with
   a strict policy quarantines a bad record while a sibling good row
   is accepted.
2. **Inspect lists held records** — the operator console sees the
   quarantined row with diagnostic ``context``.
3. **Release moves to commit path** — releasing a corrected record
   transitions the row to ``RELEASED`` and appends an audit + outbox
   event.
4. **Reject drops** — rejecting a row (e.g. a duplicate) transitions
   it to ``REJECTED`` and emits its own audit + outbox event; a
   subsequent release attempt is rejected because the record is
   terminal.
5. **Authorization** — non-admin actors cannot inspect / resolve
   quarantine records; cross-tenant reads return ``NotFound``.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5. Tests are deterministic: fixed clock, fixed UUIDs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.application.channels import manage_channels
from app.domain.channel import (
    Channel,
    ChannelKind,
    QuarantineRecord,
    QuarantineStatus,
    ValidationPolicy,
    ValidationSeverity,
)
from app.domain.errors import Forbidden, NotFound, ValidationFailed
from app.domain.provenance import is_valid_payload_hash

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
ADMIN_USER = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
INTAKE_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

FIXED_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


@dataclass
class StagedChannel:
    channel: Channel


class InMemoryChannelRepository:
    """In-memory ``ChannelRepository``."""

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


@dataclass
class StagedQuarantine:
    record: QuarantineRecord


class InMemoryQuarantineRepository:
    """In-memory ``QuarantineRepository``.

    Preserves insertion order on ``list`` so the operator console
    reads in capture-descending order (the SQL adapter uses
    ``ORDER BY captured_at DESC``).
    """

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


@dataclass
class StagedAudit:
    tenant_id: UUID
    event_type: str
    actor_id: UUID
    subject_type: str
    subject_id: UUID
    payload: dict[str, Any]
    correlation_id: str


class InMemoryAuditStore:
    def __init__(self) -> None:
        self.events: list[StagedAudit] = []

    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID,
        actor_type: str,
        subject_type: str,
        subject_id: UUID,
        case_id: UUID | None,
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        self.events.append(
            StagedAudit(
                tenant_id=tenant_id,
                event_type=event_type,
                actor_id=actor_id,
                subject_type=subject_type,
                subject_id=subject_id,
                payload=payload,
                correlation_id=correlation_id,
            )
        )


@dataclass
class StagedOutbox:
    tenant_id: UUID
    event_type: str
    event_id: UUID
    payload: dict[str, Any]


class InMemoryOutbox:
    def __init__(self) -> None:
        self.events: list[StagedOutbox] = []

    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        event_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        self.events.append(
            StagedOutbox(
                tenant_id=tenant_id,
                event_type=event_type,
                event_id=event_id,
                payload=payload,
            )
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


@pytest.fixture
def audit() -> InMemoryAuditStore:
    return InMemoryAuditStore()


@pytest.fixture
def outbox() -> InMemoryOutbox:
    return InMemoryOutbox()


@pytest.fixture
def channel_repo() -> InMemoryChannelRepository:
    return InMemoryChannelRepository()


@pytest.fixture
def quarantine_repo() -> InMemoryQuarantineRepository:
    return InMemoryQuarantineRepository()


def _strict_policy() -> ValidationPolicy:
    return ValidationPolicy(
        required_fields=("title", "summary"),
        allowed_classifications=(),
        max_record_bytes=None,
        missing_field_severity=ValidationSeverity.QUARANTINE,
    )


def _loose_policy() -> ValidationPolicy:
    return ValidationPolicy()  # no required fields → accepts everything


async def _seed_channel(
    channel_repo: InMemoryChannelRepository,
    *,
    tenant_id: UUID = ACME_TENANT,
    actor_id: UUID = ADMIN_USER,
    policy: ValidationPolicy | None = None,
    clock: FixedClock,
    enabled: bool = True,
    key: str = "import-main",
) -> Channel:
    channel = Channel.create(
        tenant_id=tenant_id,
        key=key,
        kind=ChannelKind.IMPORT,
        policy=policy or _strict_policy(),
        created_by=actor_id,
        now=clock.now(),
        enabled=enabled,
    )
    await channel_repo.add(channel)
    return channel


def _enable_cmd(
    *,
    key: str = "import-main",
    policy: ValidationPolicy | None = None,
    enabled: bool = True,
) -> manage_channels.EnableChannelCommand:
    return manage_channels.EnableChannelCommand(
        tenant_id=ACME_TENANT,
        actor_id=ADMIN_USER,
        actor_roles=("process_admin",),
        key=key,
        kind=ChannelKind.IMPORT,
        policy=policy or _strict_policy(),
        enabled=enabled,
        correlation_id="corr-channels-001",
    )


# ---------------------------------------------------------------------------
# 1. Strict policy quarantines bad record without blocking good ones
# ---------------------------------------------------------------------------


async def test_quarantine_capture__strict_policy_bad_row_quarantined_good_row_accepted(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
    channel_repo: InMemoryChannelRepository,
    quarantine_repo: InMemoryQuarantineRepository,
) -> None:
    """A row missing the required ``title`` field is quarantined while a
    sibling row that satisfies the policy is accepted."""
    channel = await _seed_channel(
        channel_repo,
        clock=clock,
    )
    # Good row.
    good = await manage_channels.quarantine_capture(
        manage_channels.QuarantineCaptureCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            channel_id=channel.id,
            source_label="synthetic-row-good",
            values={"title": "Hello", "summary": "World"},
            correlation_id="corr-quarantine-001",
        ),
        channel_repo=channel_repo,
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert good.decision.accepted is True
    assert good.record is None
    # Bad row.
    bad = await manage_channels.quarantine_capture(
        manage_channels.QuarantineCaptureCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            channel_id=channel.id,
            source_label="synthetic-row-bad",
            values={"summary": "Missing title"},
            correlation_id="corr-quarantine-002",
        ),
        channel_repo=channel_repo,
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert bad.decision.accepted is False
    assert bad.record is not None
    assert bad.record.status is QuarantineStatus.HELD
    assert bad.record.error_code == "missing_required_field"
    assert "title" in bad.record.error_message
    assert bad.record.context.get("missing") == ["title"]

    # The quarantine repo holds only the bad row.
    assert len(quarantine_repo._rows) == 1

    # Audit + outbox records the quarantine event only.
    quarantine_events = [e for e in audit.events if e.event_type == "CaptureQuarantined"]
    assert len(quarantine_events) == 1
    outbox_events = [e for e in outbox.events if e.event_type == "CaptureQuarantined"]
    assert len(outbox_events) == 1
    # And no rejection events were emitted for the good row.
    rejected_events = [e for e in audit.events if e.event_type == "CaptureRejected"]
    assert rejected_events == []


# ---------------------------------------------------------------------------
# 2. Inspect lists held records
# ---------------------------------------------------------------------------


async def test_inspect_quarantine__lists_held_records_for_operator(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
    channel_repo: InMemoryChannelRepository,
    quarantine_repo: InMemoryQuarantineRepository,
) -> None:
    """``inspect_quarantine`` returns the held rows in capture-descending
    order with their diagnostic ``context``."""
    channel = await _seed_channel(channel_repo, clock=clock)
    # Quarantine two rows.
    for i, label in enumerate(("synthetic-row-a", "synthetic-row-b"), start=1):
        await manage_channels.quarantine_capture(
            manage_channels.QuarantineCaptureCommand(
                tenant_id=ACME_TENANT,
                actor_id=ADMIN_USER,
                actor_roles=("process_admin",),
                channel_id=channel.id,
                source_label=label,
                values={"summary": f"row {i}"},  # title intentionally absent
                correlation_id=f"corr-inspect-{i}",
            ),
            channel_repo=channel_repo,
            quarantine_repo=quarantine_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )

    result = await manage_channels.inspect_quarantine(
        manage_channels.InspectQuarantineQuery(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            channel_id=channel.id,
            status=QuarantineStatus.HELD,
        ),
        quarantine_repo=quarantine_repo,
    )
    assert result.total == 2
    labels = {item.source_label for item in result.items}
    assert labels == {"synthetic-row-a", "synthetic-row-b"}
    for item in result.items:
        assert item.status is QuarantineStatus.HELD
        assert item.error_code == "missing_required_field"
        assert "title" in item.context.get("missing", [])


# ---------------------------------------------------------------------------
# 3. Release moves to commit path
# ---------------------------------------------------------------------------


async def test_release_quarantine__transitions_to_released_with_audit(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
    channel_repo: InMemoryChannelRepository,
    quarantine_repo: InMemoryQuarantineRepository,
) -> None:
    """Releasing a corrected record flips status to ``RELEASED`` and
    appends the audit + outbox events."""
    channel = await _seed_channel(channel_repo, clock=clock)
    capture = await manage_channels.quarantine_capture(
        manage_channels.QuarantineCaptureCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            channel_id=channel.id,
            source_label="synthetic-correction",
            values={"summary": "Missing title"},
            correlation_id="corr-release-001",
        ),
        channel_repo=channel_repo,
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert capture.record is not None
    record_id = capture.record.id

    # Operator releases the corrected record.
    result = await manage_channels.release_quarantine(
        manage_channels.ReleaseQuarantineCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            quarantine_id=record_id,
            note="title added in correction",
            correlation_id="corr-release-002",
        ),
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert result.record.status is QuarantineStatus.RELEASED
    assert result.record.resolved_by == ADMIN_USER
    assert result.record.resolution_note == "title added in correction"

    # Audit + outbox trail.
    release_audit = [e for e in audit.events if e.event_type == "QuarantineReleased"]
    assert len(release_audit) == 1
    release_outbox = [e for e in outbox.events if e.event_type == "QuarantineReleased"]
    assert len(release_outbox) == 1

    # Second release is a no-op (idempotent).
    again = await manage_channels.release_quarantine(
        manage_channels.ReleaseQuarantineCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            quarantine_id=record_id,
            correlation_id="corr-release-003",
        ),
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert again.record.status is QuarantineStatus.RELEASED
    assert len([e for e in audit.events if e.event_type == "QuarantineReleased"]) == 1


# ---------------------------------------------------------------------------
# 4. Reject drops; releasing a rejected record is refused
# ---------------------------------------------------------------------------


async def test_reject_quarantine__transitions_to_rejected_then_release_fails(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
    channel_repo: InMemoryChannelRepository,
    quarantine_repo: InMemoryQuarantineRepository,
) -> None:
    """A duplicate row is rejected; the terminal status refuses release."""
    channel = await _seed_channel(channel_repo, clock=clock)
    capture = await manage_channels.quarantine_capture(
        manage_channels.QuarantineCaptureCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            channel_id=channel.id,
            source_label="synthetic-duplicate",
            values={"summary": "Duplicate"},
            correlation_id="corr-reject-001",
        ),
        channel_repo=channel_repo,
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert capture.record is not None
    record_id = capture.record.id

    result = await manage_channels.reject_quarantine(
        manage_channels.RejectQuarantineCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            quarantine_id=record_id,
            note="duplicate of row 1",
            correlation_id="corr-reject-002",
        ),
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert result.record.status is QuarantineStatus.REJECTED
    assert result.record.resolution_note == "duplicate of row 1"

    rejected_audit = [e for e in audit.events if e.event_type == "QuarantineRejected"]
    assert len(rejected_audit) == 1

    # Cannot release a rejected record.
    with pytest.raises(ValidationFailed):
        await manage_channels.release_quarantine(
            manage_channels.ReleaseQuarantineCommand(
                tenant_id=ACME_TENANT,
                actor_id=ADMIN_USER,
                actor_roles=("process_admin",),
                quarantine_id=record_id,
                correlation_id="corr-reject-003",
            ),
            quarantine_repo=quarantine_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 5. Authorization
# ---------------------------------------------------------------------------


async def test_inspect_quarantine__intake_role__raises_forbidden(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
    channel_repo: InMemoryChannelRepository,
    quarantine_repo: InMemoryQuarantineRepository,
) -> None:
    """The intake role lacks ``ADMIN_TENANT_CONFIG`` and is denied."""
    with pytest.raises(Forbidden):
        await manage_channels.inspect_quarantine(
            manage_channels.InspectQuarantineQuery(
                tenant_id=ACME_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=("intake",),
            ),
            quarantine_repo=quarantine_repo,
        )


async def test_release_quarantine__cross_tenant_actor__raises_not_found(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
    channel_repo: InMemoryChannelRepository,
    quarantine_repo: InMemoryQuarantineRepository,
) -> None:
    """A cross-tenant actor passes the policy check (admin role) but the
    repository's tenant filter denies them with ``NotFound``."""
    channel = await _seed_channel(channel_repo, clock=clock)
    capture = await manage_channels.quarantine_capture(
        manage_channels.QuarantineCaptureCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            channel_id=channel.id,
            source_label="synthetic-row",
            values={"summary": "Missing title"},
            correlation_id="corr-cross-001",
        ),
        channel_repo=channel_repo,
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert capture.record is not None
    with pytest.raises(NotFound):
        await manage_channels.release_quarantine(
            manage_channels.ReleaseQuarantineCommand(
                tenant_id=OTHER_TENANT,
                actor_id=ADMIN_USER,
                actor_roles=("process_admin",),
                quarantine_id=capture.record.id,
                correlation_id="corr-cross-002",
            ),
            quarantine_repo=quarantine_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 6. Disabled channel refuses new captures (fail-closed)
# ---------------------------------------------------------------------------


async def test_quarantine_capture__disabled_channel__rejects_with_audit(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
    channel_repo: InMemoryChannelRepository,
    quarantine_repo: InMemoryQuarantineRepository,
) -> None:
    """A disabled channel produces a ``channel_disabled`` reject with no
    quarantine row (the operator has already taken it offline)."""
    channel = await _seed_channel(channel_repo, clock=clock, enabled=False)
    result = await manage_channels.quarantine_capture(
        manage_channels.QuarantineCaptureCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            channel_id=channel.id,
            source_label="synthetic-row",
            values={"title": "Hello", "summary": "World"},
            correlation_id="corr-disabled-001",
        ),
        channel_repo=channel_repo,
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert result.decision.accepted is False
    assert result.decision.error_code == "channel_disabled"
    assert result.record is None
    rejected_events = [e for e in audit.events if e.event_type == "CaptureRejected"]
    assert len(rejected_events) == 1
    assert quarantine_repo._rows == {}


# ---------------------------------------------------------------------------
# 7. Enable a channel end-to-end (admin-only audit + outbox)
# ---------------------------------------------------------------------------


async def test_enable_channel__creates_row_with_strict_policy_and_audit(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
    channel_repo: InMemoryChannelRepository,
) -> None:
    """Enabling a new channel writes the row, the audit event, and the
    outbox event."""
    result = await manage_channels.enable_channel(
        _enable_cmd(),
        channel_repo=channel_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert result.created is True
    assert result.channel.enabled is True
    assert result.channel.key == "import-main"
    assert result.channel.policy.required_fields == ("title", "summary")

    enabled_audit = [e for e in audit.events if e.event_type == "ChannelEnabled"]
    assert len(enabled_audit) == 1
    enabled_outbox = [e for e in outbox.events if e.event_type == "ChannelEnabled"]
    assert len(enabled_outbox) == 1


async def test_enable_channel__intake_role__raises_forbidden(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
    channel_repo: InMemoryChannelRepository,
) -> None:
    with pytest.raises(Forbidden):
        await manage_channels.enable_channel(
            manage_channels.EnableChannelCommand(
                tenant_id=ACME_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=("intake",),
                key="import-main",
                kind=ChannelKind.IMPORT,
                policy=_strict_policy(),
            ),
            channel_repo=channel_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 8. Poison isolation carries payload hash without value leakage (T1)
# ---------------------------------------------------------------------------


async def test_quarantine_capture__poison_row__isolates_with_hash_and_no_leakage(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
    channel_repo: InMemoryChannelRepository,
    quarantine_repo: InMemoryQuarantineRepository,
) -> None:
    """A poison row isolates with ``payload_hash`` + ``source_system`` and
    never embeds the row's values in record, audit, or outbox."""
    # // given a strict channel.
    channel = await _seed_channel(channel_repo, clock=clock)
    # // when a poison row arrives.
    bad = await manage_channels.quarantine_capture(
        manage_channels.QuarantineCaptureCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            channel_id=channel.id,
            source_label="synthetic-poison-row",
            values={"summary": "s3cr3t-value-must-not-leak"},
            correlation_id="corr-poison-001",
        ),
        channel_repo=channel_repo,
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    # // then the row is held with a valid hash and safe reason.
    assert bad.record is not None
    assert bad.record.error_code == "missing_required_field"
    assert bad.record.context.get("missing") == ["title"]
    payload_hash = bad.record.context.get("payload_hash", "")
    assert isinstance(payload_hash, str)
    assert is_valid_payload_hash(payload_hash)
    assert bad.record.context.get("source_system") == channel.kind.value
    # // and values never leak into record, audit, or outbox.
    blob = (
        str(bad.record.context)
        + str([e.payload for e in audit.events])
        + str([e.payload for e in outbox.events])
    )
    assert "s3cr3t-value-must-not-leak" not in blob
    assert "payload_hash" in bad.record.context
    # // and sibling good row still accepted.
    good = await manage_channels.quarantine_capture(
        manage_channels.QuarantineCaptureCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            channel_id=channel.id,
            source_label="synthetic-good-row",
            values={"title": "Hello", "summary": "World"},
            correlation_id="corr-poison-002",
        ),
        channel_repo=channel_repo,
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert good.decision.accepted is True
    assert good.record is None


async def test_quarantine_capture__reject_severity__drops_with_hash_audit_only(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
    channel_repo: InMemoryChannelRepository,
    quarantine_repo: InMemoryQuarantineRepository,
) -> None:
    """A ``REJECT``-severity failure drops without a holding-area row but
    still audits the safe reason + payload hash."""
    # // given a channel whose missing-field policy rejects.
    reject_policy = ValidationPolicy(
        required_fields=("title",),
        missing_field_severity=ValidationSeverity.REJECT,
    )
    channel = await _seed_channel(channel_repo, clock=clock, policy=reject_policy)
    # // when a poison row arrives.
    result = await manage_channels.quarantine_capture(
        manage_channels.QuarantineCaptureCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            channel_id=channel.id,
            source_label="synthetic-spam-row",
            values={"summary": "spam without title"},
            correlation_id="corr-reject-sev-001",
        ),
        channel_repo=channel_repo,
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    # // then no holding-area row exists.
    assert result.decision.accepted is False
    assert result.record is None
    assert quarantine_repo._rows == {}
    # // but audit + outbox carry the hash for reconciliation.
    rejected = [e for e in audit.events if e.event_type == "CaptureRejected"]
    assert len(rejected) == 1
    assert is_valid_payload_hash(str(rejected[0].payload.get("payload_hash", "")))
    assert rejected[0].payload.get("source_system") == channel.kind.value
    assert len([e for e in outbox.events if e.event_type == "CaptureRejected"]) == 1


async def test_inspect_quarantine__filters_by_channel_and_status(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
    channel_repo: InMemoryChannelRepository,
    quarantine_repo: InMemoryQuarantineRepository,
) -> None:
    """The holding area is queryable by ``channel_id`` + ``status``."""
    # // given two channels each holding one row.
    channel_a = await _seed_channel(channel_repo, clock=clock, key="import-a")
    channel_b = await _seed_channel(channel_repo, clock=clock, key="import-b")
    for channel, label in ((channel_a, "row-a"), (channel_b, "row-b")):
        await manage_channels.quarantine_capture(
            manage_channels.QuarantineCaptureCommand(
                tenant_id=ACME_TENANT,
                actor_id=ADMIN_USER,
                actor_roles=("process_admin",),
                channel_id=channel.id,
                source_label=label,
                values={"summary": "missing title"},
                correlation_id=f"corr-filter-{label}",
            ),
            channel_repo=channel_repo,
            quarantine_repo=quarantine_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
    # // when filtering by channel + held status.
    result = await manage_channels.inspect_quarantine(
        manage_channels.InspectQuarantineQuery(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            channel_id=channel_a.id,
            status=QuarantineStatus.HELD,
        ),
        quarantine_repo=quarantine_repo,
    )
    # // then only that channel's held row returns.
    assert result.total == 1
    assert result.items[0].channel_id == channel_a.id
    assert result.items[0].source_label == "row-a"
    # // and an unfiltered read sees both.
    all_rows = await manage_channels.inspect_quarantine(
        manage_channels.InspectQuarantineQuery(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
        ),
        quarantine_repo=quarantine_repo,
    )
    assert all_rows.total == 2


async def test_release_quarantine__records_actor_reason_and_hash_passthrough(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
    channel_repo: InMemoryChannelRepository,
    quarantine_repo: InMemoryQuarantineRepository,
) -> None:
    """Release audits ``actor_id`` + ``resolved_by`` + ``note`` + hash."""
    # // given a held poison row.
    channel = await _seed_channel(channel_repo, clock=clock)
    capture = await manage_channels.quarantine_capture(
        manage_channels.QuarantineCaptureCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            channel_id=channel.id,
            source_label="synthetic-release-hash",
            values={"summary": "missing title"},
            correlation_id="corr-rel-hash-001",
        ),
        channel_repo=channel_repo,
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert capture.record is not None
    held_hash = capture.record.context.get("payload_hash", "")
    assert is_valid_payload_hash(str(held_hash))
    # // when the operator releases with a reason.
    result = await manage_channels.release_quarantine(
        manage_channels.ReleaseQuarantineCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            quarantine_id=capture.record.id,
            note="title corrected by operator",
            correlation_id="corr-rel-hash-002",
        ),
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    # // then the record pins actor + reason.
    assert result.record.resolved_by == ADMIN_USER
    assert result.record.resolution_note == "title corrected by operator"
    # // and exactly one audit + outbox row carries actor + hash.
    released_audit = [e for e in audit.events if e.event_type == "QuarantineReleased"]
    assert len(released_audit) == 1
    assert released_audit[0].payload.get("actor_id") == str(ADMIN_USER)
    assert released_audit[0].payload.get("resolved_by") == str(ADMIN_USER)
    assert released_audit[0].payload.get("resolution_note") == "title corrected by operator"
    assert released_audit[0].payload.get("payload_hash") == held_hash
    assert released_audit[0].payload.get("channel_key") == channel.key
    assert len([e for e in outbox.events if e.event_type == "QuarantineReleased"]) == 1


async def test_reject_quarantine__records_actor_reason_and_idempotent(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
    channel_repo: InMemoryChannelRepository,
    quarantine_repo: InMemoryQuarantineRepository,
) -> None:
    """Reject audits actor + reason + hash; a re-reject emits no second event."""
    # // given a held row.
    channel = await _seed_channel(channel_repo, clock=clock)
    capture = await manage_channels.quarantine_capture(
        manage_channels.QuarantineCaptureCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            channel_id=channel.id,
            source_label="synthetic-reject-hash",
            values={"summary": "missing title"},
            correlation_id="corr-rej-hash-001",
        ),
        channel_repo=channel_repo,
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert capture.record is not None
    held_hash = capture.record.context.get("payload_hash", "")
    # // when rejected with a reason.
    result = await manage_channels.reject_quarantine(
        manage_channels.RejectQuarantineCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            quarantine_id=capture.record.id,
            note="duplicate of row 1",
            correlation_id="corr-rej-hash-002",
        ),
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert result.record.resolved_by == ADMIN_USER
    assert result.record.resolution_note == "duplicate of row 1"
    rejected_audit = [e for e in audit.events if e.event_type == "QuarantineRejected"]
    assert len(rejected_audit) == 1
    assert rejected_audit[0].payload.get("resolved_by") == str(ADMIN_USER)
    assert rejected_audit[0].payload.get("payload_hash") == held_hash
    # // and a re-reject is idempotent with no second event.
    again = await manage_channels.reject_quarantine(
        manage_channels.RejectQuarantineCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            quarantine_id=capture.record.id,
            correlation_id="corr-rej-hash-003",
        ),
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert again.record.status is QuarantineStatus.REJECTED
    assert len([e for e in audit.events if e.event_type == "QuarantineRejected"]) == 1
    assert len([e for e in outbox.events if e.event_type == "QuarantineRejected"]) == 1


async def test_release_quarantine__intake_role__raises_forbidden(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
    channel_repo: InMemoryChannelRepository,
    quarantine_repo: InMemoryQuarantineRepository,
) -> None:
    """Non-admin actors cannot resolve quarantine records."""
    # // given a held row.
    channel = await _seed_channel(channel_repo, clock=clock)
    capture = await manage_channels.quarantine_capture(
        manage_channels.QuarantineCaptureCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            channel_id=channel.id,
            source_label="synthetic-row",
            values={"summary": "missing title"},
            correlation_id="corr-forbid-001",
        ),
        channel_repo=channel_repo,
        quarantine_repo=quarantine_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert capture.record is not None
    # // when the intake role attempts release // then Forbidden.
    with pytest.raises(Forbidden):
        await manage_channels.release_quarantine(
            manage_channels.ReleaseQuarantineCommand(
                tenant_id=ACME_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=("intake",),
                quarantine_id=capture.record.id,
                correlation_id="corr-forbid-002",
            ),
            quarantine_repo=quarantine_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


__all__ = []
