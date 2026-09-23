"""Operational reports for supervisors (P12-US1, US2 / FM-051).

The first slice ships two read-only reports scoped to the actor's
team:

- **Open cases by status** — every case currently in
  ``CaseStatus.OPEN`` for a queue the supervisor is a member of.
- **Overdue tasks** — every ``OPEN`` task whose ``due_at`` is at or
  before "now" for the same set of queues.

Both reports are **snapshots**: the wire shape carries
``generated_at`` so a downstream consumer can answer "as of when" with
the exact timestamp the query was issued against the system of record
(P12 pillar statement, §1). Reports are read-only; the use case does
not write audit or outbox rows. A future export path will emit a
``ReportExportRequested`` audit event when P12-US6 lands.

# Why a package (R2 structural split)

Formerly a single ``operational_reports.py`` module of ~750 lines —
past the R2 600-line soft limit. Split by responsibility into
:mod:`vocabulary` (the ``REPORT_SOURCE`` marker and ``ReportKind``
enum), :mod:`shapes` (the frozen wire dataclasses, the failed-work
read port, and the query envelope), :mod:`helpers` (team resolution,
row projections, the limit guard, the authorization-scope marker), and
:mod:`use_cases` (the ``open_cases`` / ``overdue_tasks`` /
``failed_work`` snapshots). This ``__init__`` is a pure re-export
shim: ``__all__`` and every previously visible attribute are
re-exported unchanged, so no consumer import line needs editing and
``dir()`` stays identical.

# Authorization (P11 / FM-002)

The reports require ``Action.CASE_READ`` AND ``Action.TASK_READ``. A
supervisor role grants both; a reviewer grants both but does not have
the team-membership guarantee (a reviewer who is not a queue member
sees an empty result set, not a forbidden — the report never leaks
whether the queue exists). The deny-by-default policy is enforced
inside :func:`open_cases` / :func:`overdue_tasks` /
:func:`failed_work`.

# Team scope (FM-016 / FM-051)

A supervisor's "team" is the set of queues the user is a member of.
The :class:`QueueRepository` is the live source — the same set the
worker-claim flow uses, so revocation of queue membership takes
effect on the next report call without a cache invalidation.

A supervisor with no queue memberships sees an empty report rather
than a 404 (the report is about "the team the actor belongs to"; an
actor with no team has no rows by construction). The team scope is
clamped to the actor's tenant so a cross-tenant queue id cannot leak
a different tenant's work.

# Failure behavior

- Missing role grant -> ``Forbidden`` (HTTP 403).
- Cross-tenant actor -> denied by the policy (resource.tenant_id
  matches actor.tenant_id only).
- No team memberships -> empty report, generated_at populated, 200.

The use case is pure: no FastAPI / SQLAlchemy imports. The router is
the only place that wires ``SqlCaseRepository`` /
``SqlTaskRepository`` / ``SqlQueueRepository`` through the dependency
graph.

# INTEGRATION_SEAM: report engine

A ``Report`` port (P12 pillar §2) is reserved for a future slice where
the reports can be backed by Azure Synapse / Power BI. Today the use
case delegates directly to the existing repositories; the wire shape
already carries ``generated_at`` and ``kind`` so a future swap is a
single-file change in the router.
"""

from __future__ import annotations

from collections.abc import Sequence  # noqa: F401
from dataclasses import dataclass, field  # noqa: F401
from datetime import datetime, timedelta  # noqa: F401
from enum import StrEnum  # noqa: F401
from typing import Any, Protocol, cast  # noqa: F401
from uuid import UUID  # noqa: F401

from app.application.authorization import Action, Resource, authorize  # noqa: F401
from app.domain.case import Case  # noqa: F401
from app.domain.errors import ValidationFailed  # noqa: F401
from app.domain.ports import (  # noqa: F401
    CaseRepository,
    QueueRepository,
    TaskRepository,
)

from .helpers import (  # noqa: F401
    _ActorProxy,
    _case_to_row,
    _enforce_limit,
    _failed_to_row,
    _resolve_team,
    _task_to_row,
    build_authorization_scope,
)
from .shapes import (  # noqa: F401
    FailedWorkReport,
    FailedWorkRepository,
    FailedWorkRow,
    OpenCaseRow,
    OpenCasesReport,
    OperationalReportsQuery,
    OverdueTaskRow,
    OverdueTasksReport,
)
from .use_cases import failed_work, open_cases, overdue_tasks  # noqa: F401
from .vocabulary import REPORT_SOURCE, ReportKind  # noqa: F401

__all__ = [
    "FailedWorkReport",
    "FailedWorkRepository",
    "FailedWorkRow",
    "OpenCaseRow",
    "OpenCasesReport",
    "OperationalReportsQuery",
    "OverdueTaskRow",
    "OverdueTasksReport",
    "REPORT_SOURCE",
    "ReportKind",
    "build_authorization_scope",
    "failed_work",
    "open_cases",
    "overdue_tasks",
]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, but the pre-split single-file module exposed no such
# attributes. Popping them (and the package-only ``__path__``) keeps
# ``dir(app.application.reports.operational_reports)`` byte-identical to the
# old module's attribute surface — the R2 refactor must be invisible. The
# submodules stay importable (they are already in ``sys.modules``, which the
# import system consults before touching ``__path__``); only the redundant
# package-level attributes go away.
for _submodule in (
    "helpers",
    "shapes",
    "use_cases",
    "vocabulary",
):
    globals().pop(_submodule, None)
del _submodule
globals().pop("__path__", None)
