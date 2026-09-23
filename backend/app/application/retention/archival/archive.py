"""Case archive + unfreeze use cases (P08-US5 / FM-041).

:func:`archive_case` moves a case to a cooler storage tier and
freezes it — the application layer refuses disposition while an
active :class:`CaseArchival` row exists (the archive use case is
the writer; the disposition approval use case is the reader).
:func:`unfreeze_case` lifts the freeze and vacates the
``(tenant_id, case_id)`` active slot; the historical archival row
stays in the audit chain.

Both operations pair the domain change with its audit row in the
caller's DB transaction — a partial commit is impossible
(Constitution III).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.audit import CASE_ARCHIVED, CASE_UNFROZEN
from app.domain.errors import NotFound
from app.domain.ports import (
    AuditEventStore,
    CaseArchivalRepository,
    CaseRepository,
)
from app.domain.retention import ArchivalTier, CaseArchival

from .common import (
    SUBJECT_TYPE_ARCHIVAL,
    SUBJECT_TYPE_CASE,
    _ActorProxy,
    _ClockLike,
    _new_correlation_id,
)


@dataclass(frozen=True, slots=True)
class ArchiveCaseCommand:
    """Input to :func:`archive_case`.

    ``tier`` is the destination tier (``WARM`` for v1; ``HOT``
    is the default mutable tier and the aggregate refuses it as
    a safety net). ``reason`` is required and audited.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    tier: ArchivalTier
    reason: str
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class ArchiveCaseResult:
    """The post-archive outcome returned to the router.

    ``archival`` is the freshly created :class:`CaseArchival`
    row the router projects onto the wire response.
    """

    archival: CaseArchival


async def archive_case(
    cmd: ArchiveCaseCommand,
    *,
    case_repo: CaseRepository,
    archival_repo: CaseArchivalRepository,
    audit: AuditEventStore,
    clock: _ClockLike,
) -> ArchiveCaseResult:
    """Move a case to a cooler storage tier (P08-US5 / FM-041).

    Failure behavior:

    - Auth/not-found errors are raised before any DB write.
    - :class:`NotFound` when the case is missing or
      cross-tenant.
    - :class:`Conflict` when the case is already under an
      active archival row (the application layer refuses a
      double-archive; the in-memory adapter raises the same
      ``Conflict``).
    - The archival row and the ``CaseArchived`` audit row
      share one transaction.
    """
    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.RETENTION_ARCHIVE,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind=SUBJECT_TYPE_CASE, id=cmd.case_id),
    )

    case = await case_repo.get(cmd.tenant_id, cmd.case_id)
    if case is None:
        raise NotFound(f"case {cmd.case_id} not found")

    now = clock.now()
    archival = CaseArchival.archive(
        tenant_id=cmd.tenant_id,
        case_id=case.id,
        tier=cmd.tier,
        actor_id=cmd.actor_id,
        at=now,
        reason=cmd.reason,
    )
    await archival_repo.add(archival)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=CASE_ARCHIVED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_ARCHIVAL,
        subject_id=archival.id,
        case_id=case.id,
        correlation_id=cmd.correlation_id or _new_correlation_id(),
        causation_id=None,
        payload={
            "case_reference": case.reference.value,
            **archival.to_payload(),
        },
        occurred_at=now,
    )

    return ArchiveCaseResult(archival=archival)


@dataclass(frozen=True, slots=True)
class UnfreezeCaseCommand:
    """Input to :func:`unfreeze_case`.

    ``reason`` is required and audited (the operation is the
    inverse of :func:`archive_case` and the same evidence
    standard applies).
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    reason: str
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class UnfreezeCaseResult:
    """The post-unfreeze outcome returned to the router.

    ``archival`` is the updated :class:`CaseArchival` row
    (status ``unfrozen``).
    """

    archival: CaseArchival


async def unfreeze_case(
    cmd: UnfreezeCaseCommand,
    *,
    case_repo: CaseRepository,
    archival_repo: CaseArchivalRepository,
    audit: AuditEventStore,
    clock: _ClockLike,
) -> UnfreezeCaseResult:
    """Lift the freeze on an archived case (P08-US5 / FM-041).

    The historical archival row stays in the audit chain; the
    ``(tenant_id, case_id)`` active slot is vacated so a future
    :func:`archive_case` rebinds it.

    Failure behavior:

    - Auth/not-found errors are raised before any DB write.
    - :class:`NotFound` when the case is missing or
      cross-tenant.
    - :class:`NotFound` when the case has no active archival
      row (a release against a non-archived case is a process
      error; the use case refuses it).
    - The archival update and the ``CaseUnfrozen`` audit row
      share one transaction.
    """
    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.RETENTION_UNFREEZE,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind=SUBJECT_TYPE_CASE, id=cmd.case_id),
    )

    case = await case_repo.get(cmd.tenant_id, cmd.case_id)
    if case is None:
        raise NotFound(f"case {cmd.case_id} not found")

    archival = await archival_repo.get_active_for_case(cmd.tenant_id, case.id)
    if archival is None:
        raise NotFound(f"case {case.reference.value} is not currently archived")

    now = clock.now()
    updated = archival.unfreeze(
        actor_id=cmd.actor_id,
        at=now,
        reason=cmd.reason,
    )
    await archival_repo.update(updated)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=CASE_UNFROZEN,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_ARCHIVAL,
        subject_id=updated.id,
        case_id=case.id,
        correlation_id=cmd.correlation_id or _new_correlation_id(),
        causation_id=None,
        payload={
            "case_reference": case.reference.value,
            **updated.to_payload(),
        },
        occurred_at=now,
    )

    return UnfreezeCaseResult(archival=updated)


__all__ = [
    "ArchiveCaseCommand",
    "ArchiveCaseResult",
    "UnfreezeCaseCommand",
    "UnfreezeCaseResult",
    "archive_case",
    "unfreeze_case",
]
