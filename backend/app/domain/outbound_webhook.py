"""Outbound webhook subscription + delivery aggregates (FM-056 / P13-US10).

# Why outbound is separate from inbound (FM-030 / ADR-007)

Inbound and outbound webhooks are **inverses** (specs/pillars/01-deduplicated-feature-map.md:189,
ADR-007 §"Inbound vs Outbound"):

- The platform is the **consumer** of inbound webhooks (producers push
  events in); the platform is the **producer** of outbound webhooks
  (subscribers pull events out).
- Trust direction is the inverse. Inbound verifies the producer's
  HMAC; outbound signs with the platform's secret so the subscriber can
  verify the platform's identity.
- Idempotency is the inverse. Inbound dedupes on the producer's
  ``X-Webhook-Id``; outbound dedupes on the platform's own
  ``event_id`` (which is what makes the delivery retry-safe — a
  subscriber seeing the same ``event_id`` knows it is a replay, not a
  new event).
- Failure semantics are the inverse. Inbound must **reject**
  unauthorized deliveries at the boundary; outbound must **retry**
  authorized deliveries until they succeed or exhaust the cap.

This module owns the outbound half. The inbound surface lives in
:mod:`app.domain.webhook_capture` and is wired through
:mod:`app.application.capture.webhook_commands`.

# Domain purity

This module imports nothing from FastAPI, SQLAlchemy, httpx, or any
adapter. The HTTP transport is a Protocol implemented in the
application layer (see :mod:`app.application.webhooks.outbound`).
Persistence is via ``WebhookSubscriptionRepository`` and
``OutboundDeliveryRepository`` Protocols in :mod:`app.domain.ports`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from dataclasses import replace as _dc_replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation, ValidationFailed  # noqa: I001

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class SubscriptionStatus(str, enum.Enum):  # noqa: UP042 - intentional str+Enum for JSON
    """The lifecycle of a :class:`WebhookSubscription`.

    A subscription is created ``ACTIVE``; a subscriber can disable it
    (``DISABLED``) without losing the URL / event filter; ``RETIRED`` is
    terminal and indicates the operator no longer intends to deliver to
    this endpoint (the row stays for audit).

    Why a status enum rather than a boolean: the platform can later
    add ``PAUSED`` (maintenance window) or ``QUARANTINED`` (repeated
    unauthorized responses) without a schema migration.
    """

    ACTIVE = "active"
    DISABLED = "disabled"
    RETIRED = "retired"


class SigningAlgorithm(str, enum.Enum):  # noqa: UP042 - intentional str+Enum for JSON
    """The signature algorithm the platform uses on outbound deliveries.

    ``HMAC_SHA256`` is the default; every subscriber is provisioned with
    a per-subscription secret used to compute the signature header.
    ``JWT_BEARER`` is reserved for high-trust consumers (e.g. internal
    services) that want a token they can introspect. The two can be
    combined: an outbound delivery is BOTH HMAC-signed (so the
    subscriber can verify the body) AND can carry a JWT bearer
    (``Authorization: Bearer ...``) when ``require_bearer=True``.
    """

    HMAC_SHA256 = "hmac_sha256"
    JWT_BEARER = "jwt_bearer"


class DeliveryState(str, enum.Enum):  # noqa: UP042 - intentional str+Enum for JSON
    """The state of a single outbound delivery.

    Lifecycle::

        PENDING  --(transport success)-->  SUCCEEDED
        PENDING  --(transport failure, attempts < MAX)-->  PENDING (retry)
        PENDING  --(attempts >= MAX)-->  DEAD_LETTERED
        DEAD_LETTERED --(operator replay)-->  PENDING

    The state machine is enforced by :func:`record_attempt` in this
    module. Transitions are not invoked from the application layer's
    helpers — they are driven by ``DeliveryService.attempt`` (see
    :mod:`app.application.webhooks.outbound`).
    """

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    DEAD_LETTERED = "dead_lettered"


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------


# Why a bound on URL length: the URL is stored in a portable Text/String
# column; the bound is a sanity check, not a security boundary. RFC 3986
# does not recommend an upper bound, but real platforms cap URLs to
# avoid pathologically long rows in the delivery log.
_MAX_URL_LENGTH: Final[int] = 2048

# Why a bound on event_type filter length: the event catalog is small
# (fewer than ten entries today); the bound is a sanity check that
# catches "everything" filters a subscriber might try to register by
# mistake.
_MAX_EVENT_TYPES_PER_SUBSCRIPTION: Final[int] = 32


@dataclass(frozen=True, slots=True)
class WebhookSubscription:
    """An authorized outbound webhook subscriber (FM-056 / P13-US10).

    The subscription binds a tenant-scoped HTTP endpoint to a set of
    event_type filters and a signing secret. It is the authorization
    anchor for outbound delivery: a delivery is permitted only if
    (a) the subscription is ACTIVE, (b) the event_type is in
    ``event_filters``, and (c) the actor is permitted to manage the
    subscription (operator role, not case roles).

    The signing secret is the **subscriber's** view of the
    shared secret. The platform stores it via the ``SecretProvider``
    port; the entity carries the secret name (logical key), not the
    value. This keeps secret material out of the application layer
    and out of the structured log lines (Constitution V).

    Attributes:
        id: Stable subscription id.
        tenant_id: Owning tenant; every query is scoped to this id.
        url: HTTPS URL the platform POSTs deliveries to. The platform
            refuses to deliver to non-HTTPS URLs in non-``local``
            environments (``local`` posture is allowed to use
            ``http://localhost`` for the synthetic demo).
        event_filters: Tuple of event_type strings the subscriber
            wants. The platform only delivers events whose
            ``event_type`` is in this set. An empty tuple means
            "match no events" (fail-closed default) — a subscription
            must be configured to receive something.
        secret_name: The logical key into the ``SecretProvider``
            used to sign outbound deliveries for this subscription.
        signing_algorithm: The algorithm used in the
            ``X-Webhook-Signature`` header.
        require_bearer: When ``True``, the platform attaches an
            ``Authorization: Bearer <jwt>`` header in addition to the
            HMAC signature. The JWT is signed with the subscription
            secret and contains ``sub=<subscription_id>`` and the
            ``event_id`` claim.
        status: Lifecycle status.
        version_token: Optimistic concurrency token. Bumped on
            configuration changes; ``PATCH /webhooks/subscriptions/{id}``
            requires ``If-Match`` and rejects on stale tokens.
        created_at: When the subscription was registered.
        updated_at: When the subscription was last modified.
        created_by: The user that registered the subscription.
        description: Optional human label for the operator console.
    """

    tenant_id: UUID
    url: str
    id: UUID = field(default_factory=uuid4)
    event_filters: tuple[str, ...] = ()
    secret_name: str = ""
    signing_algorithm: SigningAlgorithm = SigningAlgorithm.HMAC_SHA256
    require_bearer: bool = False
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE
    version_token: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    description: str = ""

    def __post_init__(self) -> None:
        # URL is a transport-critical value: empty, non-string, or
        # overlong URLs would surface as a 422 at the application
        # layer. Catching them at construction keeps the failure
        # message close to the bug.
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValidationFailed("subscription url is required", field="url")
        if len(self.url) > _MAX_URL_LENGTH:
            raise ValidationFailed(f"subscription url exceeds {_MAX_URL_LENGTH} chars", field="url")
        if not self.event_filters:
            # Fail-closed default: an empty filter list would
            # accidentally match every event if a subscriber registered
            # an empty filter. We require the operator to be explicit.
            raise ValidationFailed(
                "subscription must declare at least one event_filter",
                field="event_filters",
            )
        if len(self.event_filters) > _MAX_EVENT_TYPES_PER_SUBSCRIPTION:
            raise ValidationFailed(
                f"subscription event_filters exceeds {_MAX_EVENT_TYPES_PER_SUBSCRIPTION}",
                field="event_filters",
            )
        if not self.secret_name:
            raise ValidationFailed("subscription must declare a secret_name", field="secret_name")
        # version_token starts at 0 for fresh rows; the repository
        # bumps it on update. A negative value would be a bug.
        if self.version_token < 0:
            raise InvariantViolation("version_token must be non-negative")

    # ------------------------------------------------------------------
    # Behavior
    # ------------------------------------------------------------------

    def matches_event(self, event_type: str) -> bool:
        """Return ``True`` if this subscription accepts ``event_type``.

        A disabled or retired subscription matches nothing. The
        caller is responsible for filtering by tenant scope (every
        query MUST include ``WHERE tenant_id = :tenant_id``).
        """
        if self.status is not SubscriptionStatus.ACTIVE:
            return False
        return event_type in self.event_filters

    def disable(self) -> WebhookSubscription:
        """Return a copy with status set to ``DISABLED``.

        ``updated_at`` is left to the caller; the application layer
        stamps it from the clock so the domain stays pure.
        """
        # why ``dataclasses.replace`` rather than a hand-rolled copy:
        # a manual field-by-field copy silently drops any field added
        # to the dataclass later. ``replace`` carries every field
        # forward and the type-checker verifies the overrides.
        return _dc_replace(self, status=SubscriptionStatus.DISABLED)

    def retire(self) -> WebhookSubscription:
        """Return a copy with status set to ``RETIRED`` (terminal)."""
        return _dc_replace(self, status=SubscriptionStatus.RETIRED)

    def bump_version(self) -> WebhookSubscription:
        """Return a copy with ``version_token`` incremented by one.

        Called by the application layer on every configuration change
        so concurrent operators cannot clobber each other (the
        ``If-Match`` gate on PATCH enforces this).
        """
        return _dc_replace(self, version_token=self.version_token + 1)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutboundDelivery:
    """A single outbound webhook delivery attempt (FM-056 / P13-US10).

    A delivery is the durable record of "the platform tried to send
    event X to subscription Y at URL Z, and here's how that went." It
    is the retry anchor: the dispatcher reads pending rows, attempts
    the HTTP POST, and either marks the row succeeded or bumps the
    attempt counter.

    The row carries the wire-shaped event envelope (event_id, event_type,
    payload, correlation_id, occurred_at) so a replay can reconstruct
    the exact request body without re-reading the outbox. The envelope
    is immutable once written — a delivery is an idempotent record of
    "this event was/will-be delivered to this subscription."

    Idempotency anchor: the unique constraint
    ``(subscription_id, event_id)`` ensures a second enqueue for the
    same event is a no-op. The application layer catches the
    ``IntegrityError`` and returns the prior row (mirrors
    ``CapturedWebhookRepository`` in :mod:`app.domain.webhook_capture`).

    Attributes:
        id: Stable delivery id.
        tenant_id: Owning tenant; every query is scoped to this id.
        subscription_id: The :class:`WebhookSubscription` this
            delivery targets.
        event_id: The platform-issued event id (UUID). The
            subscriber dedupes on this.
        event_type: The event_type string (e.g. ``CaseCreated``).
            Carried on the row so the dispatcher can look up the
            subscription filter without joining.
        payload_json: The wire-shaped payload (already
            catalog-validated by the outbox layer; this module does
            not re-validate).
        correlation_id: The originating correlation id (or
            ``None`` for system events).
        occurred_at: When the platform recorded the event
            (UTC ISO-8601 string, format-stable for SQLite/Postgres).
        target_url: Snapshot of the subscription's URL at
            enqueue time. The URL is the only mutable field on the
            subscription that could affect delivery; we snapshot it
            so a reconfiguration between enqueue and dispatch
            cannot send the event to a different URL silently.
        state: Delivery state.
        attempt_count: How many HTTP attempts have been made
            (initial attempt is 1).
        last_status_code: HTTP status of the most recent attempt
            (or ``None`` if no attempt has been made yet, or the
            attempt failed before reaching the wire).
        last_error: Truncated error string from the most recent
            attempt (``None`` on first enqueue). Truncated to
            500 chars by the application layer.
        first_attempted_at: Timestamp of the first attempt.
        last_attempted_at: Timestamp of the most recent attempt.
        succeeded_at: Timestamp of the first successful attempt.
        created_at: When the delivery row was created.
    """

    tenant_id: UUID
    subscription_id: UUID
    event_id: UUID
    event_type: str
    id: UUID = field(default_factory=uuid4)
    # why ``Any`` rather than ``object``: the payload is a JSON-shaped
    # mapping the dispatcher serializes verbatim. ``object`` values
    # force a cast on every read; ``Any`` matches the sibling
    # aggregates (``Case.data``, ``DomainEvent.payload``).
    payload_json: dict[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None
    occurred_at: str | None = None
    target_url: str = ""
    state: DeliveryState = DeliveryState.PENDING
    attempt_count: int = 0
    last_status_code: int | None = None
    last_error: str | None = None
    first_attempted_at: datetime | None = None
    last_attempted_at: datetime | None = None
    succeeded_at: datetime | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.attempt_count < 0:
            raise InvariantViolation("attempt_count must be non-negative")
        if self.last_status_code is not None and not (100 <= self.last_status_code <= 599):
            raise InvariantViolation(f"last_status_code out of range: {self.last_status_code}")

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def record_attempt(
        self,
        *,
        status_code: int | None,
        error: str | None,
        succeeded: bool,
        now: datetime,
        max_attempts: int,
    ) -> OutboundDelivery:
        """Return a copy with the attempt recorded and state advanced.

        State machine (owned here, not by the caller):

        - ``succeeded=True`` -> state = SUCCEEDED, ``succeeded_at``
          stamped (if not already), attempt_count bumped.
        - ``succeeded=False`` and attempt_count + 1 < ``max_attempts``
          -> state stays PENDING, attempt_count bumped, ``last_error``
          recorded. The dispatcher retries after the backoff window
          (see :func:`delivery_due`).
        - ``succeeded=False`` and attempt_count + 1 >= ``max_attempts``
          -> state = DEAD_LETTERED, attempt_count bumped,
          ``last_error`` recorded.

        Poison (permanent 4xx), missing-secret, and missing-subscription
        failures dead-letter immediately via
        :meth:`record_permanent_failure` instead of consuming the retry
        budget — a 401 will never become a 200 on retry.

        The application layer supplies ``now`` from the ``Clock`` and
        ``max_attempts`` from its retry policy so the domain stays pure
        (the cap is a policy choice; the transition is a domain rule).
        """
        new_attempts = self.attempt_count + 1
        if succeeded:
            return _dc_replace(
                self,
                state=DeliveryState.SUCCEEDED,
                attempt_count=new_attempts,
                last_status_code=status_code,
                last_error=None,
                first_attempted_at=self.first_attempted_at or now,
                last_attempted_at=now,
                succeeded_at=self.succeeded_at or now,
            )
        if new_attempts >= max_attempts:
            # why dead-letter here rather than in the caller: the cap
            # is the boundary between "retry later" and "operator
            # triage". Owning the transition in the domain guarantees
            # every caller (dispatcher tick, manual replay, catalog
            # subscriber) applies the same rule — a caller that forgets
            # the check can no longer retry forever.
            return _dc_replace(
                self,
                state=DeliveryState.DEAD_LETTERED,
                attempt_count=new_attempts,
                last_status_code=status_code,
                last_error=error,
                first_attempted_at=self.first_attempted_at or now,
                last_attempted_at=now,
            )
        return _dc_replace(
            self,
            state=DeliveryState.PENDING,
            attempt_count=new_attempts,
            last_status_code=status_code,
            last_error=error,
            first_attempted_at=self.first_attempted_at or now,
            last_attempted_at=now,
        )

    def record_permanent_failure(
        self,
        *,
        status_code: int | None,
        error: str | None,
        now: datetime,
    ) -> OutboundDelivery:
        """Return a copy moved to ``DEAD_LETTERED`` on a poison failure.

        Used for failures that will never succeed on retry: permanent
        4xx responses, a missing signing secret, or a deleted
        subscription. The attempt counter is still bumped so the audit
        trail shows the delivery was attempted, but the row leaves the
        retry loop immediately instead of burning ``max_attempts``.
        """
        return _dc_replace(
            self,
            state=DeliveryState.DEAD_LETTERED,
            attempt_count=self.attempt_count + 1,
            last_status_code=status_code,
            last_error=error,
            first_attempted_at=self.first_attempted_at or now,
            last_attempted_at=now,
        )


# ---------------------------------------------------------------------------
# Retry backoff (mirrors G-2 outbox schedule)
# ---------------------------------------------------------------------------


# Retry backoff: deterministic exponential delay between attempts —
# ``min(cap, base * 2**(attempt-1))``. The schedule matches the outbox
# dispatcher (G-2) so operators reason about one retry posture:
# 5s + 10s + 20s + 40s ≈ 75s before dead-letter at the fifth failure
# (default ``MAX_ATTEMPTS = 5``). No jitter, so drain timing stays
# reproducible in tests and worst-case lag is computable.
RETRY_BASE_SECONDS: Final[float] = 5.0
RETRY_MAX_SECONDS: Final[float] = 300.0


def backoff_delay_seconds(attempt: int) -> float:
    """Return the retry delay after ``attempt`` failures (1-based).

    Args:
        attempt: The 1-based failure count (``delivery.attempt_count``
            after the failed attempt is recorded).

    Returns:
        Delay in seconds before the delivery is due again.

    Failure mode: non-positive input is clamped to the base delay —
        a zero attempt count means "no failure yet", which must never
        produce a zero or negative gate.
    """
    return min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2.0 ** max(0, attempt - 1)))


def _ensure_aware(value: datetime) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime.

    Why: SQLite returns naive datetimes for ``DateTime(timezone=True)``
    columns (the tz is not stored). Comparing a naive
    ``last_attempted_at`` with an aware ``clock.now()`` raises
    ``TypeError`` and would kill the drain. Timestamps are always
    written in UTC, so interpreting a naive read as UTC is safe.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def delivery_due(delivery: OutboundDelivery, now: datetime) -> bool:
    """Return ``True`` when ``delivery`` may be attempted at ``now``.

    A fresh delivery (no attempts yet) is always due. A failed
    delivery is due once ``last_attempted_at + backoff(attempt_count)``
    has passed. The dispatcher skips not-due rows without POSTing and
    without touching the attempt counter, so one poison endpoint
    cannot hot-spin the loop and starve healthy rows behind it in the
    batch.

    Manual replay (``DeliveryService.attempt`` called directly) bypasses
    this gate — the operator explicitly asked for an immediate attempt.
    """
    if delivery.attempt_count <= 0 or delivery.last_attempted_at is None:
        return True
    due_at = _ensure_aware(delivery.last_attempted_at) + timedelta(
        seconds=backoff_delay_seconds(delivery.attempt_count)
    )
    return _ensure_aware(now) >= due_at


__all__ = [
    "DeliveryState",
    "OutboundDelivery",
    "RETRY_BASE_SECONDS",
    "RETRY_MAX_SECONDS",
    "SigningAlgorithm",
    "SubscriptionStatus",
    "WebhookSubscription",
    "backoff_delay_seconds",
    "delivery_due",
]
