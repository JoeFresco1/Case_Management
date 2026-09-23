"""SQL composition provider for the webhooks HTTP boundary.

This adapter is deliberately outside ``app.interfaces``. It binds the local
SQLAlchemy repositories and transaction-bound stores to the infra-free
webhooks HTTP provider contract; an Azure adapter can implement the same
contract later.

Why no secret provider here: the secret adapter is request-independent
(not bound to the DB session), so the router keeps resolving it via the
existing ``app.deps.get_secret_provider`` dependency.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.webhooks.http_dependencies import WebhookHttpRepositories
from app.infrastructure.persistence.audit_outbox import (
    SqlAuditEventStore,
    SqlOutboxWriter,
)
from app.infrastructure.persistence.repositories import (
    SqlCapturedWebhookRepository,
    SqlCaseRepository,
    SqlChannelRepository,
    SqlOutboundDeliveryRepository,
    SqlWebhookSubscriptionRepository,
)


class SqlWebhookHttpDependencyProvider:
    """Bind all webhooks HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> WebhookHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return WebhookHttpRepositories(
            channel=SqlChannelRepository(db),
            case=SqlCaseRepository(db),
            captured_webhook=SqlCapturedWebhookRepository(db),
            subscription=SqlWebhookSubscriptionRepository(db),
            delivery=SqlOutboundDeliveryRepository(db),
            audit=SqlAuditEventStore(db),
            outbox=SqlOutboxWriter(db),
        )


__all__ = ["SqlWebhookHttpDependencyProvider"]
