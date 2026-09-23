"""Outbound webhook subscription + delivery use cases (FM-056 / P13-US10).

# Why a package (R2 structural split)

Formerly a single ``outbound.py`` module of ~1100 lines — past the R2
1000-line hard cap. Split by responsibility into :mod:`constants`,
:mod:`audit`, :mod:`subscriptions`, :mod:`enqueue`, :mod:`dispatch`,
:mod:`signing`, and :mod:`subscriber`. This ``__init__`` is a pure
re-export shim: ``__all__`` and every previously visible attribute are
re-exported unchanged, so no consumer import line needs editing.

# Why a separate module from inbound (FM-030 / ADR-007)

Inbound and outbound webhooks are **inverses**. ADR-007 §"Inbound vs
Outbound" spells it out: trust direction, idempotency anchor, and
failure semantics are all inverted. The two surfaces share **no
code** — the inbound module (``app.application.capture.webhook_commands``)
is the consumer; this module is the producer.

# What's in this file

1. **Subscription management** — :func:`register_subscription`,
   :func:`update_subscription`, :func:`disable_subscription`,
   :func:`retire_subscription`. The subscription is the authorization
   anchor for delivery; a delivery is permitted only if the
   subscription is ACTIVE and the event_type is in the filter.
2. **Delivery enqueue** — :func:`enqueue_delivery`. Called by the
   ``MessageBus`` subscriber that mirrors domain events to outbound
   subscribers. The enqueue is idempotent on
   ``(subscription_id, event_id)``: a duplicate returns the prior
   row without inserting.
3. **Delivery dispatch** — :class:`DeliveryService.attempt` and
   :meth:`DeliveryService.dispatch_pending`. The dispatch loop signs
   the body, POSTs via the transport, records the result, and either
   marks the row succeeded or bumps the attempt counter. The cap
   (``MAX_ATTEMPTS``) is the boundary between "retry later" and
   "dead-letter for the operator."
4. **Signing** — :func:`sign_delivery` and :func:`build_jwt_bearer`.
   Both are pure functions; the secret is read through the
   ``SecretProvider`` port (AGENTS.md §12) and never lives in this
   module.

# Authorization

The subscription management endpoints are operator-scoped: only
actors with ``Action.WEBHOOK_SUBSCRIPTION_MANAGE`` may register,
update, disable, or retire a subscription. The application-level
policy is the same ``policy.evaluate`` gate every other
configuration endpoint uses (P11-US1, deny-by-default). The actor's
tenant is the authorization scope; a cross-tenant subscription id
is never observable (Constitution V).

# Idempotency

Two layers compose:

- **Content idempotency on ``(subscription_id, event_id)``** — the
  unique constraint on ``outbound_deliveries`` is the durable
  dedup ledger. A second enqueue for the same event collides and the
  application layer returns the prior row.
- **HMAC replay window on the response** — the signature header
  carries the unix-epoch timestamp; subscribers SHOULD reject
  signatures outside a 5-minute window. The platform applies the
  same window on the inbound surface (ADR-007) so subscribers can
  implement one policy.

# Failure behavior

- A transport timeout, network error, or 5xx response counts as a
  failed attempt. The row stays PENDING and is retried after the
  backoff window (``delivery_due``: 5s, 10s, 20s, 40s — the same
  schedule as the G-2 outbox dispatcher). A transport that raises
  instead of returning a result is caught and recorded the same way,
  so a hanging subscriber can never kill the drain.
- A 4xx response (other than 408/429) is a **permanent** failure
  for that delivery — the row is moved to DEAD_LETTERED on the
  first 4xx because retrying a 400/401/403/404/422 will not help.
  408 (Request Timeout) and 429 (Too Many Requests) are retried
  like 5xx.
- After ``MAX_ATTEMPTS`` failed attempts, the row is moved to
  DEAD_LETTERED. The operator console (``GET /admin/webhooks/
  deliveries?state=dead_lettered``) surfaces these for triage.
- Replay resets the row to PENDING and clears ``attempt_count``;
  the row's ``last_error`` is preserved as audit history. A manual
  replay bypasses the backoff gate — the operator explicitly asked
  for an immediate attempt.

# INTEGRATION_SEAM: outbound transport

The :class:`OutboundDeliveryTransport` Protocol in
``app.domain.ports`` is the seam. The local adapter
(``app.infrastructure.webhooks.httpx_transport.HttpXOutboundTransport``)
is a thin ``httpx.AsyncClient`` wrapper; the Azure adapter
(``app.infrastructure.webhooks.azure_event_grid_transport``) is a
placeholder. The application layer never imports ``httpx``; tests
substitute a recording transport.
"""

from __future__ import annotations

from .audit import (  # noqa: F401
    AuditEventStore,
    Clock,
    _record_audit,
)
from .constants import (  # noqa: F401
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
    Final,
    logger,
    logging,
)
from .dispatch import (  # noqa: F401
    DeliveryService,
    OutboundDeliveryTransport,
    SecretProvider,
    TransportResult,
    delivery_due,
)
from .enqueue import (  # noqa: F401
    Any,
    DeliveryState,
    EnqueueDeliveryCommand,
    OutboundDelivery,
    OutboundDeliveryRepository,
    enqueue_delivery,
)
from .signing import (  # noqa: F401
    _b64url,
    base64,
    build_jwt_bearer,
    datetime,
    hashlib,
    hmac,
    json,
    sign_delivery,
)
from .subscriber import make_catalog_subscriber  # noqa: F401
from .subscriptions import (  # noqa: F401
    UUID,
    Conflict,
    Forbidden,
    NotFound,
    RegisterSubscriptionCommand,
    SigningAlgorithm,
    SubscriptionStatus,
    UpdateSubscriptionCommand,
    UserContext,
    ValidationFailed,
    WebhookSubscription,
    WebhookSubscriptionRepository,
    dataclass,
    disable_subscription,
    find_event_spec,
    register_subscription,
    retire_subscription,
    update_subscription,
    uuid4,
)

__all__ = [
    "ATTEMPT_TIMEOUT_SECONDS",
    "CORRELATION_ID_HEADER",
    "DeliveryService",
    "EnqueueDeliveryCommand",
    "JWT_BEARER_TTL_SECONDS",
    "MAX_ATTEMPTS",
    "RegisterSubscriptionCommand",
    "SIGNATURE_ALG_HEADER",
    "SIGNATURE_HEADER",
    "SIGNATURE_ID_HEADER",
    "SIGNATURE_TIMESTAMP_HEADER",
    "UpdateSubscriptionCommand",
    "build_jwt_bearer",
    "disable_subscription",
    "enqueue_delivery",
    "make_catalog_subscriber",
    "register_subscription",
    "retire_subscription",
    "sign_delivery",
    "update_subscription",
]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, but the pre-split single-file module exposed no such
# attributes. Popping them keeps ``dir(app.application.webhooks.outbound)``
# byte-identical to the old module's attribute surface — the R2 refactor must
# be invisible. The submodules stay importable (``...outbound.dispatch``
# resolves through ``sys.modules``, which is untouched); only the redundant
# package-level attributes go away.
for _submodule in (
    "audit",
    "constants",
    "dispatch",
    "enqueue",
    "signing",
    "subscriber",
    "subscriptions",
):
    globals().pop(_submodule, None)
del _submodule
