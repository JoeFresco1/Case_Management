"""Inbound webhook command use case (P04-US8 / FM-030, ADR-007).

The use case turns an authorized inbound webhook into a durable
``CapturedWebhook`` provenance row plus a ``CaseTransitioned`` audit
+ outbox record — the deterministic half of the seam. The HTTP router
in :mod:`app.interfaces.http.routers.webhooks` is responsible for
**HMAC verification** (transport-level trust); this use case owns
**content validation, channel governance, idempotency, provenance, and
the case-command application** (application-level trust).

# Why inbound webhooks are split from outbound (FM-056)

Inbound (this use case) and outbound (FM-056) webhooks have opposite
trust directions and failure semantics. The platform is the *consumer*
of inbound webhooks and the *producer* of outbound webhooks. The
authorization model, idempotency anchor, and the failure story are
all inverted; merging them would conflate two unrelated contracts
(see ``01-deduplicated-feature-map.md:189`` and ADR-007).

# Flow

1. **Validate** the command surface — ``command_kind`` must be a
   supported enum value, the ``payload`` must be a dict, the
   ``webhook_id`` must be a non-empty visible-ASCII string. Validation
   raises before any DB write so a malformed capture never touches
   storage.
2. **Resolve the channel** for the actor's tenant. The channel must be
   of kind ``ChannelKind.WEBHOOK`` and **enabled** — a disabled
   channel refuses new captures (fail-closed, mirrors the email
   path in :mod:`app.application.capture.email_capture`).
3. **Resolve the case** within the actor's tenant. Cross-tenant
   lookups return ``NotFound`` so the API never confirms
   cross-tenant existence (Constitution V).
4. **Authorize** the actor against ``Action.CASE_TRANSITION`` on the
   case. The policy is deny-by-default (P11-US1). A webhook cannot
   elevate an actor — even an ``is_system`` caller must hold the
   action; the bypass set is empty today.
5. **Content idempotency** — ``CapturedWebhookRepository.get_by_webhook_id``
   is consulted first. A hit returns the prior ``CapturedWebhook``
   record unchanged — no audit row, no outbox row, no second command
   applied. This is the content idempotency layer; it composes with
   the HTTP ``Idempotency-Key`` middleware (AGENTS.md §10) but is
   independent of it.
6. **Persist** ``CapturedWebhook`` + ``CaseTransitioned`` audit + outbox
   in the **same transaction**. A partial commit would leave a command
   without provenance or a provenance row without a command — both
   are explicitly forbidden (Constitution V, P09-US1).

# HMAC posture

HMAC verification is the router's responsibility (transport-level
trust). The use case accepts only the **already-verified** producer
identity (``source_system``); it does not re-verify the signature. A
``source_system`` whose HMAC has not been verified is a contract
violation at the router boundary and must not reach this use case.
The seam is the parameter: the router must call this use case only
after ``verify_hmac`` has passed.

# Failure behavior

- Auth / validation / not-found errors raise before any DB write.
- A duplicate ``webhook_id`` returns the prior result and writes
  nothing; the audit + outbox trail is not duplicated.
- A disabled / wrong-kind channel is refused with ``ValidationFailed``
  (``422``) at the use case boundary — the router maps the error to
  ``403`` for governance reasons (the operator explicitly disabled
  the channel).

# INTEGRATION_SEAM: webhook source

The HMAC secret is resolved through the ``SecretProvider`` port
(``AGENTS.md §12``). The local adapter reads from
``./.secrets.json``; the Azure adapter will read from Key Vault. The
router obtains the secret via the port; this use case does not see
the secret at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.domain.channel import ChannelKind
from app.domain.errors import NotFound, ValidationFailed
from app.domain.ports import (
    AuditEventStore,
    CapturedWebhookRepository,
    CaseRepository,
    ChannelRepository,
    OutboxWriter,
)
from app.domain.webhook_capture import (
    CapturedWebhook,
    WebhookCommandKind,
    is_valid_webhook_id,
)

# Stable subject-type + event-type literals. Subscribers (audit
# timeline, outbox consumers) depend on these strings; renaming them
# is a breaking change for downstream consumers.
SUBJECT_TYPE_WEBHOOK = "webhook_capture"
SUBJECT_TYPE_CASE = "case"

EVENT_WEBHOOK_CAPTURED = "WebhookCaptured"
EVENT_CASE_TRANSITIONED = "CaseTransitioned"

# Reasonable payload ceiling. The channel's ``ValidationPolicy`` can
# override per-tenant; this default applies when the policy does not
# set a value.
DEFAULT_MAX_WEBHOOK_BYTES = 256 * 1024


# ---------------------------------------------------------------------------
# Command + result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WebhookCommand:
    """The input to :func:`apply_webhook_command`.

    ``tenant_id`` is taken from the channel resolution (which the
    router scopes via the ``X-Webhook-Tenant`` header in ``local``
    posture), not the request body. ``source_system`` is the
    producer's identifier (resolved by the router from the HMAC
    secret name); the use case treats it as opaque provenance.

    ``case_id`` is the case the command targets. The use case does
    not currently support commands that span cases (FM-030 is a
    case-command surface, not a tenant-wide command surface — that
    is the outbound ``FM-055`` event catalog's job).
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    channel_id: UUID
    source_system: str
    webhook_id: str
    command_kind: WebhookCommandKind
    case_id: UUID
    payload: dict[str, Any]
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class WebhookCommandResult:
    """The outcome of a successful (or idempotently-replayed) webhook.

    ``replayed=True`` means a prior ``CapturedWebhook`` was found for
    the same ``(tenant_id, channel_id, webhook_id)``; the row in this
    response is the *original* one and no new command was applied.
    """

    captured: CapturedWebhook
    applied_command: dict[str, Any]
    replayed: bool


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


async def apply_webhook_command(
    cmd: WebhookCommand,
    *,
    channel_repo: ChannelRepository,
    case_repo: CaseRepository,
    captured_webhook_repo: CapturedWebhookRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> WebhookCommandResult:
    """Apply one authorized inbound webhook as a case command.

    Failure behavior mirrors :func:`app.application.capture.email_capture.capture_email`:

    - Auth / validation / not-found errors are raised before any DB
      write.
    - On a duplicate ``webhook_id`` the prior ``CapturedWebhook`` is
      returned without writing a new audit row, outbox row, or
      command record. The ``replayed`` flag distinguishes the
      idempotent replay from a fresh capture.
    - On an unexpected storage failure the use case propagates
      ``DependencyUnavailable``; no metadata / audit / outbox row is
      written. The caller's transaction rollback keeps partial state
      out of the database.
    """
    # 1. Pre-flight validation. All checks here are cheap and raise
    # before any I/O so a malformed capture never touches storage.
    if not is_valid_webhook_id(cmd.webhook_id):
        raise ValidationFailed(
            "webhook_id is required and must be a non-empty visible-ASCII string",
            field="webhook_id",
        )
    if not cmd.source_system:
        raise ValidationFailed(
            "source_system is required and must be a non-empty string",
            field="source_system",
        )
    if not isinstance(cmd.payload, dict):
        raise ValidationFailed(
            "payload is required and must be a JSON object",
            field="payload",
        )
    if not cmd.payload:
        raise ValidationFailed(
            "payload is required and must contain at least one key",
            field="payload",
        )

    # 2. Resolve the channel. The channel must be of kind WEBHOOK and
    # enabled — a disabled channel is a fail-closed refusal.
    channel = await channel_repo.get(cmd.tenant_id, cmd.channel_id)
    if channel is None:
        raise NotFound(f"channel {cmd.channel_id} not found in tenant")
    if channel.kind is not ChannelKind.WEBHOOK:
        raise ValidationFailed(
            f"channel {cmd.channel_id} is not a webhook channel (kind={channel.kind.value})",
            field="channel_id",
        )
    if not channel.enabled:
        raise ValidationFailed(
            f"channel {channel.key!r} is disabled",
            field="channel_id",
        )

    # 3. Resolve the case. Cross-tenant lookups return None and
    # surface as NotFound so the API never confirms cross-tenant
    # existence.
    case = await case_repo.get(cmd.tenant_id, cmd.case_id)
    if case is None:
        raise NotFound(f"case {cmd.case_id} not found in tenant")

    # 4. Authorize the actor. The same gate that guards an in-app
    # transition guards a webhook-driven transition; there is no
    # "webhook bypass" path. The source_system claim is provenance,
    # not authority.
    await authorize(
        Action.CASE_TRANSITION,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="case", id=case.id),
    )

    # 5. Content idempotency. A prior capture with the same webhook id
    # wins — return it without re-running the command pipeline.
    prior = await captured_webhook_repo.get_by_webhook_id(
        tenant_id=cmd.tenant_id,
        channel_id=channel.id,
        webhook_id=cmd.webhook_id,
    )
    if prior is not None:
        return WebhookCommandResult(
            captured=prior,
            applied_command={
                "command_kind": prior.command_kind.value,
                "case_id": str(prior.case_id),
            },
            replayed=True,
        )

    # 6. Persist (CapturedWebhook + CaseTransitioned audit + outbox)
    # in the SAME transaction.
    now = clock.now()
    captured = CapturedWebhook.create(
        tenant_id=cmd.tenant_id,
        channel_id=channel.id,
        channel_key=channel.key,
        webhook_id=cmd.webhook_id,
        source_system=cmd.source_system,
        command_kind=cmd.command_kind,
        case_id=case.id,
        payload=cmd.payload,
        captured_at=now,
        captured_by=cmd.actor_id,
        correlation_id=cmd.correlation_id,
    )
    await captured_webhook_repo.add(captured)

    # 6a. The audit row carries both the webhook provenance AND the
    # case-command intent. Two subject_ids are awkward in the audit
    # schema; we use the webhook as the primary subject and put the
    # case_id on the row. The case-side timeline view is built by
    # filtering audit rows by ``case_id``.
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_WEBHOOK_CAPTURED,
        actor_id=cmd.actor_id,
        actor_type="webhook",
        subject_type=SUBJECT_TYPE_WEBHOOK,
        subject_id=captured.id,
        case_id=case.id,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "channel_id": str(channel.id),
            "channel_key": channel.key,
            "webhook_id": cmd.webhook_id,
            "source_system": cmd.source_system,
            "command_kind": captured.command_kind.value,
            "case_id": str(case.id),
            "payload_digest": captured.payload_digest,
        },
        occurred_at=now,
    )

    # 6b. The case-side "state change" record. The transition is
    # recorded as a ``CaseTransitioned`` audit + outbox event so the
    # case timeline reflects the external system caused a state
    # change. The actual transition key + target state live in the
    # payload — the workflow engine (P02) consumes the outbox event
    # and applies the transition deterministically (FM-014).
    transition_key = str(cmd.payload.get("transition_key", ""))
    target_state = str(cmd.payload.get("target_state", ""))
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_CASE_TRANSITIONED,
        actor_id=cmd.actor_id,
        actor_type="webhook",
        subject_type=SUBJECT_TYPE_CASE,
        subject_id=case.id,
        case_id=case.id,
        correlation_id=cmd.correlation_id,
        causation_id=str(captured.id),
        payload={
            "webhook_capture_id": str(captured.id),
            "source_system": cmd.source_system,
            "webhook_id": cmd.webhook_id,
            "channel_id": str(channel.id),
            "channel_key": channel.key,
            "transition_key": transition_key,
            "target_state": target_state,
            "command_kind": captured.command_kind.value,
        },
        occurred_at=now,
    )

    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_CASE_TRANSITIONED,
        event_id=uuid4(),
        payload={
            "case_id": str(case.id),
            "tenant_id": str(cmd.tenant_id),
            "webhook_capture_id": str(captured.id),
            "source_system": cmd.source_system,
            "webhook_id": cmd.webhook_id,
            "channel_id": str(channel.id),
            "channel_key": channel.key,
            "transition_key": transition_key,
            "target_state": target_state,
            "command_kind": captured.command_kind.value,
            "occurred_at": now.isoformat(),
        },
    )

    return WebhookCommandResult(
        captured=captured,
        applied_command={
            "command_kind": captured.command_kind.value,
            "case_id": str(case.id),
            "transition_key": transition_key,
            "target_state": target_state,
        },
        replayed=False,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_context(cmd: WebhookCommand) -> Any:
    """Duck-typed ``UserContext`` for the policy.

    The application layer does not import ``app.deps.UserContext`` to
    keep the boundary free of FastAPI. The policy reads only
    ``tenant_id``, ``user_id``, ``roles``, and ``is_system``.

    ``is_system=False`` is explicit: a webhook never has system
    authority. If the integration is meant to run with bypass
    authority it must be wired through the role-assignment table
    (P11-US4), not the channel configuration.
    """
    return _UserProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )


@dataclass(frozen=True, slots=True)
class _UserProxy:
    """Duck-typed ``UserContext`` for the policy.

    Mirrors the proxy used in ``app.application.cases.create_case`` and
    ``app.application.capture.email_capture`` so the policy can be
    exercised without dragging FastAPI into the test surface.
    """

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


__all__ = [
    "DEFAULT_MAX_WEBHOOK_BYTES",
    "EVENT_CASE_TRANSITIONED",
    "EVENT_WEBHOOK_CAPTURED",
    "SUBJECT_TYPE_CASE",
    "SUBJECT_TYPE_WEBHOOK",
    "WebhookCommand",
    "WebhookCommandResult",
    "apply_webhook_command",
]
