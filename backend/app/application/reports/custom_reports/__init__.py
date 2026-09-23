"""Custom report definitions and audited CSV export (P12-US6, US7 / FM-053).

The pillar P12 statement distinguishes snapshots from aggregates; the
first slice ships two snapshots (open cases, overdue tasks) and reserves
aggregates for later slices. This package introduces the **custom report
designer** the process administrator uses to express a controlled
aggregate: a declarative grouping/aggregation/filter/time-window spec
that is persisted as a ``ReportDefinition`` and rendered against the
operational read models on demand.

# Why a package (R2 structural split)

Formerly a single ``custom_reports.py`` module of ~950 lines — past the
R2 600-line soft limit. Split by responsibility into :mod:`vocabulary`
(closed enum allowlists), :mod:`shapes` (the frozen wire dataclasses),
:mod:`repository` (the definition port + process singleton),
:mod:`validation` (the pure closed-vocabulary validator), :mod:`define`
(define use case), :mod:`render` (render use case + filter/aggregate
helpers), :mod:`export` (audited CSV export), and :mod:`helpers` (the
shared actor proxy). This ``__init__`` is a pure re-export shim:
``__all__`` and every previously visible attribute are re-exported
unchanged, so no consumer import line needs editing and ``dir()`` stays
identical.

# Authorization (P11 / FM-002)

- ``define_custom_report`` requires ``Action.ADMIN_TENANT_CONFIG``
  (process administrators are the only role with this grant). The
  definition is tenant-scoped; a cross-tenant ``definition_id`` on a
  later read surfaces as ``NotFound`` (Constitution V).
- ``export_custom_report`` requires ``Action.CASE_READ`` AND
  ``Action.AUDIT_EXPORT`` so a supervisor can export a report they are
  authorized to read AND the export is itself a security-relevant audit
  event (P12 pillar §2: "export ... is itself an audit event").

# What "controlled" means

The spec is a closed vocabulary — the validator rejects any field
that is not on the allowlist, any operator that is not on the operator
allowlist, and any aggregation that is not on the aggregation
allowlist. The closed lists live in :mod:`vocabulary` so a future
schema extension is a one-file change. The validator is pure: no I/O,
no clock, no actor context — it can be reused by the simulator
(P02-US5) when a "simulate report" surface lands.

# Audited export (P12-US6 / P09-US5)

``export_custom_report`` stages a single ``AuditExported`` row in the
same transaction as the read so the export action is visible on the
audit chain. The audit payload carries the definition id, the actor,
the row count, and a stable ``format`` marker (``"csv"``) so a later
replay can answer "who exported what, when, and how many rows". The
export itself does not commit; the caller (the HTTP router) commits
once. The CSV body is rendered **after** the use case returns the row
collection; the application layer never writes the file.

# INTEGRATION_SEAM: report engine

The ``Report`` port (P12 pillar §2) is reserved for an Azure Synapse /
Power BI production engine. Today the renderer walks the same case /
task repositories as the operational reports; a future swap is a
single-file change in the router. The CSV serialization is also
in-process; a future slice may move it to a document-store-backed
downloadable artifact (P03 seam).
"""

from __future__ import annotations

import csv  # noqa: F401
import io  # noqa: F401
from collections.abc import Iterable, Sequence  # noqa: F401
from dataclasses import dataclass, field  # noqa: F401
from datetime import datetime, timedelta  # noqa: F401
from enum import StrEnum  # noqa: F401
from typing import Any, cast  # noqa: F401
from uuid import UUID, uuid4  # noqa: F401

from app.application.audit.record_event import RecordAuditCommand, record_event  # noqa: F401
from app.application.authorization import Action, Resource, authorize  # noqa: F401
from app.application.reports.operational_reports import (  # noqa: F401
    REPORT_SOURCE,
    OperationalReportsQuery,
    ReportKind,
    build_authorization_scope,
    open_cases,
)
from app.domain.audit import AUDIT_EXPORTED, payload_hash  # noqa: F401
from app.domain.case import Case  # noqa: F401
from app.domain.errors import NotFound, ValidationFailed  # noqa: F401
from app.domain.ports import AuditEventStore, CaseRepository, QueueRepository  # noqa: F401

from .define import define_custom_report  # noqa: F401
from .export import ExportCustomReportCommand, _render_csv, export_custom_report  # noqa: F401
from .helpers import _ActorProxy  # noqa: F401
from .render import (  # noqa: F401
    _GROUP_BY_PROJECTORS,
    _aggregate,
    _fetch_cases,
    _match_clause,
    _matches_definition,
    _project_value,
    _scalar_eq,
    _validate_time_window,
    render_custom_report,
)
from .repository import (  # noqa: F401
    ReportDefinitionRepository,
    _default_repository,
    default_repository,
    reset_default_repository,
)
from .shapes import (  # noqa: F401
    AggregateBucket,
    CustomReportResult,
    DefineCustomReportCommand,
    ExportResult,
    FilterClause,
    ReportDefinition,
)
from .validation import _validate_clause_value, _validate_command  # noqa: F401
from .vocabulary import (  # noqa: F401
    _FILTERABLE_FIELDS,
    _OPERATORS_FOR_VALUE,
    AggregationKind,
    FilterOperator,
    GroupByField,
)

__all__: list[str] = [
    "AggregationKind",
    "AggregateBucket",
    "CustomReportResult",
    "DefineCustomReportCommand",
    "ExportCustomReportCommand",
    "ExportResult",
    "FilterClause",
    "FilterOperator",
    "GroupByField",
    "ReportDefinition",
    "ReportDefinitionRepository",
    "define_custom_report",
    "default_repository",
    "export_custom_report",
    "render_custom_report",
    "reset_default_repository",
]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, but the pre-split single-file module exposed no such
# attributes. Popping them (and the package-only ``__path__``) keeps
# ``dir(app.application.reports.custom_reports)`` byte-identical to the old
# module's attribute surface — the R2 refactor must be invisible. The
# submodules stay importable (they are already in ``sys.modules``, which the
# import system consults before touching ``__path__``); only the redundant
# package-level attributes go away. ``_default_repository`` re-exported above
# is the initial ``None`` binding — the live singleton state lives in
# :mod:`.repository`, and callers reach it through ``default_repository()`` /
# ``reset_default_repository()`` as before.
for _submodule in (
    "define",
    "export",
    "helpers",
    "render",
    "repository",
    "shapes",
    "validation",
    "vocabulary",
):
    globals().pop(_submodule, None)
del _submodule
globals().pop("__path__", None)
