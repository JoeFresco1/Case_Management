"""Place and release a per-case legal hold (P08-US1, US2 / FM-039).

A legal hold is a security administrator's directive that prevents
disposition of a case, its documents, and its audit chain. Two use
cases live here:

- :func:`place_hold` — flip the case's ``legal_hold`` flag to
  ``True``, persist a fresh :class:`LegalHold` aggregate carrying
  the actor + reason + timestamp, and write a ``HoldPlaced`` audit
  row in the same transaction.
- :func:`release_hold` — find the active hold for the case, mark it
  released (actor + reason + timestamp), flip the case's
  ``legal_hold`` flag back to ``False``, and write a
  ``HoldReleased`` audit row.

Both operations require explicit authorization:

- ``place_hold`` requires ``Action.CASE_HOLD_PLACE``.
- ``release_hold`` requires ``Action.CASE_HOLD_RELEASE``.

Only the ``process_admin`` role carries these actions today (see
``app.application.authorization``). The deny-by-default posture means
an ordinary case worker — even one with ``CASE_UPDATE`` — cannot
place or release a hold.

# Refusal semantics

The refuse-to-dispose guard lives on the :class:`Case` aggregate
(``Case.change_status`` flips to terminal statuses raise
:class:`Conflict` when ``legal_hold`` is True). The application
layer is the only place that knows the *actor* placing the hold and
the *reason*; the aggregate guard is the storage-layer enforcement
that every code path inherits.

# Tenant isolation

Every repository call carries ``tenant_id`` first; the SQL adapter
must include ``WHERE tenant_id = :tenant_id`` on every query
(AGENTS.md §3 R1, §12). A cross-tenant case id surfaces as
``NotFound``; the use case never confirms cross-tenant existence
(Constitution V).

# INTEGRATION_SEAM

The :class:`LegalHoldRepository` port is the integration point for
the SQL adapter. This slice ships the in-memory test fake so the
seam is locatable, type-checkable, and the use case is fully
exercised; the SQL adapter is the next dispatch's deliverable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.audit import HOLD_PLACED, HOLD_RELEASED
from app.domain.case import Case
from app.domain.errors import NotFound
from app.domain.legal_hold import (
    HoldAlreadyPresent,
    HoldNotPresent,
    LegalHold,
)
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    LegalHoldRepository,
)

SUBJECT_TYPE_HOLD = "legal_hold"
SUBJECT_TYPE_CASE = "case"


class _ClockLike(Protocol):
    """Minimal clock protocol used by the use case.

    The application layer must not depend on FastAPI / ``app.deps``
    (Constitution VI). The duck-typed ``FixedClock`` test fake and
    any future adapter satisfy this shape.
    """

    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class PlaceHoldCommand:
    """Input to :func:`place_hold`.

    ``reason`` is required (a blank reason is rejected at the
    aggregate level so the audit row never carries a meaningless
    rationale).
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    reason: str
    correlation_id: str = ""
    causation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReleaseHoldCommand:
    """Input to :func:`release_hold`."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    reason: str
    correlation_id: str = ""
    causation_id: str | None = None


@dataclass(frozen=True, slots=True)
class HoldResult:
    """The post-commit outcome returned to the router.

    ``case`` is the case aggregate carrying the new ``legal_hold``
    flag value; ``hold`` is the persisted :class:`LegalHold` row the
    router projects onto the wire response.
    """

    case: Case
    hold: LegalHold


async def place_hold(
    cmd: PlaceHoldCommand,
    *,
    case_repo: CaseRepository,
    hold_repo: LegalHoldRepository,
    audit: AuditEventStore,
    clock: _ClockLike,
) -> HoldResult:
    """Place a legal hold on a case.

    Failure behavior:
    - Auth/validation/not-found errors are raised before any DB write.
    - ``HoldAlreadyPresent`` (409) when the case already has an
      active hold; the refusal is raised **before** any write so the
      audit row never carries a refused attempt.
    - The case ``UPDATE``, the ``legal_holds`` insert, and the
      ``HoldPlaced`` audit row share one transaction. A partial
      commit is impossible.
    """
    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.CASE_HOLD_PLACE,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind="case", id=cmd.case_id),
    )

    case = await case_repo.get(cmd.tenant_id, cmd.case_id)
    if case is None:
        raise NotFound(f"case {cmd.case_id} not found")

    # Tenant-scoped active-hold lookup. The use case refuses a second
    # placement up front so the audit row never carries a refused
    # attempt; the repository's unique-index is the
    # defense-in-depth layer (Constitution V).
    existing = await hold_repo.get_active(cmd.tenant_id, cmd.case_id)
    if existing is not None:
        raise HoldAlreadyPresent(cmd.case_id)

    now = clock.now()
    hold = LegalHold.place(
        tenant_id=cmd.tenant_id,
        case_id=cmd.case_id,
        placed_by=cmd.actor_id,
        reason=cmd.reason,
        now=now,
    )
    # Placing a hold is a case mutation: bump the version token so
    # concurrent PATCH / close attempts surface as 409 instead of
    # silently overwriting (Constitution V — no silent overwrites).
    held_case = replace(
        case,
        legal_hold=True,
        updated_at=now,
        version_token=case.version_token + 1,
    )

    await hold_repo.add(hold)
    await case_repo.update(held_case)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=HOLD_PLACED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_HOLD,
        subject_id=hold.id,
        case_id=cmd.case_id,
        correlation_id=cmd.correlation_id or _new_correlation_id(),
        causation_id=cmd.causation_id,
        payload={
            "case_id": str(cmd.case_id),
            "case_reference": case.reference.value,
            **hold.to_payload(),
        },
        occurred_at=now,
    )

    return HoldResult(case=held_case, hold=hold)


async def release_hold(
    cmd: ReleaseHoldCommand,
    *,
    case_repo: CaseRepository,
    hold_repo: LegalHoldRepository,
    audit: AuditEventStore,
    clock: _ClockLike,
) -> HoldResult:
    """Release the active legal hold on a case.

    Failure behavior:
    - Auth/not-found errors are raised before any DB write.
    - ``HoldNotPresent`` (409) when the case has no active hold.
    - The case ``UPDATE``, the ``legal_holds`` update, and the
      ``HoldReleased`` audit row share one transaction. A partial
      commit is impossible.
    """
    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.CASE_HOLD_RELEASE,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind="case", id=cmd.case_id),
    )

    case = await case_repo.get(cmd.tenant_id, cmd.case_id)
    if case is None:
        raise NotFound(f"case {cmd.case_id} not found")

    active = await hold_repo.get_active(cmd.tenant_id, cmd.case_id)
    if active is None:
        raise HoldNotPresent(cmd.case_id)

    now = clock.now()
    released = active.release(
        released_by=cmd.actor_id,
        reason=cmd.reason,
        now=now,
    )
    # Releasing a hold is a case mutation: bump the version token so
    # concurrent PATCH / close attempts surface as 409 instead of
    # silently overwriting (Constitution V — no silent overwrites).
    released_case = replace(
        case,
        legal_hold=False,
        updated_at=now,
        version_token=case.version_token + 1,
    )

    await hold_repo.update(released)
    await case_repo.update(released_case)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=HOLD_RELEASED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_HOLD,
        subject_id=released.id,
        case_id=cmd.case_id,
        correlation_id=cmd.correlation_id or _new_correlation_id(),
        causation_id=cmd.causation_id,
        payload={
            "case_id": str(cmd.case_id),
            "case_reference": case.reference.value,
            **released.to_payload(),
        },
        occurred_at=now,
    )

    return HoldResult(case=released_case, hold=released)


# ----- helpers --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy (see create_case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


def _new_correlation_id() -> str:
    """Generate a sortable correlation id.

    The published PyPI package name is ``uuid7`` but its module is
    ``uuid_extensions`` (the PyPI name was a placeholder during early
    publishing). The lazy import keeps cold-start light and survives
    a missing package.
    """
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())


__all__ = [
    "HoldResult",
    "PlaceHoldCommand",
    "ReleaseHoldCommand",
    "SUBJECT_TYPE_CASE",
    "SUBJECT_TYPE_HOLD",
    "place_hold",
    "release_hold",
]
