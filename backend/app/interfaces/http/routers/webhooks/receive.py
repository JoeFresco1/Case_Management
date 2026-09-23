"""Inbound receiver endpoint — ``POST /api/v1/webhooks/{channel}`` (P04-US8 / FM-030, ADR-007).

Receive one HMAC-signed inbound webhook event for the named channel
and turn it into a case command / state change with provenance.
Duplicate webhook ids are idempotent; the prior result is replayed.

# Authorization

Two gates guard this endpoint (the package docstring carries the full
discussion):

1. **Transport-level** (this module via :mod:`security`): HMAC
   verification against the channel's shared secret. A failed
   signature returns ``401`` so the platform never confirms the
   channel's existence to a non-authenticated caller (Constitution V).
2. **Application-level** (the use case): the same ``case.transition``
   gate that guards an in-app transition guards a webhook-driven
   transition. The actor's identity is the *caller* (a human user in
   ``local`` posture) — a webhook is not system authority.

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
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.capture import webhook_commands as webhook_uc
from app.application.webhooks.http_dependencies import WebhookHttpDependencyProvider
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
    get_secret_provider,
)
from app.domain.errors import ValidationFailed
from app.domain.webhook_capture import WebhookCommandKind
from app.interfaces.http.dependencies import get_webhook_dependencies

from .core import router
from .schemas import WebhookCommandRequest, WebhookCommandResponse
from .security import (
    _HMAC_HEADER,
    _WEBHOOK_ID_HEADER,
    _WEBHOOK_SOURCE_HEADER,
    _WEBHOOK_TIMESTAMP_HEADER,
    _verify_hmac,
)

# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/webhooks/{channel}",
    response_model=WebhookCommandResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Receive one authorized inbound webhook command",
    responses={
        status.HTTP_200_OK: {"description": "Duplicate webhook id — prior result replayed"},
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_401_UNAUTHORIZED: {
            "description": "HMAC verification failed (signature, timestamp, or channel secret)"
        },
        status.HTTP_403_FORBIDDEN: {"description": "Caller lacks case.transition on the case"},
        status.HTTP_404_NOT_FOUND: {"description": "Channel or case not found in tenant"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid payload (unknown command_kind, malformed UUID, etc.)"
        },
    },
)
async def receive_webhook(
    channel: str,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    signature: str | None = Header(default=None, alias=_HMAC_HEADER),
    timestamp: str | None = Header(default=None, alias=_WEBHOOK_TIMESTAMP_HEADER),
    webhook_id: str | None = Header(default=None, alias=_WEBHOOK_ID_HEADER),
    source: str | None = Header(default=None, alias=_WEBHOOK_SOURCE_HEADER),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    secret_provider: Any = Depends(get_secret_provider),
    dependencies: WebhookHttpDependencyProvider = Depends(get_webhook_dependencies),
) -> WebhookCommandResponse:
    """Receive one inbound webhook command.

    Headers:

    - ``X-Webhook-Signature: sha256=<hex>`` — HMAC-SHA256 over
      ``f"{timestamp}.{body}"`` with the channel's shared secret.
    - ``X-Webhook-Timestamp: <unix-epoch>`` — replay window of 5 min.
    - ``X-Webhook-Id: <stable producer id>`` — durable idempotency
      anchor (the resource-level key, not the HTTP
      ``Idempotency-Key``).
    - ``X-Webhook-Source: <producer label>`` — provenance label for
      the audit chain. Defaults to the channel key.
    - ``Idempotency-Key: <uuid>`` — request-level replay key (HTTP
      middleware).

    Body:

    ```json
    {
      "command_kind": "case.transition",
      "case_id": "<uuid>",
      "payload": {"transition_key": "submit_for_review", "target_state": "in_review"}
    }
    ```
    """
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_idempotency_key",
                "message": "Idempotency-Key header is required for this endpoint",
            },
        )
    if not webhook_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_webhook_id",
                "message": f"{_WEBHOOK_ID_HEADER} header is required",
            },
        )

    body = await request.body()
    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_body",
                "message": "webhook request body is required",
            },
        )

    # 1. HMAC verification (transport-level trust). Raises 401 on any
    # failure; we never reach the use case without a verified signature.
    now_epoch = int(clock.now().timestamp())
    verified_source = await _verify_hmac(
        body=body,
        signature=signature,
        timestamp=timestamp,
        secret=secret_provider,  # type: ignore[arg-type]
        channel_key=channel,
        now_epoch=now_epoch,
    )

    # 2. Parse the body. The body was used verbatim for HMAC, so the
    # parsed shape MUST match the signature byte-for-byte. We parse
    # strictly — a trailing newline or a different field order would
    # have failed HMAC already.
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_json",
                "message": f"webhook body is not valid JSON: {exc}",
            },
        ) from exc

    try:
        request_payload = WebhookCommandRequest.model_validate(parsed)
    except Exception as exc:  # pydantic.ValidationError
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "validation_failed",
                "message": "webhook payload is invalid",
                "details": {"errors": str(exc)},
            },
        ) from exc

    try:
        command_kind = WebhookCommandKind(request_payload.command_kind)
    except ValueError as exc:
        raise ValidationFailed(
            f"unknown command_kind {request_payload.command_kind!r}",
            field="command_kind",
        ) from exc

    try:
        case_id = UUID(request_payload.case_id)
    except ValueError as exc:
        raise ValidationFailed(
            f"case_id is not a valid UUID: {request_payload.case_id!r}",
            field="case_id",
        ) from exc

    # 3. Resolve the channel (tenant-scoped). A cross-tenant channel
    # key returns None and surfaces as 404 — the API never confirms
    # cross-tenant existence (Constitution V).
    repos = dependencies.for_session(db)
    channel_repo = repos.channel
    channel_agg = await channel_repo.get_by_key(user.tenant_id, channel)
    if channel_agg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "channel_not_found",
                "message": f"webhook channel {channel!r} not found in tenant",
            },
        )

    # 4. Apply the use case. Authorization (case.transition),
    # validation, and content idempotency all live inside the use
    # case so they share a single transaction with the audit + outbox
    # writes.
    case_repo = repos.case
    captured_repo = repos.captured_webhook
    audit_store = repos.audit
    outbox = repos.outbox

    cmd = webhook_uc.WebhookCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        channel_id=channel_agg.id,
        source_system=(source or verified_source),
        webhook_id=webhook_id,
        command_kind=command_kind,
        case_id=case_id,
        payload=request_payload.payload,
        correlation_id=correlation.correlation_id,
    )
    result = await webhook_uc.apply_webhook_command(
        cmd,
        channel_repo=channel_repo,
        case_repo=case_repo,
        captured_webhook_repo=captured_repo,
        audit=audit_store,
        outbox=outbox,
        clock=clock,
    )
    await db.commit()

    response = WebhookCommandResponse(
        captured_id=str(result.captured.id),
        channel_key=result.captured.channel_key,
        webhook_id=result.captured.webhook_id,
        source_system=result.captured.source_system,
        command_kind=result.captured.command_kind.value,
        case_id=str(result.captured.case_id),
        applied_command=result.applied_command,
        replayed=result.replayed,
        correlation_id=correlation.correlation_id,
    )
    # Override the response status when this is an idempotent replay
    # so the client can distinguish "duplicate" (200) from "fresh"
    # (201) without parsing the body.
    response.status_code = status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED
    return response
