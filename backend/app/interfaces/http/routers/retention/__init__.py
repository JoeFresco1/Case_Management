"""Retention policy + disposition + archival router (P08-US3..US8 / FM-040, FM-041).

# Why a package (R2 structural split)

Formerly a single ``retention.py`` module of ~1100 lines — past the R2
600-line soft limit. Split by route-group responsibility into
:mod:`common` (shared ``router``, idempotency guard, projections, repo
factories), :mod:`policies`, :mod:`disposition`, :mod:`archival`,
:mod:`freezes`, and :mod:`orphans`. This ``__init__`` is a pure
re-export shim: ``__all__`` and every previously visible attribute are
re-exported unchanged, so no consumer import line needs editing
(``from ...retention import router`` and
``mock.patch("app.interfaces.http.routers.retention.X")`` keep working).

Endpoints:

- ``POST /api/v1/admin/retention-policies`` — publish a new version
  of a retention policy. Requires ``RETENTION_POLICY_PUBLISH``
  (granted only to ``process_admin``).
- ``GET /api/v1/admin/disposition/eligible`` — list cases that are
  past their retention horizon, are NOT under legal hold, and do
  not already have a pending disposition request.
- ``POST /api/v1/admin/disposition/eligible/{case_id}/route`` —
  surface a case as a pending ``DispositionRequest``.
- ``POST /api/v1/admin/disposition/{request_id}/approve`` —
  approve a pending request (``DISPOSITION_APPROVE``).
- ``POST /api/v1/admin/disposition/{request_id}/reject`` —
  reject a pending request (``DISPOSITION_REJECT``); body must
  include a non-blank ``reason``.
- ``POST /api/v1/admin/archival/cases/{case_id}/archive`` — move
  a case to the warmer tier (``RETENTION_ARCHIVE``). The case
  is frozen; the disposition approval use case refuses
  approvals while a case is frozen.
- ``POST /api/v1/admin/archival/cases/{case_id}/unfreeze`` —
  lift the freeze (``RETENTION_UNFREEZE``).
- ``POST /api/v1/admin/disposal-freezes`` — declare a
  tenant-wide freeze window (``RETENTION_FREEZE_DECLARE``).
- ``POST /api/v1/admin/disposal-freezes/{freeze_id}/cancel`` —
  cancel an active freeze (``RETENTION_FREEZE_CANCEL``).
- ``GET /api/v1/admin/disposal-freezes/active`` — list active
  freezes.
- ``POST /api/v1/admin/orphan-proposals/scan`` — run the
  orphan-detection scan (``RETENTION_ORPHAN_SURFACE``). Returns
  the freshly staged proposals + the skipped-hold ids; the
  scan never auto-deletes a case.
- ``GET /api/v1/admin/orphan-proposals`` — list the open
  proposals.
- ``POST /api/v1/admin/orphan-proposals/{id}/review`` — record
  a reviewer's decision (``RETENTION_ORPHAN_SURFACE`` /
  ``RETENTION_ORPHAN_DISMISS``). The dismissal requires a
  non-blank ``reason``.

# Authorization

The records-admin endpoints all gate on the ``process_admin``
actions:

- Publish: ``RETENTION_POLICY_PUBLISH``
- Eligible list / route: ``RETENTION_POLICY_PUBLISH`` (records
  admin action)
- Approve: ``DISPOSITION_APPROVE``
- Reject: ``DISPOSITION_REJECT``
- Archive: ``RETENTION_ARCHIVE``
- Unfreeze: ``RETENTION_UNFREEZE``
- Declare freeze: ``RETENTION_FREEZE_DECLARE``
- Cancel freeze: ``RETENTION_FREEZE_CANCEL``
- Orphan scan: ``RETENTION_ORPHAN_SURFACE``
- Orphan surface review: ``RETENTION_ORPHAN_SURFACE``
- Orphan dismiss review: ``RETENTION_ORPHAN_DISMISS``

The deny-by-default policy raises :class:`Forbidden` on a missing
role, which the HTTP layer maps to 403 (AGENTS.md §4.R12).

# Transaction shape

Each use case stages the archival / freeze / proposal row + the
audit row in the caller's DB transaction. The HTTP boundary
commits once after the use case returns; on rollback everything
reverts together.

# INTEGRATION_SEAM

The :class:`InMemoryRetentionPolicyRepository` /
:class:`InMemoryDispositionRequestRepository` /
:class:`InMemoryCaseArchivalRepository` /
:class:`InMemoryDisposalFreezeRepository` /
:class:`InMemoryOrphanCaseProposalRepository` (under
``app.infrastructure.persistence.retention_repository``) are the
seam placeholders for the H1 slice; the SQL adapters land with the
follow-up dispatch. The router depends on the Protocol from
``app.domain.ports`` and the concrete adapters are wired through
``app.state``.
"""

from __future__ import annotations

from .archival import (  # noqa: F401
    ArchivalTier,
    ArchiveCaseCommand,
    ArchiveCaseRequest,
    UnfreezeCaseCommand,
    UnfreezeCaseRequest,
    archive_case,
    archive_case_endpoint,
    unfreeze_case,
    unfreeze_case_endpoint,
)
from .common import (  # noqa: F401
    APIRouter,
    CaseArchival,
    CaseArchivalRepository,
    CaseArchivalResponse,
    DisposalFreeze,
    DisposalFreezeRepository,
    DisposalFreezeResponse,
    DispositionRequestRepository,
    DispositionRequestResponse,
    HTTPException,
    LegalHoldRepository,
    OrphanCaseProposalRepository,
    OrphanProposalResponse,
    Request,
    RetentionPolicy,
    RetentionPolicyRepository,
    RetentionPolicyResponse,
    _archival_repository,
    _archival_to_response,
    _disposition_repository,
    _freeze_repository,
    _freeze_to_response,
    _hold_repository,
    _orphan_repository,
    _orphan_to_response,
    _policy_repository,
    _policy_to_response,
    _request_to_response,
    _require_idempotency_key,
    cast,
    router,
    status,
)
from .disposition import (  # noqa: F401
    UUID,
    AsyncSession,
    Clock,
    Depends,
    DispositionDecisionCommand,
    DispositionDecisionRequest,
    EligibleCaseEntryResponse,
    EligibleListResponse,
    Header,
    Path,
    RetentionHttpDependencyProvider,
    RouteDispositionCommand,
    UserContext,
    approve_disposition,
    approve_disposition_endpoint,
    get_clock,
    get_current_user,
    get_db,
    get_retention_dependencies,
    list_eligible_endpoint,
    list_eligible_for_disposition,
    reject_disposition,
    reject_disposition_endpoint,
    route_disposition_endpoint,
    route_disposition_for_case,
)
from .freezes import (  # noqa: F401
    ActiveFreezeListResponse,
    CancelDisposalFreezeCommand,
    CancelDisposalFreezeRequest,
    DeclareDisposalFreezeCommand,
    DeclareDisposalFreezeRequest,
    cancel_disposal_freeze,
    cancel_disposal_freeze_endpoint,
    declare_disposal_freeze,
    declare_disposal_freeze_endpoint,
    list_active_freezes,
    list_active_freezes_endpoint,
)
from .orphans import (  # noqa: F401
    OrphanProposalEntryResponse,
    OrphanProposalListResponse,
    OrphanScanResponse,
    ReviewOrphanProposalCommand,
    ReviewOrphanProposalRequest,
    list_orphan_proposals_endpoint,
    propose_orphan_cases,
    propose_orphan_proposals_endpoint,
    review_orphan_proposal,
    review_orphan_proposal_endpoint,
)
from .policies import (  # noqa: F401
    PublishRetentionPolicyCommand,
    PublishRetentionPolicyRequest,
    RetentionCategory,
    publish_retention_policy,
    publish_retention_policy_endpoint,
)

__all__ = ["router"]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, and packages additionally carry ``__path__`` — neither
# existed on the pre-split single-file module. Popping them keeps
# ``dir(app.interfaces.http.routers.retention)`` byte-identical to the old
# module's attribute surface — the R2 refactor must be invisible. The
# submodules stay importable (``...retention.policies`` resolves through
# ``sys.modules``, which is untouched); only the redundant package-level
# attributes go away.
for _submodule in (
    "archival",
    "common",
    "disposition",
    "freezes",
    "orphans",
    "policies",
    "__path__",
):
    globals().pop(_submodule, None)
del _submodule
