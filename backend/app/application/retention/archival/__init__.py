"""Archival + freeze + orphan use cases (P08-US5, US7, US8 / FM-041).

The retention pillar's archival surface ships six use cases on top
of the disposition pipeline:

- :func:`archive_case` — move a case to the warmer tier. The case
  is frozen (the application layer refuses disposition while a
  case is frozen). The move is audited; the unfreeze is a
  separate, audited action.
- :func:`unfreeze_case` — lift the freeze on an archived case.
  The historical archival row stays in the audit chain.
- :func:`declare_disposal_freeze` — declare a tenant-wide freeze
  window during which the disposition approval use case refuses
  to approve any pending request.
- :func:`cancel_disposal_freeze` — cancel an active freeze before
  its natural ``ends_at`` expires.
- :func:`propose_orphan_cases` — run the orphan-detection scan.
  Cases past their retention horizon AND with no activity for
  :data:`app.domain.retention.ORPHAN_INACTIVITY_DAYS` are
  surfaced as :class:`OrphanCaseProposal` rows. The scan never
  auto-deletes a case (Constitution IV) — it only stages
  reviewable proposals.
- :func:`review_orphan_proposal` — the reviewer's decision. A
  proposal can be ``surface``d (the disposition approval queue
  is then responsible for the next step) or ``dismiss``ed with
  a required rationale.

# Cross-aggregate guards

The archival + freeze + orphan use cases interact with the
existing disposition pipeline at two seams:

- A case under an active :class:`CaseArchival` cannot be
  approved for disposition. The disposition approval use case
  consults the archival repository before recording the
  approval. The archive use case is the writer of the row; the
  approval use case is the reader.
- A pending disposition request inside an active
  :class:`DisposalFreeze` window cannot be approved. The
  approval use case lists active freezes and refuses if any
  cover the evaluation moment. The freeze-declaration use case
  is the writer of the row; the approval use case is the
  reader.

# Refusal semantics

Five guards apply at the application boundary:

- Authorization — each use case requires a specific action
  (e.g. ``RETENTION_ARCHIVE`` / ``RETENTION_FREEZE_DECLARE`` /
  ``RETENTION_ORPHAN_SURFACE``). Only the ``process_admin``
  role grants these actions.
- Tenant isolation — every repository call carries
  ``tenant_id`` first; the SQL adapter must include
  ``WHERE tenant_id = :tenant_id`` on every query.
- Optimistic concurrency — :func:`unfreeze_case`,
  :func:`cancel_disposal_freeze`, and :func:`review_orphan_proposal`
  look up the row by id and refuse if the row is not in the
  expected status, so a stale reviewer cannot silently
  overwrite a concurrent decision.
- Legal hold — :func:`propose_orphan_cases` skips cases under
  active legal hold (a held case is never an orphan candidate
  even if it is past its retention horizon). The detection
  rule preserves the pillar's "hold overrides retention"
  invariant.
- Audit atomicity — every state change is paired with its
  audit row in the caller's DB transaction; a partial commit
  is impossible (Constitution III).

# Why a package (R2 structural split)

Formerly a single ``archival.py`` module of ~900 lines — past the
R2 600-line soft limit. Split by responsibility into :mod:`common`
(subject-type constants + clock/actor protocols + correlation id),
:mod:`archive` (archive / unfreeze), :mod:`freezes` (declare /
cancel freeze), :mod:`orphan` (scan / review proposals), and
:mod:`guards` (the disposition-blocked read helpers). This
``__init__`` is a pure re-export shim: ``__all__`` and every
previously visible attribute are re-exported unchanged, so no
consumer import line needs editing and ``dir()`` stays identical.

# INTEGRATION_SEAM

The repositories (``CaseArchivalRepository`` /
``DisposalFreezeRepository`` /
``OrphanCaseProposalRepository``) are the integration points.
The in-memory adapters live next to
:class:`InMemoryRetentionPolicyRepository`; the SQL adapters
land in a follow-up dispatch. The router depends on the
Protocol and the concrete adapter is wired through
``app.state`` so the SQL swap is a one-line change.
"""

from __future__ import annotations

from dataclasses import dataclass, field  # noqa: F401
from datetime import datetime  # noqa: F401
from typing import Protocol  # noqa: F401
from uuid import UUID  # noqa: F401

from app.application.authorization import Action, Resource, authorize  # noqa: F401
from app.domain.audit import (  # noqa: F401
    CASE_ARCHIVED,
    CASE_UNFROZEN,
    DISPOSAL_FREEZE_CANCELLED,
    DISPOSAL_FREEZE_DECLARED,
    ORPHAN_PROPOSAL_DISMISSED,
    ORPHAN_PROPOSAL_SURFACED,
)
from app.domain.errors import Conflict, NotFound, ValidationFailed  # noqa: F401
from app.domain.ports import (  # noqa: F401
    AuditEventStore,
    CaseArchivalRepository,
    CaseRepository,
    DisposalFreezeRepository,
    LegalHoldRepository,
    OrphanCaseProposalRepository,
)
from app.domain.retention import (  # noqa: F401
    ORPHAN_INACTIVITY_DAYS,
    ArchivalTier,
    CaseArchival,
    DisposalFreeze,
    OrphanCaseProposal,
    is_freeze_blocking,
)

from .archive import (  # noqa: F401
    ArchiveCaseCommand,
    ArchiveCaseResult,
    UnfreezeCaseCommand,
    UnfreezeCaseResult,
    archive_case,
    unfreeze_case,
)
from .common import (  # noqa: F401
    SUBJECT_TYPE_ARCHIVAL,
    SUBJECT_TYPE_CASE,
    SUBJECT_TYPE_DISPOSAL_FREEZE,
    SUBJECT_TYPE_ORPHAN_PROPOSAL,
    _ActorProxy,
    _ClockLike,
    _new_correlation_id,
)
from .freezes import (  # noqa: F401
    CancelDisposalFreezeCommand,
    CancelDisposalFreezeResult,
    DeclareDisposalFreezeCommand,
    DeclareDisposalFreezeResult,
    cancel_disposal_freeze,
    declare_disposal_freeze,
)
from .guards import (  # noqa: F401
    ActiveFreezeSnapshot,
    is_disposition_blocked,
    list_active_freezes,
)
from .orphan import (  # noqa: F401
    OrphanProposalEntry,
    OrphanScanResult,
    ReviewOrphanProposalCommand,
    ReviewOrphanProposalResult,
    propose_orphan_cases,
    review_orphan_proposal,
)

__all__: list[str] = [
    "ActiveFreezeSnapshot",
    "ArchiveCaseCommand",
    "ArchiveCaseResult",
    "CancelDisposalFreezeCommand",
    "CancelDisposalFreezeResult",
    "DeclareDisposalFreezeCommand",
    "DeclareDisposalFreezeResult",
    "OrphanProposalEntry",
    "OrphanScanResult",
    "ReviewOrphanProposalCommand",
    "ReviewOrphanProposalResult",
    "SUBJECT_TYPE_ARCHIVAL",
    "SUBJECT_TYPE_CASE",
    "SUBJECT_TYPE_DISPOSAL_FREEZE",
    "SUBJECT_TYPE_ORPHAN_PROPOSAL",
    "UnfreezeCaseCommand",
    "UnfreezeCaseResult",
    "archive_case",
    "cancel_disposal_freeze",
    "declare_disposal_freeze",
    "is_disposition_blocked",
    "list_active_freezes",
    "propose_orphan_cases",
    "review_orphan_proposal",
    "unfreeze_case",
]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, but the pre-split single-file module exposed no such
# attributes. Popping them (and the package-only ``__path__``) keeps
# ``dir(app.application.retention.archival)`` byte-identical to the old
# module's attribute surface — the R2 refactor must be invisible. The
# submodules stay importable (they are already in ``sys.modules``, which the
# import system consults before touching ``__path__``); only the redundant
# package-level attributes go away.
for _submodule in (
    "archive",
    "common",
    "freezes",
    "guards",
    "orphan",
):
    globals().pop(_submodule, None)
del _submodule
globals().pop("__path__", None)
