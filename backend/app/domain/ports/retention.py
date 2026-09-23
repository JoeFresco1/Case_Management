"""Retention policy + disposition + archival + freeze + orphan ports
(P08-US3, US4, US5, US7, US8 / FM-040, FM-041).

A ``RetentionPolicy`` is the versioned, immutable configuration
artefact ("keep ``LabResult`` documents for 7 years"). The
``DispositionRequest`` is the per-case review row surfaced when a
case crosses its retention horizon. The two repositories are kept
separate so the configuration row is small and immutable while the
workflow row carries mutable lifecycle state.

Tenant isolation is mandatory (AGENTS.md §3 R1, §12): every method
takes ``tenant_id`` first and the SQL adapter MUST include ``WHERE
tenant_id = :tenant_id`` on every query.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.ports.core import TenantScoped
from app.domain.retention import (
    CaseArchival,
    DisposalFreeze,
    DispositionRequest,
    OrphanCaseProposal,
    RetentionPolicy,
)


class RetentionPolicyRepository(TenantScoped, Protocol):
    """RetentionPolicy aggregate persistence (P08-US3 / FM-040).

    The repository is the single mutation surface for the versioned
    configuration artefact. ``next_version_number`` is the per-target
    monotonic counter the application layer uses to compute the next
    ``version_number`` before staging the new draft.

    Cross-tenant lookups return ``None`` for ``get`` / ``get_published``
    so a misconfigured caller cannot confirm cross-tenant existence
    (Constitution V). Writes raise :class:`Conflict` if a stale
    ``version_number`` would clobber a freshly published row.
    """

    async def add(self, policy: RetentionPolicy) -> None:
        """Persist a freshly drafted ``RetentionPolicy``.

        The ``tenant_id`` comes from the aggregate. Implementations
        MUST enforce the per-tenant uniqueness of
        ``(tenant_id, category, target_key, version_number)`` so a
        concurrent publish cannot stage two policies at the same
        version number. The application layer is responsible for
        resolving the next ``version_number`` via
        :meth:`next_version_number` before calling ``add``.
        """

    async def get(self, tenant_id: UUID, policy_id: UUID) -> RetentionPolicy | None:
        """Return the policy row by id, tenant-scoped.

        Returns ``None`` for missing ids AND for ids belonging to a
        different tenant so the API never confirms cross-tenant
        existence (Constitution V).
        """

    async def next_version_number(self, tenant_id: UUID, *, category: str, target_key: str) -> int:
        """Return the next monotonic ``version_number`` for the target.

        Used by the application layer to compute the
        ``version_number`` on a new draft. Returns 1 when no prior
        policy exists for the target. The lookup is keyed on
        ``(tenant_id, category, target_key)`` -- the per-target
        counter, not the global counter.
        """

    async def list_published(
        self, tenant_id: UUID, *, category: str | None = None
    ) -> list[RetentionPolicy]:
        """Return every published, non-retired policy for the tenant.

        Used by the disposition eligibility scan to resolve the
        active policy per category. When ``category`` is supplied,
        only that category's published policies are returned.
        Order is ``published_at DESC`` so the most recently
        published wins for a given target.
        """


class DispositionRequestRepository(TenantScoped, Protocol):
    """DispositionRequest aggregate persistence (P08-US4 / FM-040).

    The repository owns the per-case review queue. The disposition
    use case is the only writer; the repository exposes the durable
    add / get / list / update surface. Cross-tenant reads return
    ``None`` / empty sequences (Constitution V).
    """

    async def add(self, request: DispositionRequest) -> None:
        """Persist a freshly surfaced ``DispositionRequest``.

        Implementations SHOULD enforce a unique index on
        ``(tenant_id, case_id) WHERE status = 'pending'`` so a
        duplicate surfacing for the same case is refused. The use
        case checks first via :meth:`get_pending_for_case`; the
        index is the defense-in-depth layer (Constitution V).
        """

    async def get(self, tenant_id: UUID, request_id: UUID) -> DispositionRequest | None:
        """Return the disposition request by id, tenant-scoped.

        Returns ``None`` for missing ids AND for cross-tenant ids
        so the API never confirms cross-tenant existence
        (Constitution V).
        """

    async def get_pending_for_case(
        self, tenant_id: UUID, case_id: UUID
    ) -> DispositionRequest | None:
        """Return the (single) pending request for a case, or ``None``.

        A case owns at most one pending disposition request at any
        time -- once approved / rejected the row is terminal and the
        next eligibility pass surfaces a fresh row.
        """

    async def list_eligible(
        self, tenant_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> list[DispositionRequest]:
        """Return every pending request for the tenant, newest first.

        Used by the records-admin surface to render the review
        queue. Pagination lives on ``limit`` / ``offset`` (the same
        shape the case list endpoint uses -- AGENTS.md §17.1).
        """

    async def list_for_case(self, tenant_id: UUID, case_id: UUID) -> list[DispositionRequest]:
        """Return every disposition request (any status) for ``case_id``.

        Used by the defensible-deletion evidence report (FM-042 /
        P08-US6) to assemble the full chain of custody -- a case can
        carry multiple historical requests (e.g. a rejected one and
        a later approved one) and the report must surface them all
        in chronological order so the auditor can replay the full
        decision history. Tenant-scoped so a cross-tenant case id
        returns an empty list (Constitution V -- never confirm
        cross-tenant existence).
        """

    async def update(self, request: DispositionRequest) -> None:
        """Persist a decision on an existing request.

        The use case calls this after :meth:`DispositionRequest.approve`
        / :meth:`DispositionRequest.reject` returns a new aggregate
        carrying the decision fields. Implementations MUST refuse
        the update when the stored row is not in ``pending`` status
        (the use case's :meth:`get_pending_for_case` lookup is the
        authoritative pre-check).
        """


# ---------------------------------------------------------------------------
# Archival + freeze + orphan ports (P08-US5, US7, US8 / FM-041).
#
# The three repositories back the three pillar-level capabilities
# shipped on top of the disposition pipeline:
#
# - :class:`CaseArchivalRepository` -- per-case archival row + frozen
#   status. Looked up by the disposition approval use case to refuse
#   approval while a case is frozen.
# - :class:`DisposalFreezeRepository` -- tenant-wide disposal freezes.
#   The disposition approval use case lists active freezes and
#   refuses approval when any cover the evaluation moment.
# - :class:`OrphanCaseProposalRepository` -- reviewable per-case orphan
#   proposals. The detection use case adds rows; reviewers surface
#   or dismiss them. Auto-deletion is never wired -- the application
#   layer only writes proposals, the reviewer is the one who
#   authorizes the next step (Constitution IV).
#
# Tenant isolation is mandatory (AGENTS.md §3 R1, §12): every method
# takes ``tenant_id`` first; SQL adapters MUST include
# ``WHERE tenant_id = :tenant_id`` on every query.
# ---------------------------------------------------------------------------


class CaseArchivalRepository(TenantScoped, Protocol):
    """CaseArchival aggregate persistence (P08-US5 / FM-041).

    The repository owns the per-case archival row + the *current
    freeze state* (the most recent unfrozen row vacates the active
    slot; a fresh :meth:`archive` rebinds it). Cross-tenant reads
    return ``None`` / empty sequences so the API never confirms
    cross-tenant existence (Constitution V).
    """

    async def add(self, archival: CaseArchival) -> None:
        """Persist a fresh ``CaseArchival`` row.

        Implementations MUST enforce the ``(tenant_id, case_id,
        id)`` uniqueness constraint and refuse a second
        ``archived`` row for the same case until the prior one
        has been unfrozen (the in-memory adapter raises
        :class:`Conflict`; the SQL adapter relies on a partial
        unique index).
        """

    async def get(self, tenant_id: UUID, archival_id: UUID) -> CaseArchival | None:
        """Return the archival row by id, tenant-scoped."""

    async def get_active_for_case(self, tenant_id: UUID, case_id: UUID) -> CaseArchival | None:
        """Return the *current* archival row for ``case_id``, or ``None``.

        A case has at most one ``archived`` row at any time;
        ``unfreeze`` vacates the active slot so a future
        :meth:`archive` rebinds it.
        """

    async def update(self, archival: CaseArchival) -> None:
        """Persist an unfreeze on an existing ``archived`` row.

        Refused when the row is missing, cross-tenant, or already
        in ``unfrozen`` status (the use case calls
        :meth:`get_active_for_case` first and the caller never
        passes a stale aggregate).
        """


class DisposalFreezeRepository(TenantScoped, Protocol):
    """DisposalFreeze aggregate persistence (P08-US7 / FM-041).

    Freezes live per tenant. A freeze is *active* when
    ``is_active_at`` returns True; the repository's only job is
    durable storage -- the application layer consults
    :meth:`list_active` to assemble the candidate set.
    """

    async def add(self, freeze: DisposalFreeze) -> None:
        """Persist a fresh freeze row."""

    async def get(self, tenant_id: UUID, freeze_id: UUID) -> DisposalFreeze | None:
        """Return the freeze row by id, tenant-scoped."""

    async def list_active(self, tenant_id: UUID, *, at: datetime) -> list[DisposalFreeze]:
        """Return every freeze that blocks disposition at ``at``.

        Used by the disposition approval use case before
        recording an approval. Order is
        ``starts_at ASC`` (oldest first) so the audit
        replay is deterministic.
        """

    async def list_all(self, tenant_id: UUID) -> list[DisposalFreeze]:
        """Return every freeze row for the tenant (active and cancelled).

        Used by the admin console to render the freeze history
        with a "currently active" filter on the client.
        Order is ``starts_at DESC`` (newest first).
        """

    async def update(self, freeze: DisposalFreeze) -> None:
        """Persist a cancellation on an existing freeze row.

        Refused when the row is missing, cross-tenant, or already
        cancelled.
        """


class OrphanCaseProposalRepository(TenantScoped, Protocol):
    """OrphanCaseProposal aggregate persistence (P08-US8 / FM-041).

    The repository owns the per-case review queue. A case may
    carry at most one ``open`` proposal at a time; surfacing or
    dismissing vacates the slot so the next detection pass can
    surface a fresh row. Cross-tenant reads return ``None`` /
    empty sequences (Constitution V).
    """

    async def add(self, proposal: OrphanCaseProposal) -> None:
        """Persist a fresh ``open`` proposal.

        Implementations SHOULD refuse a second ``open`` row for
        the same case; the application layer checks first via
        :meth:`get_open_for_case` (defense in depth).
        """

    async def get(self, tenant_id: UUID, proposal_id: UUID) -> OrphanCaseProposal | None:
        """Return the proposal row by id, tenant-scoped."""

    async def get_open_for_case(self, tenant_id: UUID, case_id: UUID) -> OrphanCaseProposal | None:
        """Return the (single) open proposal for ``case_id``, or ``None``."""

    async def list_open(
        self, tenant_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> list[OrphanCaseProposal]:
        """Return every open proposal for the tenant, oldest first.

        Used by the records-admin surface to render the review
        queue. Pagination lives on ``limit`` / ``offset``.
        """

    async def update(self, proposal: OrphanCaseProposal) -> None:
        """Persist a decision (surface / dismiss) on an open proposal.

        Refused when the row is missing, cross-tenant, or already
        decided.
        """


__all__ = [
    "CaseArchivalRepository",
    "DisposalFreezeRepository",
    "DispositionRequestRepository",
    "OrphanCaseProposalRepository",
    "RetentionPolicyRepository",
]
