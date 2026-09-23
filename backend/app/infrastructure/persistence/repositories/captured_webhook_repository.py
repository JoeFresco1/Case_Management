"""SQL adapter for :class:`CapturedWebhook` (P04-US8 / FM-030, ADR-007).

Tenant isolation: every query includes ``WHERE tenant_id = :tenant_id``.
The unique constraint on ``(tenant_id, channel_id, webhook_id)`` is
the database-level idempotency anchor — a re-submitted webhook id
raises ``IntegrityError`` on ``add`` and the application layer falls
back to ``get_by_webhook_id`` to return the prior row (the
insert-race is a feature, not a bug: the loser replays the winner).

The shape mirrors :class:`SqlCapturedEmailRepository` so the two
inbound capture surfaces share one idempotency contract: the
producer's id (``Message-Id`` / ``X-Webhook-Id``) scoped by
``(tenant_id, channel_id)``.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import Conflict
from app.domain.webhook_capture import CapturedWebhook, WebhookCommandKind
from app.infrastructure.persistence.models import CapturedWebhook as CapturedWebhookRow


def _to_domain(row: CapturedWebhookRow) -> CapturedWebhook:
    """Translate an ORM row into the :class:`CapturedWebhook` aggregate.

    Args:
        row: The persisted ``captured_webhooks`` row.

    Returns:
        The domain aggregate with ``payload`` copied so callers cannot
        mutate the ORM state through the returned dict.
    """
    return CapturedWebhook(
        id=row.id,
        tenant_id=row.tenant_id,
        channel_id=row.channel_id,
        channel_key=row.channel_key,
        webhook_id=row.webhook_id,
        source_system=row.source_system,
        command_kind=WebhookCommandKind(row.command_kind),
        case_id=row.case_id,
        payload=dict(row.payload_json or {}),
        payload_digest=row.payload_digest,
        captured_at=row.captured_at,
        captured_by=row.captured_by,
        correlation_id=row.correlation_id,
    )


class SqlCapturedWebhookRepository:
    """SQL-backed ``CapturedWebhookRepository``.

    Tenant-scoped: every method takes ``tenant_id`` first; every query
    includes ``WHERE tenant_id = :tenant_id`` so a cross-tenant
    ``X-Webhook-Id`` is never observable (Constitution V).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_by_webhook_id(
        self,
        tenant_id: UUID,
        channel_id: UUID,
        webhook_id: str,
    ) -> CapturedWebhook | None:
        """Return the prior capture for ``(tenant_id, channel_id, webhook_id)``.

        Args:
            tenant_id: Owning tenant; the query filters on it first.
            channel_id: Channel the webhook arrived on.
            webhook_id: Producer ``X-Webhook-Id`` idempotency anchor.

        Returns:
            The prior aggregate, or None when missing or owned by a
            different tenant (indistinguishable by design).
        """
        stmt = select(CapturedWebhookRow).where(
            CapturedWebhookRow.tenant_id == tenant_id,
            CapturedWebhookRow.channel_id == channel_id,
            CapturedWebhookRow.webhook_id == webhook_id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return _to_domain(row) if row else None

    async def add(self, record: CapturedWebhook) -> None:
        """Stage a fresh ``CapturedWebhook`` row on the request transaction.

        Args:
            record: The aggregate built by ``CapturedWebhook.create``;
                ``payload_digest`` is already computed there.

        Raises:
            Conflict: When ``(tenant_id, channel_id, webhook_id)``
                already exists — the caller replays via
                :meth:`get_by_webhook_id`.
        """
        row = CapturedWebhookRow(
            id=record.id,
            tenant_id=record.tenant_id,
            channel_id=record.channel_id,
            channel_key=record.channel_key,
            webhook_id=record.webhook_id,
            source_system=record.source_system,
            command_kind=record.command_kind.value,
            case_id=record.case_id,
            payload_json=dict(record.payload),
            payload_digest=record.payload_digest,
            captured_at=record.captured_at,
            captured_by=record.captured_by,
            correlation_id=record.correlation_id,
        )
        self._s.add(row)
        try:
            await self._s.flush()
        except IntegrityError as exc:
            # Re-submitted X-Webhook-Id — the unique constraint fired.
            # Translate to Conflict so the use case can return the prior
            # result via a follow-up ``get_by_webhook_id``.
            raise Conflict(
                "captured webhook already exists for this channel and webhook id"
            ) from exc


__all__ = ["SqlCapturedWebhookRepository"]
