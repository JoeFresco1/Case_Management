"""Read helpers consumed by the disposition approval use case + HTTP surface.

- :func:`list_active_freezes` returns the active freeze snapshot for
  the current moment (records-admin freeze console + the disposition
  approval use case's pre-check).
- :func:`is_disposition_blocked` folds both cross-aggregate blockers
  — an active disposal freeze OR an active case archival — into one
  call, so the approval use case stays simple and the policy is
  encoded once instead of leaking into every caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from app.domain.ports import CaseArchivalRepository, DisposalFreezeRepository
from app.domain.retention import DisposalFreeze, is_freeze_blocking

from .common import _ClockLike


@dataclass(frozen=True, slots=True)
class ActiveFreezeSnapshot:
    """A read-only view of the active freeze set at ``at``.

    The disposition approval use case consults this snapshot
    before recording the approval. The use case stays one
    function call away from the answer and never imports the
    repository directly.
    """

    freezes: list[DisposalFreeze] = field(default_factory=list)

    def is_blocking(self) -> bool:
        """Return True iff at least one freeze blocks the snapshot moment."""
        return bool(self.freezes)


async def list_active_freezes(
    *,
    tenant_id: UUID,
    freeze_repo: DisposalFreezeRepository,
    clock: _ClockLike,
) -> ActiveFreezeSnapshot:
    """Return the active freeze snapshot for the current moment.

    Exposed for the HTTP surface (records-admin freeze console)
    AND consumed by the disposition approval use case before
    recording an approval. The function is a thin pass-through
    to the repository; the snapshot wrapper lets the call
    site use a single, typesafe name.
    """
    now = clock.now()
    freezes = await freeze_repo.list_active(tenant_id, at=now)
    return ActiveFreezeSnapshot(freezes=freezes)


async def is_disposition_blocked(
    *,
    tenant_id: UUID,
    freeze_repo: DisposalFreezeRepository,
    case_archival_repo: CaseArchivalRepository,
    case_id: UUID,
    clock: _ClockLike,
) -> bool:
    """Return True iff a freeze OR an active archive blocks the case.

    The disposition approval use case calls this helper before
    recording the approval. Both blockers are checked in one
    call so the use case stays simple; the cross-aggregate
    policy is encoded once, here, instead of leaking into
    every caller.
    """
    now = clock.now()
    freezes = await freeze_repo.list_active(tenant_id, at=now)
    if is_freeze_blocking(freezes=freezes, at=now):
        return True
    archival = await case_archival_repo.get_active_for_case(tenant_id, case_id)
    return archival is not None


__all__ = [
    "ActiveFreezeSnapshot",
    "is_disposition_blocked",
    "list_active_freezes",
]
