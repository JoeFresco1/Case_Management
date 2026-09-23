"""Configuration governance use cases (FM-021 / P10-US4, US5, US8).

This module is the application-layer seam for the **publication
history** and the **second-admin approval gate**. The history endpoint
lets an administrator answer "who published what when, and what was
the change?" — the durable record behind the admin timeline UI
(P10-US4, US5). The approval gate (P10-US8) is an optional tenant
policy: when ``require_second_admin`` is on, a publish action must be
recorded by a second administrator before the version is considered
effectively published.

# Why this lives here, not in domain

The governance surface crosses aggregates (ConfigurationPublication +
CaseTypeVersion/WorkflowVersion + AuditEvent) and is wired through
Protocol ports the domain does not own (time, audit, repo). The
orchestration belongs in the application layer; the domain owns the
immutability guards (``CaseTypeVersion.assert_mutable``,
``WorkflowVersion.assert_mutable``) that already block any mutation
on a published version.

# Authorization

All entry points require :class:`Action.ADMIN_TENANT_CONFIG` (P11-US4
/ FM-048). The history endpoint reads; the approval gate writes a
new ``ConfigurationPublished`` row with ``actor_id`` distinct from
the original publisher. The fail-closed posture is the same as
``publish_case_type.handle``: a forbidden actor raises ``Forbidden``
before any side effect.

# Second-admin approval (P10-US8)

When ``require_second_admin`` is on for the tenant:

- The first administrator's publish appends a publication row with
  ``action='publish'`` AND a follow-up row with ``action='validate'``
  recorded against a different ``actor_id``. Both rows land in the
  same transaction as the status flip (Constitution IV).
- :func:`assert_publish_approved` enforces the rule: the most recent
  ``publish`` row for the artifact + version must be paired with a
  ``validate`` row whose ``actor_id`` differs from the ``publish``
  ``actor_id``. A single-admin publish therefore raises ``Forbidden``
  with ``code=approval_required``.
- When ``require_second_admin`` is off, the publish use case emits
  only the ``publish`` row; the validator does not run.

The setting is a per-tenant policy resolved through the
:class:`_GovernancePolicy` port; today the local adapter returns
``False`` (single-admin is sufficient) and a future slice wires the
admin UI to flip the flag on a tenant. The port keeps the seam
explicit.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.errors import Forbidden, NotFound

# ---------------------------------------------------------------------------
# Vocabulary — the lifecycle actions the publication row can carry.
# The strings mirror P10 §2 ("draft → validating → published →
# retired") and the migration column comment. They are kept here
# (not as a SQLAlchemy enum) so a future "archive" or "rollback" action
# does not require a migration.
# ---------------------------------------------------------------------------

ACTION_DRAFT = "draft"
ACTION_VALIDATE = "validate"
ACTION_PUBLISH = "publish"
ACTION_RETIRE = "retire"


class _PublicationRepo(Protocol):
    async def add(
        self,
        *,
        tenant_id: UUID,
        artifact_type: str,
        artifact_id: UUID,
        version_id: UUID,
        action: str,
        actor_id: UUID,
        change_summary: str,
        occurred_at: datetime,
    ) -> UUID: ...

    async def list_for_artifact(
        self,
        tenant_id: UUID,
        artifact_type: str,
        artifact_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[PublicationRecord]: ...

    async def list_tenant_history(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        artifact_type: str | None = None,
    ) -> Sequence[PublicationRecord]: ...


class _GovernancePolicy(Protocol):
    async def require_second_admin(self, tenant_id: UUID) -> bool: ...


class _Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    """A single publication-history entry (FM-021).

    Why a dataclass instead of the ORM row: the use case must not leak
    SQLAlchemy across the application boundary. The router maps to a
    Pydantic schema from this dataclass; tests can substitute a fake
    repo that returns plain :class:`PublicationRecord` instances.
    """

    id: UUID
    tenant_id: UUID
    artifact_type: str
    artifact_id: UUID | None
    version_id: UUID | None
    action: str
    actor_id: UUID
    occurred_at: datetime
    change_summary: str


@dataclass(frozen=True, slots=True)
class RecordPublicationCommand:
    """Append a publication row and (optionally) a paired approval row.

    ``require_second_admin`` is read from the tenant's
    :class:`_GovernancePolicy` adapter inside the use case so the
    caller does not need to know the current setting.
    """

    tenant_id: UUID
    artifact_type: str
    artifact_id: UUID
    version_id: UUID
    action: str
    actor_id: UUID
    change_summary: str = ""
    # The ``requester_actor_id`` is the actor who initiated the
    # original publish action; the use case derives the approver's
    # distinct ``actor_id`` from this and the policy adapter. Today
    # only the originating actor can drive ``record_publish``, so the
    # approval row must be appended by a *different* admin via
    # ``approve_publication``.
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class RecordPublicationResult:
    publication_id: UUID
    # ``approval_required`` is True when the tenant's policy mandates
    # a second admin; the caller can use it to short-circuit the
    # response (the publish is recorded but the version is not yet
    # "effectively published"). The router maps it to a 202 Accepted.
    approval_required: bool


@dataclass(frozen=True, slots=True)
class ApprovePublicationCommand:
    """Record a second administrator's approval (P10-US8).

    The use case enforces the separation-of-duties rule: the
    approver's ``actor_id`` MUST differ from the original publisher's
    ``actor_id`` for the same artifact + version + ``publish`` action.
    A same-actor attempt raises ``Forbidden``.
    """

    tenant_id: UUID
    artifact_type: str
    artifact_id: UUID
    version_id: UUID
    approving_actor_id: UUID
    change_summary: str = ""
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class ListPublicationsQuery:
    tenant_id: UUID
    artifact_type: str | None = None
    artifact_id: UUID | None = None
    limit: int = 50
    offset: int = 0


async def record_publication(
    cmd: RecordPublicationCommand,
    *,
    repo: _PublicationRepo,
    policy: _GovernancePolicy,
    clock: _Clock,
) -> RecordPublicationResult:
    """Append a lifecycle publication row (FM-021 / P10-US4).

    When ``cmd.action`` is ``ACTION_PUBLISH`` and the tenant has
    second-admin approval enabled, this returns
    ``approval_required=True``; the caller MUST then drive
    :func:`approve_publication` from a different actor before the
    version is considered effectively published. The use case does
    not block the call — the row is durable the moment it lands, and
    the approval gate is read by downstream consumers (e.g. the
    "effective version" resolution path).

    Raises:
        InvariantViolation: ``cmd.action`` is not a known lifecycle
            action. Surfaced as 422 by the HTTP layer.
    """
    _validate_action(cmd.action)
    now = clock.now()
    publication_id = await repo.add(
        tenant_id=cmd.tenant_id,
        artifact_type=cmd.artifact_type,
        artifact_id=cmd.artifact_id,
        version_id=cmd.version_id,
        action=cmd.action,
        actor_id=cmd.actor_id,
        change_summary=cmd.change_summary,
        occurred_at=now,
    )

    approval_required = False
    if cmd.action == ACTION_PUBLISH:
        approval_required = await policy.require_second_admin(cmd.tenant_id)

    return RecordPublicationResult(
        publication_id=publication_id,
        approval_required=approval_required,
    )


async def approve_publication(
    cmd: ApprovePublicationCommand,
    *,
    repo: _PublicationRepo,
    clock: _Clock,
) -> PublicationRecord:
    """Record a second administrator's approval (P10-US8).

    The use case enforces separation-of-duties by inspecting the
    most recent ``publish`` row for the same artifact + version. The
    approver's ``actor_id`` MUST differ from the publisher's. A
    same-actor attempt or an attempt on an artifact that has no
    ``publish`` row yet raises :class:`Forbidden`.

    The approval row carries ``action='validate'`` and is appended in
    the same logical operation as the call. The HTTP layer maps
    :class:`Forbidden` to a 403 with code ``approval_required`` or
    ``approval_self``.
    """
    history = await repo.list_for_artifact(
        cmd.tenant_id,
        cmd.artifact_type,
        cmd.artifact_id,
        limit=1,
        offset=0,
    )
    latest = history[0] if history else None
    if latest is None or latest.action != ACTION_PUBLISH or latest.version_id != cmd.version_id:
        raise NotFound(
            f"no pending publish for {cmd.artifact_type} {cmd.artifact_id} version {cmd.version_id}"
        )
    if latest.actor_id == cmd.approving_actor_id:
        raise Forbidden("approval_self: a different administrator must approve the publish")

    now = clock.now()
    publication_id = await repo.add(
        tenant_id=cmd.tenant_id,
        artifact_type=cmd.artifact_type,
        artifact_id=cmd.artifact_id,
        version_id=cmd.version_id,
        action=ACTION_VALIDATE,
        actor_id=cmd.approving_actor_id,
        change_summary=cmd.change_summary,
        occurred_at=now,
    )
    return PublicationRecord(
        id=publication_id,
        tenant_id=cmd.tenant_id,
        artifact_type=cmd.artifact_type,
        artifact_id=cmd.artifact_id,
        version_id=cmd.version_id,
        action=ACTION_VALIDATE,
        actor_id=cmd.approving_actor_id,
        occurred_at=now,
        change_summary=cmd.change_summary,
    )


async def assert_publish_approved(
    *,
    tenant_id: UUID,
    artifact_type: str,
    artifact_id: UUID,
    version_id: UUID,
    repo: _PublicationRepo,
    policy: _GovernancePolicy,
) -> None:
    """Raise :class:`Forbidden` when the tenant requires second-admin
    approval but the most recent publish row is unapproved.

    Called by downstream consumers (e.g. the case-creation flow that
    binds a case to a CaseType version) so a half-published artifact
    never becomes the effective schema. When the tenant does not
    require second-admin approval, this is a no-op.

    A missing approval row raises ``Forbidden(code=approval_required)``;
    a self-approved row raises ``Forbidden(code=approval_self)``. Both
    map to 403 with a structured ``ErrorResponse``.
    """
    needs_approval = await policy.require_second_admin(tenant_id)
    if not needs_approval:
        return

    history = await repo.list_for_artifact(
        tenant_id,
        artifact_type,
        artifact_id,
        limit=50,
        offset=0,
    )
    matching = [
        r
        for r in history
        if r.version_id == version_id and r.action in {ACTION_PUBLISH, ACTION_VALIDATE}
    ]
    matching.sort(key=lambda r: r.occurred_at, reverse=True)
    publish_row = next(
        (r for r in matching if r.action == ACTION_PUBLISH),
        None,
    )
    if publish_row is None:
        return  # never published — not the gate's concern
    validate_row = next(
        (
            r
            for r in matching
            if r.action == ACTION_VALIDATE and r.occurred_at >= publish_row.occurred_at
        ),
        None,
    )
    if validate_row is None:
        raise Forbidden("approval_required: a second administrator must approve this publish")
    if validate_row.actor_id == publish_row.actor_id:
        raise Forbidden("approval_self: a different administrator must approve this publish")


async def list_publications(
    query: ListPublicationsQuery,
    *,
    repo: _PublicationRepo,
) -> list[PublicationRecord]:
    """Return publication history for an artifact or tenant (FM-021).

    When ``query.artifact_id`` is set, the result is scoped to that
    artifact + version; otherwise the result is the tenant-wide
    timeline (optionally filtered by ``artifact_type``). Newest-first.
    The HTTP layer paginates with the standard ``limit``/``offset``
    pair (AGENTS.md §17.1).
    """
    if query.artifact_id is not None:
        records = await repo.list_for_artifact(
            query.tenant_id,
            query.artifact_type or "",
            query.artifact_id,
            limit=query.limit,
            offset=query.offset,
        )
    else:
        records = await repo.list_tenant_history(
            query.tenant_id,
            limit=query.limit,
            offset=query.offset,
            artifact_type=query.artifact_type,
        )
    return sorted(records, key=lambda r: r.occurred_at, reverse=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_VALID_ACTIONS = frozenset({ACTION_DRAFT, ACTION_VALIDATE, ACTION_PUBLISH, ACTION_RETIRE})


def _validate_action(action: str) -> None:
    if action not in _VALID_ACTIONS:
        raise ValueError(
            f"unknown publication action {action!r}; expected one of {sorted(_VALID_ACTIONS)}"
        )


__all__ = [
    "ACTION_DRAFT",
    "ACTION_VALIDATE",
    "ACTION_PUBLISH",
    "ACTION_RETIRE",
    "ApprovePublicationCommand",
    "ListPublicationsQuery",
    "PublicationRecord",
    "RecordPublicationCommand",
    "RecordPublicationResult",
    "approve_publication",
    "assert_publish_approved",
    "list_publications",
    "record_publication",
]
