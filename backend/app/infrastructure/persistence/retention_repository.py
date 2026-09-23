"""In-memory ``RetentionPolicyRepository`` and
``DispositionRequestRepository`` for the FM-040 H1 slice.

These adapters follow the same seam-placeholder pattern as
``InMemoryLegalHoldRepository``: the job is to be runtime-correct so
the use case + router are exercisable end-to-end against synthetic
tenants without the SQL adapter being on the critical path. The
production seam is the SQL adapter that lands with a follow-up
dispatch.

# INTEGRATION_SEAM: retention SQL adapters

Both adapters are **seam placeholders** (AGENTS.md §12). The
production implementation will live behind the same Protocol and the
in-memory adapter is reserved for tests.

# Tenant isolation

Every method scopes by ``tenant_id`` so a cross-tenant id surfaces
as ``None`` / empty sequences; the API never confirms cross-tenant
existence (Constitution V).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from threading import RLock
from uuid import UUID

from app.domain.errors import Conflict
from app.domain.retention import (
    CaseArchival,
    DisposalFreeze,
    DispositionRequest,
    OrphanCaseProposal,
    RetentionPolicy,
)


class InMemoryRetentionPolicyRepository:
    """Process-scoped ``RetentionPolicyRepository`` (FM-040 / P08-US3).

    Policies live in an in-process dictionary keyed by id; the
    per-target version counter is derived from the highest stored
    version number for ``(tenant_id, category, target_key)``. The
    lock serializes the ``next_version_number`` -> ``add`` critical
    section so a concurrent publish cannot stage two policies at the
    same version number.
    """

    def __init__(self) -> None:
        self._by_id: dict[UUID, RetentionPolicy] = {}
        self._lock = RLock()

    async def add(self, policy: RetentionPolicy) -> None:
        with self._lock:
            key = (policy.tenant_id, policy.category.value, policy.target_key)
            for existing in self._by_id.values():
                if (
                    existing.tenant_id == policy.tenant_id
                    and existing.category == policy.category
                    and existing.target_key == policy.target_key
                    and existing.version_number == policy.version_number
                ):
                    raise Conflict(
                        f"retention policy version {policy.version_number} already exists"
                    )
            _ = key
            self._by_id[policy.id] = policy

    async def get(self, tenant_id: UUID, policy_id: UUID) -> RetentionPolicy | None:
        with self._lock:
            policy = self._by_id.get(policy_id)
            if policy is None or policy.tenant_id != tenant_id:
                return None
            return policy

    async def next_version_number(self, tenant_id: UUID, *, category: str, target_key: str) -> int:
        with self._lock:
            highest = 0
            for policy in self._by_id.values():
                if (
                    policy.tenant_id == tenant_id
                    and policy.category.value == category
                    and policy.target_key == target_key
                ):
                    highest = max(highest, policy.version_number)
            return highest + 1

    async def list_published(
        self, tenant_id: UUID, *, category: str | None = None
    ) -> list[RetentionPolicy]:
        with self._lock:
            rows = [
                p
                for p in self._by_id.values()
                if p.tenant_id == tenant_id
                and p.status.value == "published"
                and (category is None or p.category.value == category)
            ]
            rows.sort(
                key=lambda p: p.published_at or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )
            return list(rows)


class InMemoryDispositionRequestRepository:
    """Process-scoped ``DispositionRequestRepository`` (FM-040 / P08-US4).

    Requests live in an in-process dictionary keyed by id; a per-case
    pending lookup keeps the ``UNIQUE(tenant_id, case_id) WHERE status
    = 'pending'`` invariant (the application-layer pre-check is
    authoritative; the dictionary is the defense-in-depth layer).
    """

    def __init__(self) -> None:
        self._by_id: dict[UUID, DispositionRequest] = {}
        # Indexed for O(1) pending-per-case lookup.
        self._pending_by_case: dict[tuple[UUID, UUID], DispositionRequest] = {}
        self._lock = RLock()

    async def add(self, request: DispositionRequest) -> None:
        with self._lock:
            case_key = (request.tenant_id, request.case_id)
            existing = self._pending_by_case.get(case_key)
            if existing is not None and existing.status.value == "pending":
                raise Conflict(f"case {request.case_id} already has a pending disposition request")
            self._by_id[request.id] = request
            if request.status.value == "pending":
                self._pending_by_case[case_key] = request

    async def get(self, tenant_id: UUID, request_id: UUID) -> DispositionRequest | None:
        with self._lock:
            request = self._by_id.get(request_id)
            if request is None or request.tenant_id != tenant_id:
                return None
            return request

    async def get_pending_for_case(
        self, tenant_id: UUID, case_id: UUID
    ) -> DispositionRequest | None:
        with self._lock:
            request = self._pending_by_case.get((tenant_id, case_id))
            if request is None:
                return None
            if request.status.value != "pending":
                return None
            return request

    async def list_eligible(
        self, tenant_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> list[DispositionRequest]:
        with self._lock:
            rows = [
                r
                for r in self._by_id.values()
                if r.tenant_id == tenant_id and r.status.value == "pending"
            ]
            rows.sort(key=lambda r: r.surfaced_at, reverse=True)
            return rows[offset : offset + limit]

    async def list_for_case(self, tenant_id: UUID, case_id: UUID) -> list[DispositionRequest]:
        """Return every disposition request (any status) for ``case_id``.

        Used by the defensible-deletion evidence report (FM-042 / P08-US6)
        to assemble the full chain of custody — a case can carry
        multiple historical requests (e.g. a rejected one and a later
        approved one) and the report must surface them all in
        chronological order so the auditor can replay the full
        decision history. Tenant-scoped so a cross-tenant case id
        returns an empty list (Constitution V — never confirm
        cross-tenant existence).
        """
        with self._lock:
            rows = [
                r for r in self._by_id.values() if r.tenant_id == tenant_id and r.case_id == case_id
            ]
            rows.sort(key=lambda r: r.surfaced_at)
            return list(rows)

    async def update(self, request: DispositionRequest) -> None:
        with self._lock:
            stored = self._by_id.get(request.id)
            if stored is None:
                raise Conflict(f"disposition request {request.id} not found")
            if stored.tenant_id != request.tenant_id:
                raise Conflict(f"disposition request {request.id} tenant mismatch")
            if stored.status.value != "pending":
                raise Conflict(f"disposition request {request.id} is already decided")
            self._by_id[request.id] = request
            case_key = (request.tenant_id, request.case_id)
            # Once decided, the pending slot is vacated so a future
            # eligibility pass can surface a fresh request.
            if request.status.value != "pending":
                self._pending_by_case.pop(case_key, None)


# ---------------------------------------------------------------------------
# Archival + freeze + orphan in-memory adapters (P08-US5, US7, US8 / FM-041).
# ---------------------------------------------------------------------------
#
# The same seam-placeholder pattern as the disposition adapter: a
# process-scoped in-memory store that exercises the same
# tenant-isolation + uniqueness invariants the SQL adapter will
# rely on. The application layer depends on the Protocol in
# ``app.domain.ports``; the router wires this in-memory adapter via
# ``app.state`` so a future SQL swap is a one-line change.
# ---------------------------------------------------------------------------


class InMemoryCaseArchivalRepository:
    """Process-scoped ``CaseArchivalRepository`` (FM-041 / P08-US5).

    The per-case active slot is keyed on
    ``(tenant_id, case_id)`` and holds the most recent
    ``archived`` row. ``unfreeze`` keeps the historical row in
    ``_by_id`` but vacates the active slot so a future
    :meth:`archive` rebinds it.
    """

    def __init__(self) -> None:
        self._by_id: dict[UUID, CaseArchival] = {}
        self._active_by_case: dict[tuple[UUID, UUID], CaseArchival] = {}
        self._lock = RLock()

    async def add(self, archival: CaseArchival) -> None:
        with self._lock:
            case_key = (archival.tenant_id, archival.case_id)
            existing = self._active_by_case.get(case_key)
            if existing is not None and existing.is_frozen:
                raise Conflict(
                    f"case {archival.case_id} is already archived; unfreeze before re-archiving"
                )
            self._by_id[archival.id] = archival
            if archival.is_frozen:
                self._active_by_case[case_key] = archival

    async def get(self, tenant_id: UUID, archival_id: UUID) -> CaseArchival | None:
        with self._lock:
            row = self._by_id.get(archival_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return row

    async def get_active_for_case(self, tenant_id: UUID, case_id: UUID) -> CaseArchival | None:
        with self._lock:
            row = self._active_by_case.get((tenant_id, case_id))
            if row is None or not row.is_frozen:
                return None
            return row

    async def update(self, archival: CaseArchival) -> None:
        with self._lock:
            stored = self._by_id.get(archival.id)
            if stored is None:
                raise Conflict(f"archival {archival.id} not found")
            if stored.tenant_id != archival.tenant_id:
                raise Conflict(f"archival {archival.id} tenant mismatch")
            if not stored.is_frozen:
                raise Conflict(f"archival {archival.id} is already unfrozen")
            self._by_id[archival.id] = archival
            case_key = (archival.tenant_id, archival.case_id)
            if not archival.is_frozen:
                self._active_by_case.pop(case_key, None)


class InMemoryDisposalFreezeRepository:
    """Process-scoped ``DisposalFreezeRepository`` (FM-041 / P08-US7).

    Freezes live per tenant; the active set is derived on every
    ``list_active`` call so a cancelled / naturally-expired row
    immediately drops out. The lock serializes
    ``list_active`` + ``add`` / ``update`` so a concurrent
    cancellation cannot race past the active read.
    """

    def __init__(self) -> None:
        self._by_id: dict[UUID, DisposalFreeze] = {}
        self._lock = RLock()

    async def add(self, freeze: DisposalFreeze) -> None:
        with self._lock:
            self._by_id[freeze.id] = freeze

    async def get(self, tenant_id: UUID, freeze_id: UUID) -> DisposalFreeze | None:
        with self._lock:
            row = self._by_id.get(freeze_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return row

    async def list_active(self, tenant_id: UUID, *, at: datetime) -> list[DisposalFreeze]:
        with self._lock:
            rows = [
                f
                for f in self._by_id.values()
                if f.tenant_id == tenant_id and f.is_active_at(at=at)
            ]
            rows.sort(key=lambda f: f.starts_at)
            return list(rows)

    async def list_all(self, tenant_id: UUID) -> list[DisposalFreeze]:
        with self._lock:
            rows = [f for f in self._by_id.values() if f.tenant_id == tenant_id]
            rows.sort(key=lambda f: f.starts_at, reverse=True)
            return list(rows)

    async def update(self, freeze: DisposalFreeze) -> None:
        with self._lock:
            stored = self._by_id.get(freeze.id)
            if stored is None:
                raise Conflict(f"freeze {freeze.id} not found")
            if stored.tenant_id != freeze.tenant_id:
                raise Conflict(f"freeze {freeze.id} tenant mismatch")
            if stored.cancelled_at is not None:
                raise Conflict(f"freeze {freeze.id} is already cancelled")
            self._by_id[freeze.id] = freeze


class InMemoryOrphanCaseProposalRepository:
    """Process-scoped ``OrphanCaseProposalRepository`` (FM-041 / P08-US8).

    Proposals live in an in-process dictionary keyed by id; a
    per-case open slot keeps the
    ``UNIQUE(tenant_id, case_id) WHERE status = 'open'`` invariant
    (the application-layer pre-check is authoritative; the
    dictionary is the defense-in-depth layer).
    """

    def __init__(self) -> None:
        self._by_id: dict[UUID, OrphanCaseProposal] = {}
        self._open_by_case: dict[tuple[UUID, UUID], OrphanCaseProposal] = {}
        self._lock = RLock()

    async def add(self, proposal: OrphanCaseProposal) -> None:
        with self._lock:
            case_key = (proposal.tenant_id, proposal.case_id)
            existing = self._open_by_case.get(case_key)
            if existing is not None and existing.is_open:
                raise Conflict(f"case {proposal.case_id} already has an open orphan proposal")
            self._by_id[proposal.id] = proposal
            if proposal.is_open:
                self._open_by_case[case_key] = proposal

    async def get(self, tenant_id: UUID, proposal_id: UUID) -> OrphanCaseProposal | None:
        with self._lock:
            row = self._by_id.get(proposal_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return row

    async def get_open_for_case(self, tenant_id: UUID, case_id: UUID) -> OrphanCaseProposal | None:
        with self._lock:
            row = self._open_by_case.get((tenant_id, case_id))
            if row is None:
                return None
            if not row.is_open:
                return None
            return row

    async def list_open(
        self, tenant_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> list[OrphanCaseProposal]:
        with self._lock:
            rows = [p for p in self._by_id.values() if p.tenant_id == tenant_id and p.is_open]
            rows.sort(key=lambda p: p.detected_at)
            return rows[offset : offset + limit]

    async def update(self, proposal: OrphanCaseProposal) -> None:
        with self._lock:
            stored = self._by_id.get(proposal.id)
            if stored is None:
                raise Conflict(f"orphan proposal {proposal.id} not found")
            if stored.tenant_id != proposal.tenant_id:
                raise Conflict(f"orphan proposal {proposal.id} tenant mismatch")
            if not stored.is_open:
                raise Conflict(f"orphan proposal {proposal.id} is already decided")
            self._by_id[proposal.id] = proposal
            case_key = (proposal.tenant_id, proposal.case_id)
            if not proposal.is_open:
                self._open_by_case.pop(case_key, None)


# Default-dict helper for tests / extension code that wants to
# register additional repositories by name without importing the
# concrete classes. Kept here so the seam placeholders share a
# canonical home.
RETENTION_REPOSITORY_REGISTRY: dict[str, type] = defaultdict(
    lambda: InMemoryRetentionPolicyRepository,
    {
        "retention_policy": InMemoryRetentionPolicyRepository,
        "disposition_request": InMemoryDispositionRequestRepository,
        "case_archival": InMemoryCaseArchivalRepository,
        "disposal_freeze": InMemoryDisposalFreezeRepository,
        "orphan_proposal": InMemoryOrphanCaseProposalRepository,
    },
)


__all__ = [
    "InMemoryCaseArchivalRepository",
    "InMemoryDisposalFreezeRepository",
    "InMemoryDispositionRequestRepository",
    "InMemoryOrphanCaseProposalRepository",
    "InMemoryRetentionPolicyRepository",
    "RETENTION_REPOSITORY_REGISTRY",
]
