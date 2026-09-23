"""Channel + quarantine SQL repositories (P04-US4, US5 / FM-027).

SQLAlchemy adapters that satisfy ``app.domain.ports.ChannelRepository``
and ``app.domain.ports.QuarantineRepository``. Every method takes
``tenant_id`` first and every query includes ``WHERE tenant_id =
:tenant_id`` so the API never confirms cross-tenant existence
(Constitution V).

The repositories translate the ORM rows into the domain aggregates on
read, and back on write, so the application layer never sees
SQLAlchemy types.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.channel import (
    Channel,
    ChannelKind,
    QuarantineRecord,
    QuarantineStatus,
    ValidationPolicy,
    ValidationSeverity,
)
from app.domain.errors import InvariantViolation
from app.infrastructure.persistence.models import (
    Channel as ChannelRow,
)
from app.infrastructure.persistence.models import (
    QuarantineRecord as QuarantineRow,
)


def _policy_from_dict(payload: dict[str, Any]) -> ValidationPolicy:
    return ValidationPolicy(
        required_fields=tuple(payload.get("required_fields") or ()),
        allowed_classifications=tuple(payload.get("allowed_classifications") or ()),
        max_record_bytes=payload.get("max_record_bytes"),
        missing_field_severity=ValidationSeverity(
            payload.get("missing_field_severity", "quarantine")
        ),
        unknown_field_severity=ValidationSeverity(
            payload.get("unknown_field_severity", "quarantine")
        ),
        invalid_enum_severity=ValidationSeverity(
            payload.get("invalid_enum_severity", "quarantine")
        ),
    )


def _policy_to_dict(policy: ValidationPolicy) -> dict[str, Any]:
    return {
        "required_fields": list(policy.required_fields),
        "allowed_classifications": list(policy.allowed_classifications),
        "max_record_bytes": policy.max_record_bytes,
        "missing_field_severity": policy.missing_field_severity.value,
        "unknown_field_severity": policy.unknown_field_severity.value,
        "invalid_enum_severity": policy.invalid_enum_severity.value,
    }


def _channel_from_row(row: ChannelRow) -> Channel:
    return Channel(
        id=row.id,
        tenant_id=row.tenant_id,
        key=row.key,
        kind=ChannelKind(row.kind),
        enabled=row.enabled,
        policy=_policy_from_dict(row.policy_json or {}),
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
    )


def _record_from_row(row: QuarantineRow) -> QuarantineRecord:
    return QuarantineRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        channel_id=row.channel_id,
        channel_key=row.channel_key,
        source_label=row.source_label,
        captured_at=row.captured_at,
        captured_by=row.captured_by,
        error_code=row.error_code,
        error_message=row.error_message,
        correlation_id=row.correlation_id,
        status=QuarantineStatus(row.status),
        resolved_at=row.resolved_at,
        resolved_by=row.resolved_by,
        resolution_note=row.resolution_note,
        context=dict(row.context_json or {}),
    )


class SqlChannelRepository:
    """SQLAlchemy adapter for :class:`Channel` persistence.

    Tenant isolation: every query includes ``WHERE tenant_id = :tenant_id``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, tenant_id: UUID, channel_id: UUID) -> Channel | None:
        stmt = select(ChannelRow).where(
            ChannelRow.tenant_id == tenant_id,
            ChannelRow.id == channel_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return _channel_from_row(row)

    async def get_by_key(self, tenant_id: UUID, key: str) -> Channel | None:
        stmt = select(ChannelRow).where(
            ChannelRow.tenant_id == tenant_id,
            ChannelRow.key == key,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return _channel_from_row(row)

    async def list(self, tenant_id: UUID, *, enabled_only: bool = False) -> Sequence[Channel]:
        stmt = select(ChannelRow).where(ChannelRow.tenant_id == tenant_id)
        if enabled_only:
            stmt = stmt.where(ChannelRow.enabled.is_(True))
        stmt = stmt.order_by(ChannelRow.created_at.asc())
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_channel_from_row(r) for r in rows]

    async def add(self, channel: Channel) -> None:
        row = ChannelRow(
            id=channel.id,
            tenant_id=channel.tenant_id,
            key=channel.key,
            kind=channel.kind.value,
            enabled=channel.enabled,
            policy_json=_policy_to_dict(channel.policy),
            created_by=channel.created_by,
            created_at=channel.created_at,
            updated_at=channel.updated_at,
        )
        self._session.add(row)

    async def update(self, channel: Channel) -> None:
        row = (
            await self._session.execute(
                select(ChannelRow).where(
                    ChannelRow.tenant_id == channel.tenant_id,
                    ChannelRow.id == channel.id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise InvariantViolation(f"channel {channel.id} no longer exists in tenant")
        row.enabled = channel.enabled
        row.policy_json = _policy_to_dict(channel.policy)
        row.updated_at = channel.updated_at


class SqlQuarantineRepository:
    """SQLAlchemy adapter for :class:`QuarantineRecord` persistence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, tenant_id: UUID, quarantine_id: UUID) -> QuarantineRecord | None:
        stmt = select(QuarantineRow).where(
            QuarantineRow.tenant_id == tenant_id,
            QuarantineRow.id == quarantine_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return _record_from_row(row)

    async def list(
        self,
        tenant_id: UUID,
        *,
        channel_id: UUID | None = None,
        status: QuarantineStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[QuarantineRecord]:
        stmt = select(QuarantineRow).where(QuarantineRow.tenant_id == tenant_id)
        if channel_id is not None:
            stmt = stmt.where(QuarantineRow.channel_id == channel_id)
        if status is not None:
            stmt = stmt.where(QuarantineRow.status == status.value)
        stmt = stmt.order_by(QuarantineRow.captured_at.desc()).limit(limit).offset(offset)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_record_from_row(r) for r in rows]

    async def add(self, record: QuarantineRecord) -> None:
        row = QuarantineRow(
            id=record.id,
            tenant_id=record.tenant_id,
            channel_id=record.channel_id,
            channel_key=record.channel_key,
            source_label=record.source_label,
            captured_at=record.captured_at,
            captured_by=record.captured_by,
            error_code=record.error_code,
            error_message=record.error_message,
            correlation_id=record.correlation_id,
            status=record.status.value,
            resolved_at=record.resolved_at,
            resolved_by=record.resolved_by,
            resolution_note=record.resolution_note,
            context_json=dict(record.context),
            created_at=record.captured_at,
            updated_at=record.captured_at,
        )
        self._session.add(row)

    async def update(self, record: QuarantineRecord) -> None:
        row = (
            await self._session.execute(
                select(QuarantineRow).where(
                    QuarantineRow.tenant_id == record.tenant_id,
                    QuarantineRow.id == record.id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise InvariantViolation(f"quarantine record {record.id} no longer exists in tenant")
        row.status = record.status.value
        row.resolved_at = record.resolved_at
        row.resolved_by = record.resolved_by
        row.resolution_note = record.resolution_note
        row.context_json = dict(record.context)
        row.updated_at = record.resolved_at or datetime.now(tz=UTC)


__all__ = [
    "SqlChannelRepository",
    "SqlQuarantineRepository",
]
