"""Wire shapes — the dataclasses the router serializes (P12-US1, US2, US4).

Frozen value objects passed between the HTTP layer and the operational
report use cases: the row types, the snapshot report envelopes, the
failed-work read port, and the common query envelope the router
supplies. Split out of the former single-file ``operational_reports.py``
during the R2 package split; no behavior lives here beyond field
defaults.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from .vocabulary import REPORT_SOURCE, ReportKind


@dataclass(frozen=True, slots=True)
class OpenCaseRow:
    """A single row in the open-cases report (P12-US1 / FM-051).

    The shape is intentionally narrow: only the fields the supervisor
    needs to triage. Sensitive case data (``data_json``) is not
    projected here — the supervisor's sensitive-read grant is checked
    at the use-case boundary; the row carries the case id / reference
    so the supervisor can navigate to the workspace for the full view.

    The case is "on the team" by virtue of having at least one open
    task assigned to a queue the supervisor is a member of. The
    row does not project a single ``assignee_queue_id`` because a
    case may have tasks across several queues.
    """

    case_id: UUID
    reference: str
    case_type_version_id: UUID
    status: str
    priority: str
    title: str
    current_workflow_state: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OverdueTaskRow:
    """A single row in the overdue-tasks report (P12-US2 / FM-051).

    Overdue is computed from the system clock: a task is overdue iff
    ``status == OPEN`` AND ``due_at <= now``. The row exposes the
    overdue window as a derived field so the UI does not have to
    recompute it.
    """

    task_id: UUID
    case_id: UUID
    type: str
    title: str
    priority: str
    assignee_user_id: UUID | None
    assignee_queue_id: str | None
    due_at: datetime
    overdue_seconds: int


@dataclass(frozen=True, slots=True)
class OpenCasesReport:
    """The wire shape for ``GET /api/v1/reports/open-cases``.

    ``generated_at`` is the system-of-record-as-of timestamp (P12 §1):
    the report reflects the **current state** frozen at this instant
    (``is_historical`` is always ``False`` for this snapshot).
    ``team`` is the resolved set of queue keys the report covers —
    supervisors can verify the scope they are looking at.
    ``authorization_scope`` repeats the tenant / actor / roles / team
    the policy evaluated so a KPI consumer can audit "who saw what
    under which grant". ``source`` names the system of record.
    ``items`` is the snapshot list. ``total`` is the count at
    ``generated_at``.
    """

    kind: ReportKind
    generated_at: datetime
    tenant_id: UUID
    actor_id: UUID
    team: tuple[str, ...]
    total: int
    items: tuple[OpenCaseRow, ...] = field(default_factory=tuple)
    authorization_scope: tuple[str, ...] = field(default_factory=tuple)
    source: str = REPORT_SOURCE
    is_historical: bool = False
    window_start: datetime | None = None
    window_end: datetime | None = None


@dataclass(frozen=True, slots=True)
class OverdueTasksReport:
    """The wire shape for ``GET /api/v1/reports/overdue-tasks``.

    Snapshot of **current state** (``is_historical=False``); see
    :class:`OpenCasesReport` for the ``generated_at`` /
    ``authorization_scope`` / ``source`` contract.
    """

    kind: ReportKind
    generated_at: datetime
    tenant_id: UUID
    actor_id: UUID
    team: tuple[str, ...]
    total: int
    items: tuple[OverdueTaskRow, ...] = field(default_factory=tuple)
    authorization_scope: tuple[str, ...] = field(default_factory=tuple)
    source: str = REPORT_SOURCE
    is_historical: bool = False
    window_start: datetime | None = None
    window_end: datetime | None = None


@dataclass(frozen=True, slots=True)
class FailedWorkRow:
    """A single row in the failed-work report (P12-US4 / FM-052).

    Surfaces background work that needs an operator: the durable
    ``event_type`` that failed, how many attempts ran, the last safe
    error (never a stack trace or secret), and whether the row is
    still eligible for replay. ``queue_key`` carries the team affinity
    when the work item is queue-bound; rows without queue affinity
    (tenant-level infrastructure work) carry ``None`` and are only
    visible to actors whose team covers them via the operator grant.
    """

    work_id: UUID
    event_type: str
    queue_key: str | None
    case_id: UUID | None
    attempt_count: int
    last_error: str | None
    first_failed_at: datetime
    last_attempt_at: datetime
    replay_eligible: bool


@dataclass(frozen=True, slots=True)
class FailedWorkReport:
    """The wire shape for the failed-background-work report.

    Snapshot of **current state**: every row is work that is still
    failing at ``generated_at``. Historical (already-replayed or
    dispatched) work never appears here — replay is a separate audited
    action on the outbox console. ``authorization_scope`` / ``source``
    / ``is_historical`` follow the :class:`OpenCasesReport` contract
    so current-state and historical reports are visually distinct.
    """

    kind: ReportKind
    generated_at: datetime
    tenant_id: UUID
    actor_id: UUID
    team: tuple[str, ...]
    total: int
    items: tuple[FailedWorkRow, ...] = field(default_factory=tuple)
    authorization_scope: tuple[str, ...] = field(default_factory=tuple)
    source: str = REPORT_SOURCE
    is_historical: bool = False
    window_start: datetime | None = None
    window_end: datetime | None = None


class FailedWorkRepository(Protocol):
    """Persistence port for failed background work (P12-US4 / FM-052).

    Lives here (not in ``domain/ports``) because reporting owns the
    read shape; the outbox dispatcher owns the write shape. Any object
    exposing ``list_failed`` satisfies the port — the test layer
    supplies an in-memory fake, the HTTP layer wires the outbox SQL
    adapter behind a thin translator. ``Any`` is confined to this
    port per the layer contract.
    """

    async def list_failed(
        self,
        tenant_id: UUID,
        *,
        queue_keys: tuple[str, ...],
        limit: int = 200,
    ) -> Sequence[Any]:
        """Return still-failing work rows for ``queue_keys``, oldest first."""
        ...


@dataclass(frozen=True, slots=True)
class OperationalReportsQuery:
    """Common fields every operational-report query carries.

    The router does not have to know which queues the supervisor is a
    member of — the use case resolves them through the live
    ``QueueRepository``. The router only supplies the actor + tenant
    context; everything else is the supervisor's live authorization.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]


__all__ = [
    "FailedWorkReport",
    "FailedWorkRepository",
    "FailedWorkRow",
    "OpenCaseRow",
    "OpenCasesReport",
    "OperationalReportsQuery",
    "OverdueTaskRow",
    "OverdueTasksReport",
]
