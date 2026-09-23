"""SQL adapter for :class:`CapturedEmail` (P04-US6 / FM-028).

Tenant isolation: every query includes ``WHERE tenant_id = :tenant_id``.
The unique constraint on ``(tenant_id, channel_id, message_id)`` is
the database-level idempotency anchor — a re-submitted Message-Id
raises ``IntegrityError`` on ``add`` and the application layer falls
back to ``get_by_message_id`` to return the prior row (the
insert-race is a feature, not a bug: the loser replays the winner).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.email_capture import CapturedEmail
from app.domain.errors import Conflict
from app.infrastructure.persistence.models import CapturedEmail as CapturedEmailRow


def _to_domain(row: CapturedEmailRow) -> CapturedEmail:
    return CapturedEmail(
        id=row.id,
        tenant_id=row.tenant_id,
        channel_id=row.channel_id,
        channel_key=row.channel_key,
        message_id=row.message_id,
        sender=row.sender,
        recipients=tuple(row.recipients_json or ()),
        subject=row.subject,
        captured_at=row.captured_at,
        captured_by=row.captured_by,
        destination=row.destination,
        artifact_id=row.artifact_id,
        artifact_kind=row.artifact_kind,
        correlation_id=row.correlation_id,
        case_id=row.case_id,
    )


class SqlCapturedEmailRepository:
    """SQL-backed ``CapturedEmailRepository``."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_by_message_id(
        self,
        tenant_id: UUID,
        channel_id: UUID,
        message_id: str,
    ) -> CapturedEmail | None:
        stmt = select(CapturedEmailRow).where(
            CapturedEmailRow.tenant_id == tenant_id,
            CapturedEmailRow.channel_id == channel_id,
            CapturedEmailRow.message_id == message_id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return _to_domain(row) if row else None

    async def add(self, record: CapturedEmail) -> None:
        row = CapturedEmailRow(
            id=record.id,
            tenant_id=record.tenant_id,
            channel_id=record.channel_id,
            channel_key=record.channel_key,
            message_id=record.message_id,
            sender=record.sender,
            recipients_json=list(record.recipients),
            subject=record.subject,
            captured_at=record.captured_at,
            captured_by=record.captured_by,
            destination=record.destination,
            artifact_id=record.artifact_id,
            artifact_kind=record.artifact_kind,
            correlation_id=record.correlation_id,
            case_id=record.case_id,
        )
        self._s.add(row)
        try:
            await self._s.flush()
        except IntegrityError as exc:
            # Re-submitted Message-Id — the unique constraint fired.
            # Translate to Conflict so the router can return the prior
            # result via a follow-up ``get_by_message_id``.
            raise Conflict("captured email already exists for this channel and Message-Id") from exc


__all__ = ["SqlCapturedEmailRepository"]
