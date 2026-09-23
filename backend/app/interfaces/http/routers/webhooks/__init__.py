"""Webhook router package — inbound receiver + outbound subscriptions.

Feature scope: inbound FM-030 / outbound FM-056 (ADR-007 / ADR-013).

Endpoints:

- ``POST /api/v1/webhooks/{channel}`` — receive one HMAC-signed
  inbound webhook event for the named channel and turn it into a
  case command / state change with provenance. Duplicate webhook
  ids are idempotent; the prior result is replayed.
- ``GET/POST/PATCH /api/v1/webhooks/subscriptions[...]`` plus the
  ``disable`` / ``retire`` / ``replay`` lifecycle actions — the
  outbound half (FM-056); see :mod:`subscriptions`.

# Authorization

The router has TWO authorization gates on the inbound surface:

1. **Transport-level** (the :mod:`security` module): HMAC
   verification using the channel's shared secret. The signature is
   computed over the raw request body and the ``X-Webhook-Timestamp``
   header; the secret is resolved through the ``SecretProvider`` port
   (``AGENTS.md §12``). A failed HMAC returns ``401`` so the
   platform never confirms the channel's existence to a
   non-authenticated caller (Constitution V).

2. **Application-level** (the use case): the same ``case.transition``
   gate that guards an in-app transition guards a webhook-driven
   transition. The actor's identity is the *caller* (a human user in
   ``local`` posture) — a webhook is not system authority.

Outbound subscription management is fail-closed behind
``Action.ADMIN_TENANT_CONFIG`` (see
:func:`subscriptions._authorize_subscription_manage`).

# Idempotency — two layers

1. **HTTP ``Idempotency-Key`` header** (AGENTS.md §10): replay by
   request fingerprint; applies to every mutating endpoint under
   ``/api/v1`` via the middleware.
2. **Content idempotency on ``X-Webhook-Id``** (this slice): the use
   case looks up ``CapturedWebhookRepository.get_by_webhook_id``
   before writing anything and returns the prior result on a hit. The
   two layers compose — a retried POST with the same
   ``Idempotency-Key`` AND same ``X-Webhook-Id`` still hits the
   same captured row.

# Multi-tenant posture

The channel is resolved by ``(tenant_id, key)``. The tenant is
provided by the authenticated caller (``X-Dev-User`` +
``X-Dev-Tenant`` in ``local`` posture). A cross-tenant ``X-Webhook-Id``
is never observable; the ``CapturedWebhookRepository.get_by_webhook_id``
filter is ``WHERE tenant_id = :tenant_id``.

# Synthetic posture

The HMAC secret is stored in the local ``SecretProvider`` (file or
env). The P14 connector for outbound webhooks (FM-056) is a separate
seam; the inbound surface (FM-030) is this package.

# Why a package (R2 structural split)

Formerly a single ``webhooks.py`` module of 1030 lines — past the R2
1000-line hard cap (soft limit 600). Split by responsibility into
:mod:`core` (shared ``APIRouter`` + route-precedence fix),
:mod:`security` (HMAC constants and verification), :mod:`schemas`
(wire shapes for both surfaces), :mod:`receive` (the inbound
receiver endpoint), and :mod:`subscriptions` (outbound subscription
management). This ``__init__`` is a pure re-export shim: ``__all__``
and every previously visible attribute — public and private,
including names the old module merely imported, such as
``hashlib``/``hmac``/``json``/``status`` — are re-exported
unchanged, so no consumer import line needs editing. The route set,
route order, and OpenAPI operation ids are unchanged.
"""

from __future__ import annotations

from .core import (  # noqa: F401
    APIRouter,
    _static_routes_first,
    router,
)
from .receive import (  # noqa: F401
    UUID,
    AsyncSession,
    Clock,
    CorrelationContext,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
    UserContext,
    ValidationFailed,
    WebhookCommandKind,
    WebhookHttpDependencyProvider,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
    get_secret_provider,
    get_webhook_dependencies,
    json,
    receive_webhook,
    status,
    webhook_uc,
)
from .schemas import (  # noqa: F401
    DEFAULT_LIMIT,
    Any,
    BaseModel,
    Field,
    SubscriptionCreateRequest,
    SubscriptionListResponse,
    SubscriptionResponse,
    SubscriptionUpdateRequest,
    WebhookCommandRequest,
    WebhookCommandResponse,
)
from .security import (  # noqa: F401
    _HMAC_HEADER,
    _HMAC_REPLAY_WINDOW_SECONDS,
    _SECRET_NAME_TEMPLATE,
    _WEBHOOK_ID_HEADER,
    _WEBHOOK_SOURCE_HEADER,
    _WEBHOOK_TIMESTAMP_HEADER,
    SecretProvider,
    _constant_time_eq,
    _expected_secret_name,
    _verify_hmac,
    _verify_timestamp,
    compute_signature,
    hashlib,
    hmac,
)
from .subscriptions import (  # noqa: F401
    MAX_LIMIT,
    Action,
    DeliveryState,
    OutboundDelivery,
    Query,
    Resource,
    WebhookSubscription,
    _authorize_subscription_manage,
    _require_idempotency_key,
    _subscription_to_response,
    authorize,
    create_subscription,
    disable_subscription,
    list_subscriptions,
    outbound_uc,
    replay_subscription,
    retire_subscription,
    update_subscription,
)

__all__ = [
    "SubscriptionListResponse",
    "router",
    "compute_signature",
]

# why: the receive + subscriptions imports above have now registered every
# route; the precedence sort must run exactly once, after both, so static
# ``/webhooks/subscriptions...`` paths win over the templated channel
# catch-all (see :func:`core._static_routes_first` for the full rationale).
_static_routes_first()

# why this loop: importing the submodules above bound each one as an attribute
# of this package, but the pre-split single-file module exposed no such
# attributes. Popping them keeps ``dir(app.interfaces.http.routers.webhooks)``
# byte-identical to the old module's attribute surface — the R2 refactor must
# be invisible. The submodules stay importable (``...webhooks.receive``
# resolves through ``sys.modules``, which is untouched); only the redundant
# package-level attributes go away.
for _submodule in (
    "core",
    "receive",
    "schemas",
    "security",
    "subscriptions",
):
    globals().pop(_submodule, None)
del _submodule
