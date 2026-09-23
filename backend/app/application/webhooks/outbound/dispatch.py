"""Delivery dispatch: sign, POST, record the result, retry or dead-letter.

# INTEGRATION_SEAM: outbound transport

The HTTP hop happens only through the ``OutboundDeliveryTransport`` Protocol
(``app.domain.ports``); this module never imports ``httpx``. Tests substitute
a recording transport through the constructor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from app.domain.context import Clock
from app.domain.events import find_event_spec
from app.domain.outbound_webhook import (
    DeliveryState,
    OutboundDelivery,
    SubscriptionStatus,
    delivery_due,
)
from app.domain.ports import (
    OutboundDeliveryRepository,
    OutboundDeliveryTransport,
    SecretProvider,
    TransportResult,
    WebhookSubscriptionRepository,
)

from .constants import (
    _PERMANENT_FAILURE_STATUSES,
    ATTEMPT_TIMEOUT_SECONDS,
    AUTHORIZATION_HEADER,
    CORRELATION_ID_HEADER,
    DELIVERY_ID_HEADER,
    EVENT_ID_HEADER,
    EVENT_TYPE_HEADER,
    JWT_BEARER_TTL_SECONDS,
    MAX_ATTEMPTS,
    SIGNATURE_ALG_HEADER,
    SIGNATURE_HEADER,
    SIGNATURE_ID_HEADER,
    SIGNATURE_TIMESTAMP_HEADER,
    logger,
)
from .signing import build_jwt_bearer, sign_delivery


@dataclass(frozen=True, slots=True)
class DeliveryService:
    """Drives the outbound delivery retry loop.

    The dispatcher (``OutboxDispatcher`` style) calls
    :meth:`dispatch_pending` on a tick. For each pending row:

    1. Resolve the subscription. If the subscription has been
       disabled or retired since enqueue, the row is left PENDING
       (the operator may re-enable; we do not auto-cancel).
    2. Resolve the signing secret. A missing secret is a
       configuration error; the row is dead-lettered on the first
       attempt with ``last_error = "missing_secret"`` so the
       operator can find it.
    3. Sign the body, attach the headers, POST via the transport.
    4. Record the result. On success the row is marked
       ``SUCCEEDED``; on a retriable failure the attempt counter
       is bumped; on a permanent 4xx the row is dead-lettered
       immediately.

    The service is stateless across calls; the repository owns
    the durable state.
    """

    delivery_repo: OutboundDeliveryRepository
    subscription_repo: WebhookSubscriptionRepository
    transport: OutboundDeliveryTransport
    secret_provider: SecretProvider
    clock: Clock
    max_attempts: int = MAX_ATTEMPTS
    attempt_timeout_seconds: float = ATTEMPT_TIMEOUT_SECONDS

    async def dispatch_pending(self, *, tenant_id: UUID, batch_size: int = 50) -> int:
        """Attempt up to ``batch_size`` pending deliveries; return dispatched count.

        "Dispatched" here means the row reached a terminal state
        (SUCCEEDED or DEAD_LETTERED) — pending rows that hit a
        retriable failure, rows still inside their backoff window,
        and rows skipped for an inactive subscription are not
        counted, even though a retriable failure bumps the attempt
        counter.
        """
        pending = await self.delivery_repo.list_pending(tenant_id, limit=batch_size)
        now = self.clock.now()
        dispatched = 0
        for delivery in pending:
            if not delivery_due(delivery, now):
                # why skip rather than attempt: the row failed recently
                # and its backoff window has not elapsed. POSTing now
                # would hot-spin the loop against a struggling
                # subscriber and starve healthy rows behind it in the
                # batch. The counter is untouched; the next tick
                # re-checks the gate.
                logger.debug(
                    "outbound_webhook.delivery_backoff_skip",
                    extra={
                        "delivery_id": str(delivery.id),
                        "tenant_id": str(delivery.tenant_id),
                        "attempt_count": delivery.attempt_count,
                    },
                )
                continue
            terminal = await self.attempt(delivery)
            if terminal:
                dispatched += 1
        return dispatched

    async def attempt(self, delivery: OutboundDelivery) -> bool:
        """Attempt a single delivery; return ``True`` on terminal state.

        Public so the operator console can call it directly for a
        manual replay (the replay endpoint resets the row first).
        A direct call bypasses the backoff gate — the operator
        explicitly asked for an immediate attempt. The batch path
        (:meth:`dispatch_pending`) honors the gate.

        A delivery whose subscription is no longer ``ACTIVE``
        (disabled or retired after enqueue) is skipped: no HTTP POST
        is made, the row stays ``PENDING`` with its attempt counter
        untouched, and the call returns ``False`` so the dispatcher
        does not count it as dispatched. Re-enabling the subscription
        resumes delivery without an operator replay.

        A transport that raises (timeout, connection reset, DNS
        failure from a non-conforming adapter) is caught and recorded
        as a retriable failure — the exception type name is kept for
        triage but the row, not the drain, absorbs the fault.
        """
        sub = await self.subscription_repo.get(delivery.tenant_id, delivery.subscription_id)
        if sub is None:
            # The subscription was deleted between enqueue and
            # dispatch. Dead-letter the row so the operator sees
            # it; we cannot recover a missing subscription.
            return await self._dead_letter(
                delivery,
                status_code=None,
                error="subscription_not_found",
            )
        if sub.status is not SubscriptionStatus.ACTIVE:
            # Why skip rather than dead-letter: the operator turned
            # the endpoint off, which is a reversible administrative
            # action — not a delivery failure. We must not POST to an
            # endpoint the operator disabled, and we must not burn the
            # retry budget (or hide the row in dead-letter) while the
            # subscription is off. The row stays PENDING; the next
            # dispatcher tick re-checks the status.
            logger.info(
                "outbound_webhook.delivery_skipped_inactive_subscription",
                extra={
                    "delivery_id": str(delivery.id),
                    "tenant_id": str(delivery.tenant_id),
                    "subscription_id": str(delivery.subscription_id),
                    "subscription_status": sub.status.value,
                },
            )
            return False
        try:
            secret = await self.secret_provider.get(sub.secret_name)
        except KeyError:
            return await self._dead_letter(
                delivery,
                status_code=None,
                error=f"missing_secret:{sub.secret_name}",
            )
        # Build the request. ``sign_delivery`` is pure given the
        # body + secret; ``build_jwt_bearer`` is pure given the
        # secret + claims.
        spec = find_event_spec(delivery.event_type)
        schema_version = spec.version if spec is not None else 0
        envelope = {
            "event_id": str(delivery.event_id),
            "event_type": delivery.event_type,
            "version": schema_version,
            "tenant_id": str(delivery.tenant_id),
            "payload": delivery.payload_json,
            "correlation_id": delivery.correlation_id,
            "occurred_at": delivery.occurred_at,
        }
        body = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
        timestamp = str(int(self.clock.now().timestamp()))
        signature = sign_delivery(
            body=body,
            timestamp=timestamp,
            secret=secret,
            algorithm=sub.signing_algorithm,
        )
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            SIGNATURE_TIMESTAMP_HEADER: timestamp,
            SIGNATURE_HEADER: signature,
            SIGNATURE_ALG_HEADER: sub.signing_algorithm.value,
            SIGNATURE_ID_HEADER: str(uuid4()),
            EVENT_ID_HEADER: str(delivery.event_id),
            EVENT_TYPE_HEADER: delivery.event_type,
            DELIVERY_ID_HEADER: str(delivery.id),
            CORRELATION_ID_HEADER: delivery.correlation_id or "",
        }
        if sub.require_bearer:
            headers[AUTHORIZATION_HEADER] = "Bearer " + build_jwt_bearer(
                secret=secret,
                subscription_id=sub.id,
                delivery_id=delivery.id,
                event_id=delivery.event_id,
                now=self.clock.now(),
                ttl_seconds=JWT_BEARER_TTL_SECONDS,
            )
        result = await self._post(delivery.target_url, body, headers)
        if result.success:
            await self._mark_succeeded(delivery, result.status_code)
            return True
        # Failure path. 4xx (except 408/429) is permanent; the
        # dispatcher will not retry.
        if result.status_code is not None and self._is_permanent_failure(result.status_code):
            return await self._dead_letter(delivery, result.status_code, result.error)
        return await self._record_failure(delivery, result.status_code, result.error)

    @staticmethod
    def _is_permanent_failure(status_code: int) -> bool:
        """Return ``True`` for 4xx codes we will not retry.

        408 (Request Timeout) and 429 (Too Many Requests) are
        retried because the subscriber explicitly invited a retry.
        Everything else in 4xx is a permanent failure.
        """
        if status_code in (408, 429):
            return False
        return status_code in _PERMANENT_FAILURE_STATUSES

    async def _post(self, url: str, body: bytes, headers: dict[str, str]) -> TransportResult:
        """POST via the transport; convert a raising adapter to a failure.

        The :class:`OutboundDeliveryTransport` Protocol asks adapters to
        return a result rather than raise, but a non-conforming adapter
        (or a future Azure transport surfacing SDK errors) must not
        kill the drain: the exception is recorded as a retriable
        failure with ``status_code=None`` (same as a timeout) so the
        row is retried after the backoff window. Only the exception
        type name and a truncated message are kept — never headers,
        body, or secret material.
        """
        try:
            return await self.transport.post(
                url=url,
                body=body,
                headers=headers,
                timeout_seconds=self.attempt_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — transport fault becomes a retry
            logger.warning(
                "outbound_webhook.transport_error",
                extra={"url": url, "error_type": type(exc).__name__},
            )
            return TransportResult(
                success=False,
                status_code=None,
                error=f"transport_error:{type(exc).__name__}: {str(exc)[:200]}",
            )

    async def _mark_succeeded(self, delivery: OutboundDelivery, status_code: int | None) -> None:
        new = delivery.record_attempt(
            status_code=status_code,
            error=None,
            succeeded=True,
            now=self.clock.now(),
            max_attempts=self.max_attempts,
        )
        await self.delivery_repo.update(new)

    async def _record_failure(
        self,
        delivery: OutboundDelivery,
        status_code: int | None,
        error: str | None,
    ) -> bool:
        """Bump the attempt counter; dead-letter if the cap is reached.

        The cap transition itself is owned by
        :meth:`OutboundDelivery.record_attempt` — this wrapper persists
        the result and reports whether the row reached a terminal
        state (``True`` for DEAD_LETTERED, ``False`` for PENDING).

        Returns:
            ``True`` when the row reached a terminal state.
        """
        truncated = (error or "")[:500]
        new = delivery.record_attempt(
            status_code=status_code,
            error=truncated,
            succeeded=False,
            now=self.clock.now(),
            max_attempts=self.max_attempts,
        )
        await self.delivery_repo.update(new)
        if new.state is DeliveryState.DEAD_LETTERED:
            logger.warning(
                "outbound_webhook.delivery_dead_lettered",
                extra={
                    "delivery_id": str(delivery.id),
                    "tenant_id": str(delivery.tenant_id),
                    "subscription_id": str(delivery.subscription_id),
                    "event_id": str(delivery.event_id),
                    "attempt_count": new.attempt_count,
                    "last_error": truncated,
                },
            )
            return True
        return False

    async def _dead_letter(
        self,
        delivery: OutboundDelivery,
        status_code: int | None,
        error: str | None,
    ) -> bool:
        """Move the row to DEAD_LETTERED on a permanent failure.

        Permanent means "retrying will not help": a poison 4xx, a
        missing signing secret, or a deleted subscription. The domain
        owns the transition via
        :meth:`OutboundDelivery.record_permanent_failure`; the first
        permanent failure is enough and the retry budget is untouched.
        """
        truncated = (error or "")[:500]
        new = delivery.record_permanent_failure(
            status_code=status_code,
            error=truncated,
            now=self.clock.now(),
        )
        await self.delivery_repo.update(new)
        logger.warning(
            "outbound_webhook.delivery_dead_lettered",
            extra={
                "delivery_id": str(delivery.id),
                "tenant_id": str(delivery.tenant_id),
                "subscription_id": str(delivery.subscription_id),
                "event_id": str(delivery.event_id),
                "last_error": truncated,
                "last_status_code": status_code,
            },
        )
        return True


__all__ = [
    "DeliveryService",
    "OutboundDeliveryTransport",
    "SecretProvider",
    "TransportResult",
    "delivery_due",
]
