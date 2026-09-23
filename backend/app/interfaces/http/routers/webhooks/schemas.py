"""Pydantic wire shapes for the webhooks router endpoints.

Request/response models shared by the two surfaces of this package:

- The **inbound** command body and result of
  ``POST /api/v1/webhooks/{channel}`` (:mod:`receive`).
- The **outbound** subscription CRUD bodies and results under
  ``/api/v1/webhooks/subscriptions...`` (:mod:`subscriptions`).

The models are transport-only: they carry no tenant/actor fields —
those are filled in by the endpoints from the authenticated caller.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.interfaces.http.pagination import DEFAULT_LIMIT

# ---------------------------------------------------------------------------
# Wire shape — inbound command
# ---------------------------------------------------------------------------


class WebhookCommandRequest(BaseModel):
    """Body of ``POST /api/v1/webhooks/{channel}``.

    The shape is the use case's :class:`webhook_uc.WebhookCommand`
    minus the transport fields. The router fills in ``tenant_id``,
    ``channel_id``, ``source_system``, ``webhook_id``, ``actor_id``,
    and ``correlation_id`` from the verified request.
    """

    command_kind: str = Field(
        description="case.transition | case.comment | case.note",
    )
    case_id: str = Field(description="UUID of the case the command targets")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Command-specific payload (transition_key, text, ...)",
    )


class WebhookCommandResponse(BaseModel):
    """Wire shape returned by ``POST /api/v1/webhooks/{channel}``.

    ``replayed=True`` means the webhook id was already on file; the
    applied_command below is the *original* capture's and no new
    command was applied.
    """

    captured_id: str
    channel_key: str
    webhook_id: str
    source_system: str
    command_kind: str
    case_id: str
    applied_command: dict[str, Any]
    replayed: bool
    correlation_id: str


# ---------------------------------------------------------------------------
# Wire shape — outbound subscription management (FM-056 / P13-US10 / ADR-013)
# ---------------------------------------------------------------------------


class SubscriptionCreateRequest(BaseModel):
    """Body of ``POST /api/v1/webhooks/subscriptions``.

    The shape is the application layer's
    :class:`RegisterSubscriptionCommand` minus transport fields
    (``tenant_id`` is taken from the authenticated caller).
    """

    url: str = Field(description="HTTPS URL deliveries are POSTed to")
    event_filters: list[str] = Field(
        description="event_type values this subscription accepts",
    )
    secret_name: str = Field(
        description=(
            "Logical key into SecretProvider. The platform resolves "
            "the value through SecretProvider.get(secret_name)."
        ),
    )
    signing_algorithm: str = Field(
        default="hmac_sha256",
        description="hmac_sha256 | jwt_bearer",
    )
    require_bearer: bool = Field(
        default=False,
        description="When true, an Authorization: Bearer <jwt> is attached",
    )
    description: str = Field(default="", description="Operator-facing label")


class SubscriptionUpdateRequest(BaseModel):
    """Body of ``PATCH /api/v1/webhooks/subscriptions/{id}``."""

    version_token: int = Field(
        description="The version_token the caller observed; rejected on stale"
    )
    url: str | None = None
    event_filters: list[str] | None = None
    secret_name: str | None = None
    signing_algorithm: str | None = None
    require_bearer: bool | None = None
    description: str | None = None


class SubscriptionResponse(BaseModel):
    """Wire shape returned for a subscription."""

    id: str
    tenant_id: str
    url: str
    event_filters: list[str]
    secret_name: str
    signing_algorithm: str
    require_bearer: bool
    status: str
    version_token: int
    description: str
    created_at: str
    updated_at: str
    correlation_id: str = ""


class SubscriptionListResponse(BaseModel):
    """Paginated list of outbound webhook subscriptions (Page contract)."""

    items: list[SubscriptionResponse]
    next_cursor: str | None = None
    limit: int = DEFAULT_LIMIT
