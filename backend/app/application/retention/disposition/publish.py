"""Publish a new versioned retention policy (P08-US3 / FM-040)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.application.retention.disposition.commands import (
    SUBJECT_TYPE_RETENTION_POLICY,
    _ActorProxy,
    _ClockLike,
    _new_correlation_id,
)
from app.domain.audit import RETENTION_POLICY_PUBLISHED
from app.domain.ports import AuditEventStore, RetentionPolicyRepository
from app.domain.retention import RetentionCategory, RetentionPolicy


@dataclass(frozen=True, slots=True)
class PublishRetentionPolicyCommand:
    """Input to :func:`publish_retention_policy`.

    ``period_days`` is the only required operational knob; the
    aggregate translates it to a ``timedelta`` on construction. The
    application layer is responsible for resolving the next
    ``version_number`` via the repository — see
    :meth:`RetentionPolicyRepository.next_version_number`.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    category: RetentionCategory
    target_key: str = ""
    period_days: int = 0
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class RetentionPolicyResult:
    """The post-commit outcome returned to the router.

    ``policy`` is the persisted :class:`RetentionPolicy` row the
    router projects onto the wire response.
    """

    policy: RetentionPolicy


async def publish_retention_policy(
    cmd: PublishRetentionPolicyCommand,
    *,
    policy_repo: RetentionPolicyRepository,
    audit: AuditEventStore,
    clock: _ClockLike,
) -> RetentionPolicyResult:
    """Publish a new version of a retention policy.

    Failure behavior:
    - Auth/not-found errors are raised before any DB write.
    - The draft policy, the published status flip, and the
      ``RetentionPolicyPublished`` audit row share one transaction.
      A partial commit is impossible (Constitution III).
    """
    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.RETENTION_POLICY_PUBLISH,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind=SUBJECT_TYPE_RETENTION_POLICY),
    )

    # Resolve the next version number per
    # ``(tenant_id, category, target_key)``. The repository is the
    # authoritative counter; the application layer does not guess.
    next_version = await policy_repo.next_version_number(
        cmd.tenant_id,
        category=cmd.category.value,
        target_key=cmd.target_key.strip(),
    )

    now = clock.now()
    draft = RetentionPolicy.draft(
        tenant_id=cmd.tenant_id,
        category=cmd.category,
        target_key=cmd.target_key.strip(),
        period=timedelta(days=cmd.period_days),
        created_at=now,
    )
    # The aggregate factory sets ``version_number=1``; we overwrite
    # with the resolved number so the first publish lands at the
    # correct per-target counter (1, 2, 3...).
    numbered = replace(draft, version_number=next_version)
    published = numbered.publish(actor_id=cmd.actor_id, at=now)

    await policy_repo.add(published)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=RETENTION_POLICY_PUBLISHED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_RETENTION_POLICY,
        subject_id=published.id,
        case_id=None,
        correlation_id=cmd.correlation_id or _new_correlation_id(),
        causation_id=None,
        payload={
            "category": published.category.value,
            "target_key": published.target_key,
            "period_days": cmd.period_days,
            "version_number": published.version_number,
        },
        occurred_at=now,
    )

    return RetentionPolicyResult(policy=published)
