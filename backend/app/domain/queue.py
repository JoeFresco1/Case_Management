"""Queue aggregate (P02-US3, US9 / FM-016).

A ``Queue`` is a tenant-scoped named work container the workflow
engine uses as an ``assignee`` on :class:`app.domain.task.Task`. v1
supports two ``assignment_policy`` values:

- ``manual`` — a worker must explicitly claim a task from the queue.
  The round-robin cursor (``rr_cursor``) is meaningless and is
  pinned at ``0``.
- ``round_robin`` — every auto-assigned task (via
  :func:`app.application.queues.manage_queues.claim_next`) is
  handed to the next member in declared order, wrapping around at
  the end. The cursor is advanced on each claim so successive calls
  cycle deterministically.

# Lifecycle

The aggregate is immutable: every status or membership change
(``configure_policy``, ``set_members``, ``rename``, ``advance_cursor``)
returns a new instance with an incremented ``version_token`` so a
concurrent edit is refused with ``Conflict`` (Constitution V). The
repository persists the new shape and enforces the token check on
the way down.

# Keys vs names (AGENTS.md §16.2)

``key`` is the stable identifier the workflow engine references
(``actions[].queue``); ``name`` is the human label and may rename
across versions without breaking history. A non-empty ``key`` is
validated against a conservative slug grammar (``[a-z0-9_-]{1,64}``)
so the validator does not have to reject garbage at publish time.

# Why this lives in domain, not application

The aggregate enforces invariants the validator and the use cases
all rely on (non-empty key, member list contains only the tenant's
user ids when known, round-robin cursor bounds). The application
layer coordinates with repositories and audit; the domain owns the
shape and the rules.

# Membership, visibility, aging, and delegation (FM-016 / P02-US3, US9)
#
# - ``VisibilityScope`` controls who sees a queue. ``TENANT`` (default)
#   is visible to every user in the tenant; ``MEMBERS`` restricts the
#   worker listing to ``member_user_ids`` (tenant admins always see
#   every queue). The scope is a domain field with a ``TENANT``
#   default; the SQL adapter persists it once the B-5 migration adds
#   the column (see ``app.infrastructure.persistence.mapping``) — the
#   in-memory fakes carry it today.
# - ``task_aging_context`` is the pure due-date / aging readout the
#   worker queue shows per task (age since creation, overdue flag and
#   lateness, time-to-due). It takes ``now`` explicitly so tests are
#   deterministic and the engine never smuggles in wall-clock reads.
# - ``Delegation`` is the durable record of a time-bounded handoff
#   (P02-US9): who handed what to whom until when. The application
#   layer's ``expire_delegations`` replays these records and restores
#   the prior assignee once ``now`` passes ``until``.
#
# # INTEGRATION_SEAM: QueueRepository
#
# ``app.domain.ports.QueueRepository`` (Protocol) is the persistence
# port. The SQL adapter lands in T066-T077; the application use case
# talks to the Protocol only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation

_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class AssignmentPolicy(StrEnum):
    """How a queue distributes work to its members (P02-US3 / FM-016)."""

    MANUAL = "manual"
    ROUND_ROBIN = "round_robin"


class VisibilityScope(StrEnum):
    """Who may see a queue in the worker listing (P02-US3 / FM-016).

    ``TENANT`` (default) lists the queue to every user in the tenant;
    ``MEMBERS`` lists it only to ``member_user_ids``. Tenant admins
    always see every queue regardless of scope (Constitution V —
    administration is never hidden from admins).
    """

    TENANT = "tenant"
    MEMBERS = "members"


@dataclass(frozen=True, slots=True)
class Queue:
    """A tenant-scoped named work container with an assignment policy.

    ``member_user_ids`` is the ordered set of users that may work the
    queue. Under ``manual`` it is informational (a UI list); under
    ``round_robin`` it is the cycling order. ``rr_cursor`` is the
    index into ``member_user_ids`` that the next auto-assignment
    targets; it advances on every claim and wraps modulo length.

    ``capacity`` is the soft cap on open tasks the queue will accept;
    the workflow engine rejects a transition that targets a saturated
    queue (out of scope for the first slice, but the column lives
    here so the v2 rule has no migration). ``0`` means "no cap".

    ``version_token`` is bumped on every mutation so a concurrent
    editor's write is refused with ``Conflict`` (Constitution V).
    """

    id: UUID
    tenant_id: UUID
    key: str
    name: str
    description: str = ""
    assignment_policy: AssignmentPolicy = AssignmentPolicy.MANUAL
    capacity: int = 0
    member_user_ids: tuple[UUID, ...] = field(default_factory=tuple)
    rr_cursor: int = 0
    # Why default TENANT: every queue created before visibility scopes
    # existed (and every row read from SQL before the B-5 column lands)
    # behaves as tenant-visible, so the scope can only narrow, never
    # widen, existing access (fail-closed, Constitution V).
    visibility_scope: VisibilityScope = VisibilityScope.TENANT
    created_at: object | None = None
    updated_at: object | None = None
    version_token: int = 1

    # ----- factories --------------------------------------------------------

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        key: str,
        name: str,
        description: str = "",
        assignment_policy: AssignmentPolicy = AssignmentPolicy.MANUAL,
        capacity: int = 0,
        member_user_ids: tuple[UUID, ...] = (),
        visibility_scope: VisibilityScope = VisibilityScope.TENANT,
        created_at: object | None = None,
        updated_at: object | None = None,
    ) -> Queue:
        """Construct a new queue.

        Enforces the invariants every consumer relies on:

        - ``key`` matches the slug grammar (lowercase alnum + ``_`` /
          ``-``, 1..64 chars). Workflow authors reference queues by
          key, so a permissive grammar would let typos survive.
        - ``name`` is non-empty after stripping.
        - ``capacity`` is non-negative. ``0`` is the "no cap" sentinel.
        - ``member_user_ids`` contains no duplicates; the order is
          preserved exactly because round-robin depends on it.

        Raises :class:`InvariantViolation` on any violation.
        """
        if tenant_id is None:
            raise InvariantViolation("tenant_id is required")
        if not isinstance(key, str) or not _KEY_PATTERN.match(key):
            raise InvariantViolation("queue key must match [a-z0-9][a-z0-9_-]{0,63}")
        if not name or not name.strip():
            raise InvariantViolation("queue name is required")
        if capacity < 0:
            raise InvariantViolation("queue capacity must be >= 0")
        if assignment_policy is AssignmentPolicy.ROUND_ROBIN and not member_user_ids:
            raise InvariantViolation("round_robin queue requires at least one member")
        if not isinstance(visibility_scope, VisibilityScope):
            raise InvariantViolation("queue visibility_scope must be a VisibilityScope")
        deduped: list[UUID] = []
        seen: set[UUID] = set()
        for uid in member_user_ids:
            if uid in seen:
                continue
            seen.add(uid)
            deduped.append(uid)
        return Queue(
            id=uuid4(),
            tenant_id=tenant_id,
            key=key.strip(),
            name=name.strip(),
            description=description or "",
            assignment_policy=assignment_policy,
            capacity=capacity,
            member_user_ids=tuple(deduped),
            rr_cursor=0,
            visibility_scope=visibility_scope,
            created_at=created_at,
            updated_at=updated_at,
            version_token=1,
        )

    # ----- transitions ------------------------------------------------------

    def rename(self, *, name: str) -> Queue:
        """Update the human-facing label (P02-US3 / FM-016).

        ``key`` is the stable identifier; ``name`` may rename freely.
        Bumps ``version_token`` for optimistic concurrency.
        """
        if not name or not name.strip():
            raise InvariantViolation("queue name is required")
        return replace(
            self,
            name=name.strip(),
            version_token=self.version_token + 1,
        )

    def set_description(self, *, description: str) -> Queue:
        """Update the free-form description.

        Idempotent — calling with the same value still bumps the
        token so the caller gets the optimistic-concurrency signal
        that the write persisted.
        """
        return replace(
            self,
            description=description or "",
            version_token=self.version_token + 1,
        )

    def configure_policy(
        self,
        *,
        assignment_policy: AssignmentPolicy,
        capacity: int | None = None,
    ) -> Queue:
        """Change the assignment policy and / or capacity.

        Switching to ``ROUND_ROBIN`` requires at least one member;
        switching back to ``MANUAL`` keeps the membership list
        intact so a later policy flip back to ``ROUND_ROBIN`` does
        not lose context.

        ``capacity`` ``None`` leaves the current cap unchanged;
        ``0`` clears the cap.
        """
        if capacity is not None and capacity < 0:
            raise InvariantViolation("queue capacity must be >= 0")
        if assignment_policy is AssignmentPolicy.ROUND_ROBIN and not self.member_user_ids:
            raise InvariantViolation("round_robin queue requires at least one member")
        return replace(
            self,
            assignment_policy=assignment_policy,
            capacity=capacity if capacity is not None else self.capacity,
            rr_cursor=0 if assignment_policy is AssignmentPolicy.MANUAL else self.rr_cursor,
            version_token=self.version_token + 1,
        )

    def set_members(self, *, member_user_ids: tuple[UUID, ...]) -> Queue:
        """Replace the member list.

        Order is significant under ``ROUND_ROBIN`` — successive
        claims cycle through this list. The cursor resets to ``0``
        so the first auto-claim hands to the freshly declared head.
        Duplicates are removed (order preserved) so the cursor
        cannot land on a phantom index.

        Switching back to ``ROUND_ROBIN`` with zero members is a
        :class:`InvariantViolation`; call :meth:`configure_policy`
        first to switch to ``MANUAL``.
        """
        deduped: list[UUID] = []
        seen: set[UUID] = set()
        for uid in member_user_ids:
            if uid in seen:
                continue
            seen.add(uid)
            deduped.append(uid)
        if self.assignment_policy is AssignmentPolicy.ROUND_ROBIN and not deduped:
            raise InvariantViolation("round_robin queue requires at least one member")
        return replace(
            self,
            member_user_ids=tuple(deduped),
            rr_cursor=0,
            version_token=self.version_token + 1,
        )

    def set_visibility_scope(self, *, visibility_scope: VisibilityScope) -> Queue:
        """Narrow or widen who sees the queue in the worker listing.

        Widening ``MEMBERS`` -> ``TENANT`` is allowed (the admin
        explicitly opens the queue); narrowing is the fail-closed
        default. Bumps ``version_token`` for optimistic concurrency.
        """
        if not isinstance(visibility_scope, VisibilityScope):
            raise InvariantViolation("queue visibility_scope must be a VisibilityScope")
        return replace(
            self,
            visibility_scope=visibility_scope,
            version_token=self.version_token + 1,
        )

    def is_visible_to(self, user_id: UUID, *, is_admin: bool = False) -> bool:
        """Return whether ``user_id`` may see this queue listed.

        Tenant admins always see every queue. A ``TENANT`` queue is
        visible to every tenant user; a ``MEMBERS`` queue is visible
        only to its ``member_user_ids``. Pure read — does not mutate.
        """
        if is_admin:
            return True
        if self.visibility_scope is VisibilityScope.TENANT:
            return True
        return user_id in self.member_user_ids

    def advance_cursor(self) -> Queue:
        """Advance the round-robin cursor and return the next assignee.

        Returns a **new** aggregate with the cursor at the next
        member's index. The caller is responsible for persisting
        the returned shape (the repository's ``update`` enforces the
        token check so a concurrent advance cannot be silently
        overwritten — Constitution V).

        Under ``MANUAL`` the cursor is meaningless; the call still
        bumps the token so any caller that observes a race gets a
        clean ``Conflict`` rather than a silent no-op.
        """
        if self.assignment_policy is AssignmentPolicy.MANUAL:
            return replace(self, version_token=self.version_token + 1)
        if not self.member_user_ids:
            raise InvariantViolation("round_robin queue has no members; configure members first")
        next_index = (self.rr_cursor + 1) % len(self.member_user_ids)
        return replace(
            self,
            rr_cursor=next_index,
            version_token=self.version_token + 1,
        )

    # ----- queries ----------------------------------------------------------

    def next_assignee(self) -> UUID | None:
        """Return the user the next auto-claim would target.

        Returns ``None`` under ``MANUAL`` (the worker claims by hand)
        or when the queue has no members. Pure read — does not mutate.
        """
        if self.assignment_policy is AssignmentPolicy.MANUAL:
            return None
        if not self.member_user_ids:
            return None
        return self.member_user_ids[self.rr_cursor % len(self.member_user_ids)]


def visible_queues_for(
    queues: Iterable[Queue],
    user_id: UUID,
    *,
    is_admin: bool = False,
) -> tuple[Queue, ...]:
    """Filter ``queues`` to those ``user_id`` may see listed (FM-016).

    Thin wrapper over :meth:`Queue.is_visible_to` so the application
    layer never re-implements the scope rule (and so a future scope
    value lands in one place). Preserves input order.
    """
    return tuple(q for q in queues if q.is_visible_to(user_id, is_admin=is_admin))


class _AgedTask(Protocol):
    """Minimal task shape the aging readout needs (FM-016).

    Why a Protocol: both the real ``Task`` aggregate and the
    application-layer fakes carry ``due_at`` / ``created_at``; the
    helper must not import either (domain has no application
    dependency, and importing ``Task`` here would couple queue
    rendering to the task module).
    """

    due_at: datetime | None
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class TaskAging:
    """Due-date / aging readout for one queue task (P02-US3 / FM-016).

    ``age_seconds`` floors at ``0`` when ``created_at`` is missing or
    in the future (clock skew must never render a negative age).
    ``is_overdue`` is true only when a due date exists and ``now`` is
    past it; ``overdue_by_seconds`` / ``due_in_seconds`` are mutually
    exclusive (exactly one is set when ``due_at`` exists).
    """

    age_seconds: float
    is_overdue: bool
    overdue_by_seconds: float | None
    due_in_seconds: float | None


def task_aging_context(task: _AgedTask, *, now: datetime) -> TaskAging:
    """Compute the worker-facing aging context for ``task`` at ``now``.

    Pure function — no I/O, no wall-clock read. Takes ``now``
    explicitly so the queue listing and the tests share one clock.
    """
    if task.created_at is None:
        age_seconds = 0.0
    else:
        age_seconds = max(0.0, (now - task.created_at).total_seconds())
    if task.due_at is None:
        return TaskAging(
            age_seconds=age_seconds,
            is_overdue=False,
            overdue_by_seconds=None,
            due_in_seconds=None,
        )
    delta = (now - task.due_at).total_seconds()
    if delta > 0:
        return TaskAging(
            age_seconds=age_seconds,
            is_overdue=True,
            overdue_by_seconds=delta,
            due_in_seconds=None,
        )
    return TaskAging(
        age_seconds=age_seconds,
        is_overdue=False,
        overdue_by_seconds=None,
        due_in_seconds=-delta,
    )


@dataclass(frozen=True, slots=True)
class Delegation:
    """Durable record of a time-bounded task handoff (P02-US9 / FM-016).

    ``from_user_id`` is the worker who owned the task (and to whom it
    returns); ``to_user_id`` is the delegate (e.g. PTO cover).
    ``until`` is the exclusive bound: the delegate holds the task
    while ``now <= until``; once ``now > until`` the application's
    ``expire_delegations`` restores ``from_user_id``. The record is
    immutable — expiry deletes it, never mutates it.
    """

    id: UUID
    tenant_id: UUID
    task_id: UUID
    case_id: UUID
    from_user_id: UUID
    to_user_id: UUID
    delegated_at: datetime
    until: datetime

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        task_id: UUID,
        case_id: UUID,
        from_user_id: UUID,
        to_user_id: UUID,
        delegated_at: datetime,
        until: datetime,
    ) -> Delegation:
        """Construct a delegation record.

        Enforces the invariants expiry depends on: the window must
        extend into the future (``until > delegated_at`` — a
        zero-length handoff would expire before it is read) and the
        delegate must differ from the owner (self-delegation hides
        the true owner from the timeline).
        """
        if tenant_id is None or task_id is None or case_id is None:
            raise InvariantViolation("tenant_id, task_id and case_id are required")
        if from_user_id is None or to_user_id is None:
            raise InvariantViolation("from_user_id and to_user_id are required")
        if from_user_id == to_user_id:
            raise InvariantViolation("cannot delegate to self")
        if until <= delegated_at:
            raise InvariantViolation("delegation window must extend into the future")
        return Delegation(
            id=uuid4(),
            tenant_id=tenant_id,
            task_id=task_id,
            case_id=case_id,
            from_user_id=from_user_id,
            to_user_id=to_user_id,
            delegated_at=delegated_at,
            until=until,
        )

    def is_expired(self, *, now: datetime) -> bool:
        """Return whether the delegation window has passed at ``now``.

        The bound is exclusive (``now > until``), matching the
        ``DelegateCommand`` contract: the delegate holds the task
        through ``until`` itself.
        """
        return now > self.until


__all__ = [
    "AssignmentPolicy",
    "Delegation",
    "Queue",
    "TaskAging",
    "VisibilityScope",
    "task_aging_context",
    "visible_queues_for",
]
