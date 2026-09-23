"""Channel enable / disable use cases (P04-US4 / FM-027).

Both entry points require ``ADMIN_TENANT_CONFIG`` (a ``process_admin``
grants it; ``intake`` / ``reviewer`` / ``supervisor`` do not). Every
repository call is tenant-scoped; a cross-tenant channel id surfaces as
:class:`NotFound` so the API never confirms cross-tenant existence
(Constitution V). Each operation writes the channel change, an audit row,
and an outbox event in the same DB transaction.

Split out of the former single-file ``manage_channels.py`` during the R2
package split; the quarantine lifecycle lives in :mod:`.quarantine`.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.application.authorization import Action, Resource, authorize
from app.domain.channel import Channel
from app.domain.errors import NotFound
from app.domain.ports import AuditEventStore, ChannelRepository, OutboxWriter

from .commands import (
    DisableChannelCommand,
    DisableChannelResult,
    EnableChannelCommand,
    EnableChannelResult,
)
from .constants import (
    EVENT_CHANNEL_DISABLED,
    EVENT_CHANNEL_ENABLED,
    EVENT_CHANNEL_POLICY_UPDATED,
)
from .helpers import _emit_audit, _policy_to_dict, _user_context


async def enable_channel(
    cmd: EnableChannelCommand,
    *,
    channel_repo: ChannelRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> EnableChannelResult:
    """Enable or reconfigure a :class:`Channel` for the actor's tenant.

    When ``existing_channel_id`` is provided the policy / enabled flag
    is updated on that row; otherwise a fresh row is created. Both
    branches append the appropriate ``Channel*`` audit + outbox event
    so the operator console reflects the change immediately.
    """
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="channel"),
    )

    now = clock.now()
    if cmd.existing_channel_id is not None:
        existing = await channel_repo.get(cmd.tenant_id, cmd.existing_channel_id)
        if existing is None:
            raise NotFound(f"channel {cmd.existing_channel_id} not found in tenant")
        updated = existing.update_policy(policy=cmd.policy, at=now)
        if updated.enabled != cmd.enabled:
            updated = updated.enable(at=now) if cmd.enabled else updated.disable(at=now)
        await channel_repo.update(updated)
        event_type = EVENT_CHANNEL_POLICY_UPDATED
        await _emit_audit(
            audit,
            tenant_id=cmd.tenant_id,
            actor_id=cmd.actor_id,
            subject_id=updated.id,
            event_type=event_type,
            payload={
                "channel_key": updated.key,
                "policy": _policy_to_dict(updated.policy),
                "enabled": updated.enabled,
            },
            correlation_id=cmd.correlation_id,
            occurred_at=now,
        )
        await outbox.enqueue(
            tenant_id=cmd.tenant_id,
            event_type=event_type,
            event_id=uuid4(),
            payload={
                "channel_id": str(updated.id),
                "tenant_id": str(cmd.tenant_id),
                "channel_key": updated.key,
                "enabled": updated.enabled,
                "occurred_at": now.isoformat(),
            },
        )
        return EnableChannelResult(channel=updated, created=False)

    # Create a new channel row.
    channel = Channel.create(
        tenant_id=cmd.tenant_id,
        key=cmd.key,
        kind=cmd.kind,
        policy=cmd.policy,
        created_by=cmd.actor_id,
        now=now,
        enabled=cmd.enabled,
    )
    await channel_repo.add(channel)
    event_type = EVENT_CHANNEL_ENABLED if channel.enabled else EVENT_CHANNEL_DISABLED
    await _emit_audit(
        audit,
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor_id,
        subject_id=channel.id,
        event_type=event_type,
        payload={
            "channel_key": channel.key,
            "kind": channel.kind.value,
            "policy": _policy_to_dict(channel.policy),
            "enabled": channel.enabled,
        },
        correlation_id=cmd.correlation_id,
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=event_type,
        event_id=uuid4(),
        payload={
            "channel_id": str(channel.id),
            "tenant_id": str(cmd.tenant_id),
            "channel_key": channel.key,
            "enabled": channel.enabled,
            "occurred_at": now.isoformat(),
        },
    )
    return EnableChannelResult(channel=channel, created=True)


async def disable_channel(
    cmd: DisableChannelCommand,
    *,
    channel_repo: ChannelRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> DisableChannelResult:
    """Disable a channel. Idempotent."""
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="channel"),
    )

    channel = await channel_repo.get(cmd.tenant_id, cmd.channel_id)
    if channel is None:
        raise NotFound(f"channel {cmd.channel_id} not found in tenant")
    now = clock.now()
    updated = channel.disable(at=now)
    await channel_repo.update(updated)
    await _emit_audit(
        audit,
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor_id,
        subject_id=updated.id,
        event_type=EVENT_CHANNEL_DISABLED,
        payload={
            "channel_key": updated.key,
            "enabled": False,
        },
        correlation_id=cmd.correlation_id,
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_CHANNEL_DISABLED,
        event_id=uuid4(),
        payload={
            "channel_id": str(updated.id),
            "tenant_id": str(cmd.tenant_id),
            "channel_key": updated.key,
            "occurred_at": now.isoformat(),
        },
    )
    return DisableChannelResult(channel=updated)


__all__ = [
    "disable_channel",
    "enable_channel",
]
