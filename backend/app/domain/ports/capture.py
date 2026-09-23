"""CapturedEmail and CapturedWebhook ports (FM-028, FM-030 / ADR-007)."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.domain.email_capture import CapturedEmail
from app.domain.ports.core import TenantScoped
from app.domain.webhook_capture import CapturedWebhook

# ---------------------------------------------------------------------------
# CapturedEmail port -- Message-Id-keyed idempotency for inbound email (FM-028).
# ---------------------------------------------------------------------------


class CapturedEmailRepository(TenantScoped, Protocol):
    """Captured inbound email aggregate (P04-US6 / FM-028).

    The repository is the durable dedup layer for inbound email
    captures: a second submission of the same ``(tenant_id,
    channel_id, message_id)`` returns the prior ``CapturedEmail`` row
    without producing a second document. This is **content**
    idempotency, distinct from the HTTP ``Idempotency-Key`` header
    (AGENTS.md §10) -- both layers compose.

    Tenant isolation is mandatory: ``tenant_id`` is the first
    parameter on every method and ``WHERE tenant_id = :tenant_id``
    must appear in every SQL implementation (AGENTS.md §3 R1, §12).
    """

    async def get_by_message_id(
        self,
        tenant_id: UUID,
        channel_id: UUID,
        message_id: str,
    ) -> CapturedEmail | None:
        """Return the prior ``CapturedEmail`` for this tenant+channel+message_id.

        Returns ``None`` for a missing Message-Id OR for a Message-Id
        that belongs to a different tenant -- the two cases are
        intentionally indistinguishable so the API never confirms
        cross-tenant existence (Constitution V).
        """

    async def add(self, record: CapturedEmail) -> None:
        """Stage a fresh ``CapturedEmail`` row.

        Implementations MUST insert the row in the same transaction as
        the ``Document`` / ``DocumentVersion`` it produced so a partial
        commit never leaves an artifact without its provenance.
        """


# ---------------------------------------------------------------------------
# CapturedWebhook port -- X-Webhook-Id-keyed idempotency for inbound webhooks
# (FM-030 / ADR-007). Mirrors CapturedEmailRepository for inbound email;
# the durable dedup ledger is ``UNIQUE (tenant_id, channel_id, webhook_id)``.
# ---------------------------------------------------------------------------


class CapturedWebhookRepository(TenantScoped, Protocol):
    """Captured inbound webhook aggregate (P04-US8 / FM-030).

    The repository is the durable dedup layer for inbound webhook
    commands: a second submission of the same ``(tenant_id, channel_id,
    webhook_id)`` returns the prior ``CapturedWebhook`` row without
    dispatching a second case command. This is **content** idempotency,
    distinct from the HTTP ``Idempotency-Key`` header (AGENTS.md §10) --
    both layers compose. The producer's ``X-Webhook-Id`` is the
    resource-level anchor; the HTTP ``Idempotency-Key`` is the
    request-level anchor.

    Tenant isolation is mandatory: ``tenant_id`` is the first parameter
    on every method and ``WHERE tenant_id = :tenant_id`` must appear in
    every SQL implementation (AGENTS.md §3 R1, §12).
    """

    async def get_by_webhook_id(
        self,
        tenant_id: UUID,
        channel_id: UUID,
        webhook_id: str,
    ) -> CapturedWebhook | None:
        """Return the prior ``CapturedWebhook`` for this tenant+channel+webhook_id.

        Returns ``None`` for a missing webhook id OR for a webhook id
        that belongs to a different tenant -- the two cases are
        intentionally indistinguishable so the API never confirms
        cross-tenant existence (Constitution V).
        """

    async def add(self, record: CapturedWebhook) -> None:
        """Stage a fresh ``CapturedWebhook`` row.

        Implementations MUST insert the row in the same transaction as
        the audit + outbox rows the dispatch produces so a partial
        commit never leaves a command without its provenance (Constitution V).
        """


__all__ = [
    "CapturedEmailRepository",
    "CapturedWebhookRepository",
]
