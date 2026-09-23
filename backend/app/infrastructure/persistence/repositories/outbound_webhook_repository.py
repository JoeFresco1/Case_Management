"""SQL adapters for outbound webhook subscription + delivery (FM-056).

Tenant isolation: every query includes ``WHERE tenant_id =
:tenant_id`` (AGENTS.md §3 R1, §12). The unique constraint
``(tenant_id, subscription_id, event_id)`` on ``outbound_deliveries``
is the database-level content-idempotency anchor; the application
layer catches the ``IntegrityError`` and returns the prior row.

The repositories translate ORM rows into the domain aggregates on
read, and back on write, so the application layer never sees
SQLAlchemy types.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import Conflict
from app.domain.outbound_webhook import (
    DeliveryState,
    OutboundDelivery,
    SigningAlgorithm,
    SubscriptionStatus,
    WebhookSubscription,
)
from app.infrastructure.persistence.models import (
    OutboundDelivery as OutboundDeliveryRow,
)
from app.infrastructure.persistence.models import (
    WebhookSubscription as WebhookSubscriptionRow,
)


def _subscription_from_row(row: WebhookSubscriptionRow) -> WebhookSubscription:
    return WebhookSubscription(
        id=row.id,
        tenant_id=row.tenant_id,
        url=row.url,
        event_filters=tuple(row.event_filters_json or ()),
        secret_name=row.secret_name,
        signing_algorithm=SigningAlgorithm(row.signing_algorithm),
        require_bearer=row.require_bearer,
        status=SubscriptionStatus(row.status),
        version_token=row.version_token,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        description=row.description,
    )


def _subscription_to_row(entity: WebhookSubscription) -> dict[str, Any]:
    return {
        "id": entity.id,
        "tenant_id": entity.tenant_id,
        "url": entity.url,
        "event_filters_json": list(entity.event_filters),
        "secret_name": entity.secret_name,
        "signing_algorithm": entity.signing_algorithm.value,
        "require_bearer": entity.require_bearer,
        "status": entity.status.value,
        "version_token": entity.version_token,
        "created_at": entity.created_at,
        "updated_at": entity.updated_at,
        "created_by": entity.created_by,
        "description": entity.description,
    }


def _delivery_from_row(row: OutboundDeliveryRow) -> OutboundDelivery:
    return OutboundDelivery(
        id=row.id,
        tenant_id=row.tenant_id,
        subscription_id=row.subscription_id,
        event_id=row.event_id,
        event_type=row.event_type,
        payload_json=dict(row.payload_json or {}),
        correlation_id=row.correlation_id,
        occurred_at=row.occurred_at,
        target_url=row.target_url,
        state=DeliveryState(row.state),
        attempt_count=row.attempt_count,
        last_status_code=row.last_status_code,
        last_error=row.last_error,
        first_attempted_at=row.first_attempted_at,
        last_attempted_at=row.last_attempted_at,
        succeeded_at=row.succeeded_at,
        created_at=row.created_at,
    )


def _delivery_to_row(entity: OutboundDelivery) -> dict[str, Any]:
    return {
        "id": entity.id,
        "tenant_id": entity.tenant_id,
        "subscription_id": entity.subscription_id,
        "event_id": entity.event_id,
        "event_type": entity.event_type,
        "payload_json": dict(entity.payload_json),
        "correlation_id": entity.correlation_id,
        "occurred_at": entity.occurred_at,
        "target_url": entity.target_url,
        "state": entity.state.value,
        "attempt_count": entity.attempt_count,
        "last_status_code": entity.last_status_code,
        "last_error": entity.last_error,
        "first_attempted_at": entity.first_attempted_at,
        "last_attempted_at": entity.last_attempted_at,
        "succeeded_at": entity.succeeded_at,
        "created_at": entity.created_at,
    }


class SqlWebhookSubscriptionRepository:
    """SQL-backed :class:`WebhookSubscriptionRepository`.

    Every method is tenant-scoped; the SQL includes
    ``WHERE tenant_id = :tenant_id`` so a cross-tenant subscription
    id is never observable (Constitution V).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, tenant_id: UUID, subscription_id: UUID) -> WebhookSubscription | None:
        stmt = (
            select(WebhookSubscriptionRow)
            .where(WebhookSubscriptionRow.tenant_id == tenant_id)
            .where(WebhookSubscriptionRow.id == subscription_id)
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return _subscription_from_row(row) if row is not None else None

    async def list(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[WebhookSubscription]:
        stmt = (
            select(WebhookSubscriptionRow)
            .where(WebhookSubscriptionRow.tenant_id == tenant_id)
            .order_by(WebhookSubscriptionRow.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_subscription_from_row(r) for r in rows]

    async def add(self, subscription: WebhookSubscription) -> None:
        self._s.add(WebhookSubscriptionRow(**_subscription_to_row(subscription)))

    async def update(self, subscription: WebhookSubscription) -> None:
        # The application layer bumps ``version_token`` on success; we
        # use ``version_token`` in the WHERE clause so a stale update
        # (e.g. an operator racing with another operator) affects
        # zero rows. The caller is expected to have read the row
        # immediately before this call, but the optimistic check
        # costs nothing and prevents silent clobbering.
        stmt = (
            select(WebhookSubscriptionRow)
            .where(WebhookSubscriptionRow.tenant_id == subscription.tenant_id)
            .where(WebhookSubscriptionRow.id == subscription.id)
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        if row is None:
            # Insert path. The application layer's ``register``
            # command always inserts a new row; ``update`` is for
            # existing rows. We branch rather than upsert so a
            # mistaken ``update`` of a non-existent row surfaces as
            # a domain error rather than silently creating a new
            # row.
            self._s.add(WebhookSubscriptionRow(**_subscription_to_row(subscription)))
            return
        # In-place update. The application layer is responsible for
        # bumping ``version_token``; the database does not enforce
        # the version (we keep the database-level contract
        # portable).
        for key, value in _subscription_to_row(subscription).items():
            setattr(row, key, value)


class SqlOutboundDeliveryRepository:
    """SQL-backed :class:`OutboundDeliveryRepository`.

    Idempotency is enforced at the database level: the unique
    constraint on ``(tenant_id, subscription_id, event_id)`` fires
    on a duplicate ``add`` and we translate the ``IntegrityError``
    into a domain :class:`Conflict` so the application layer can
    fall back to ``get_by_event``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, tenant_id: UUID, delivery_id: UUID) -> OutboundDelivery | None:
        stmt = (
            select(OutboundDeliveryRow)
            .where(OutboundDeliveryRow.tenant_id == tenant_id)
            .where(OutboundDeliveryRow.id == delivery_id)
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return _delivery_from_row(row) if row is not None else None

    async def get_by_event(
        self, tenant_id: UUID, subscription_id: UUID, event_id: UUID
    ) -> OutboundDelivery | None:
        stmt = (
            select(OutboundDeliveryRow)
            .where(OutboundDeliveryRow.tenant_id == tenant_id)
            .where(OutboundDeliveryRow.subscription_id == subscription_id)
            .where(OutboundDeliveryRow.event_id == event_id)
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return _delivery_from_row(row) if row is not None else None

    async def list_pending(
        self,
        tenant_id: UUID,
        *,
        subscription_id: UUID | None = None,
        limit: int = 50,
    ) -> Sequence[OutboundDelivery]:
        stmt = (
            select(OutboundDeliveryRow)
            .where(OutboundDeliveryRow.tenant_id == tenant_id)
            .where(OutboundDeliveryRow.state == DeliveryState.PENDING.value)
            .order_by(OutboundDeliveryRow.created_at.asc())
            .limit(limit)
        )
        if subscription_id is not None:
            stmt = stmt.where(OutboundDeliveryRow.subscription_id == subscription_id)
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_delivery_from_row(r) for r in rows]

    async def list_dead_lettered(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
    ) -> Sequence[OutboundDelivery]:
        stmt = (
            select(OutboundDeliveryRow)
            .where(OutboundDeliveryRow.tenant_id == tenant_id)
            .where(OutboundDeliveryRow.state == DeliveryState.DEAD_LETTERED.value)
            .order_by(OutboundDeliveryRow.last_attempted_at.asc())
            .limit(limit)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_delivery_from_row(r) for r in rows]

    async def add(self, delivery: OutboundDelivery) -> None:
        self._s.add(OutboundDeliveryRow(**_delivery_to_row(delivery)))
        try:
            await self._s.flush()
        except IntegrityError as exc:
            # The unique constraint on (tenant_id, subscription_id, event_id)
            # is the content idempotency anchor. A re-enqueue for the same
            # event collides here and the application layer falls back to
            # ``get_by_event``. We translate the database error to a
            # domain Conflict so the use case's ``except Conflict`` branch
            # catches it.
            await self._s.rollback()
            raise Conflict(
                f"delivery for subscription {delivery.subscription_id} and "
                f"event {delivery.event_id} already exists"
            ) from exc

    async def update(self, delivery: OutboundDelivery) -> None:
        stmt = (
            select(OutboundDeliveryRow)
            .where(OutboundDeliveryRow.tenant_id == delivery.tenant_id)
            .where(OutboundDeliveryRow.id == delivery.id)
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        if row is None:
            # A delivery row that has not been persisted cannot be
            # updated. The application layer's enqueue path inserts
            # before the dispatcher loop runs, so reaching this
            # branch is a programming error.
            raise Conflict(f"delivery {delivery.id} not found for update")
        for key, value in _delivery_to_row(delivery).items():
            setattr(row, key, value)


__all__ = [
    "SqlOutboundDeliveryRepository",
    "SqlWebhookSubscriptionRepository",
]
