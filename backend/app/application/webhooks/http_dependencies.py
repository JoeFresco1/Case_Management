"""Dependency contracts for the webhooks HTTP composition boundary.

The webhooks router (inbound commands + outbound subscriptions) needs
several ports bound to one request transaction. This module describes that
bundle without importing FastAPI, SQLAlchemy, or any infrastructure
adapter. The composition root supplies the concrete provider.

Why ``SecretProvider`` is NOT in the bundle: the secret adapter is
request-independent (it is not bound to the DB session), so the router
keeps resolving it via the existing ``app.deps.get_secret_provider``
dependency, which already returns only the ``app.domain.ports``
contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.ports import (
    AuditEventStore,
    CapturedWebhookRepository,
    CaseRepository,
    ChannelRepository,
    OutboundDeliveryRepository,
    OutboxWriter,
    WebhookSubscriptionRepository,
)


@dataclass(frozen=True, slots=True)
class WebhookHttpRepositories:
    """Ports required by the webhooks HTTP endpoints for one session."""

    channel: ChannelRepository
    case: CaseRepository
    captured_webhook: CapturedWebhookRepository
    subscription: WebhookSubscriptionRepository
    delivery: OutboundDeliveryRepository
    audit: AuditEventStore
    outbox: OutboxWriter


class WebhookHttpDependencyProvider(Protocol):
    """Create a request-scoped webhook port bundle from a persistence session."""

    def for_session(self, session: object) -> WebhookHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = ["WebhookHttpDependencyProvider", "WebhookHttpRepositories"]
