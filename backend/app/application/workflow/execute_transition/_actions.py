"""Transition action materializers: create_task, set_field, start_sla, task reads.

Split from the flat ``execute_transition`` module (R2: files under 600
lines). Each helper stages one configured action type; ``handle`` calls
them in order inside the single transaction so state bump, field writes,
new tasks, and SLA clocks commit (or roll back) together (Constitution
III). ``_tasks_for_case`` lives here because the create-task
double-fire guard (ADR-029) is its primary caller; ``handle`` and
``preflight_transitions`` also re-use it via the package root.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from app.domain.case import Case
from app.domain.errors import InvariantViolation, ValidationFailed
from app.domain.ports import (
    CaseTypeVersionRepository,
    SlaClockRepository,
    TaskRepository,
)
from app.domain.sla import SlaClock, SlaClockStatus
from app.domain.task import Task, TaskStatus


async def _tasks_for_case(task_repo: TaskRepository, tenant_id: UUID, case_id: UUID) -> list[Task]:
    """Read the tasks for a case via the ``list_for_case`` port.

    The port is now part of ``TaskRepository`` (the SQL and in-memory
    adapters both implement it), so guards that reference ``case.tasks``
    and the create-task double-fire guard (ADR-029) both read the real
    task list. The ``getattr`` fallback is retained only so older test
    fakes that implement the earlier, narrower surface keep working
    without a rewrite.
    """
    list_fn = getattr(task_repo, "list_for_case", None)
    if list_fn is None:
        return []
    rows = await list_fn(tenant_id, case_id)
    return list(rows)


def _as_queue(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None


def _as_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _positive_hours(value: Any) -> float | None:
    """Parse a positive SLA duration in hours, or return ``None``.

    Mirrors the ``start_workflow`` helper: malformed or non-positive
    durations are skipped at runtime (publish validation is the gate).
    """
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    return hours if hours > 0 else None


async def _materialize_create_task_actions(
    *,
    task_repo: TaskRepository,
    tenant_id: UUID,
    case_id: UUID,
    actions: object,
    transition_key: str,
    now: datetime,
) -> list[Task]:
    """Materialize ``create_task`` transition actions, then persist them.

    A ``create_task`` action whose ``task_type`` is already open on the
    case is skipped: ``start_workflow`` stages that same action as the
    intake "welcome" task (``created_by_transition="initial"``), and the
    transition must not fire a second, duplicate task when it runs
    (welcome-vs-transition double-fire — ADR-029). Only exact ``type``
    matches against **open** tasks are suppressed; a completed or
    cancelled task of that type does not block a fresh one, and distinct
    task types materialize normally.
    """
    if not isinstance(actions, (list, tuple)):
        return []
    existing_open_types = {
        t.type
        for t in await _tasks_for_case(task_repo, tenant_id, case_id)
        if t.status is TaskStatus.OPEN
    }
    new_tasks: list[Task] = []
    for action in actions:
        if not isinstance(action, dict) or action.get("type") != "create_task":
            continue
        task_type = str(action.get("task_type", "")).strip()
        if not task_type:
            # The publish-time validator rejects an action without a
            # task_type; runtime is defense-in-depth only.
            continue
        if task_type in existing_open_types:
            # Already materialized by the welcome path (or a prior
            # transition); skip rather than double-fire the task.
            continue
        new_tasks.append(
            Task.create(
                tenant_id=tenant_id,
                case_id=case_id,
                type=task_type,
                title=str(action.get("title", task_type)),
                priority=str(action.get("priority", "normal")),
                assignee_queue_id=_as_queue(action.get("queue")),
                assignee_user_id=_as_uuid(action.get("assignee_user_id")),
                due_at=None,
                created_at=now,
                created_by_transition=transition_key,
            )
        )
        existing_open_types.add(task_type)

    for task in new_tasks:
        await task_repo.add(task)
    return new_tasks


async def _fire_start_sla_actions(
    *,
    tenant_id: UUID,
    case_id: UUID,
    actions: object,
    sla_clock_repo: SlaClockRepository | None,
    now: datetime,
) -> list[str]:
    """Fire ``start_sla`` transition actions (T071).

    A clock whose key already has a live (active/warning/paused) clock
    on this case is left alone — restarting a running deadline would
    corrupt its breach semantics (the welcome-task path in
    ``start_workflow`` already staged one at intake for the common
    blueprint). A terminal (breached/completed) clock does not block a
    fresh one: the new phase gets a new deadline. Without an SLA
    repository the actions are skipped. Returns the started keys for
    the audit payload.
    """
    started: list[str] = []
    if sla_clock_repo is None or not isinstance(actions, (list, tuple)):
        return started
    live_keys: set[str] = set()
    for existing in await sla_clock_repo.list_for_case(tenant_id, case_id):
        if existing.status in (
            SlaClockStatus.ACTIVE,
            SlaClockStatus.WARNING,
            SlaClockStatus.PAUSED,
        ):
            live_keys.add(existing.clock_key)
    for action in actions:
        if not isinstance(action, dict) or action.get("type") != "start_sla":
            continue
        clock_key = str(action.get("sla_key", "")).strip()
        hours = _positive_hours(action.get("hours"))
        if not clock_key or hours is None or clock_key in live_keys:
            # Malformed actions are rejected at publish time; runtime
            # skips them rather than creating an invalid deadline (same
            # defense-in-depth as ``create_task``).
            continue
        # FM-018: forward inline escalation config (validated at
        # publish time) onto the clock so the scanner fires it on
        # breach/warning. ``escalation`` is the legacy alias for
        # ``escalation_on_breach``.
        escalation_on_breach = action.get("escalation_on_breach", action.get("escalation"))
        escalation_on_warning = action.get("escalation_on_warning")
        if escalation_on_breach is not None and not isinstance(escalation_on_breach, dict):
            escalation_on_breach = None
        if escalation_on_warning is not None and not isinstance(escalation_on_warning, dict):
            escalation_on_warning = None
        await sla_clock_repo.add(
            SlaClock.start(
                tenant_id=tenant_id,
                case_id=case_id,
                clock_key=clock_key,
                started_at=now,
                due_at=now + timedelta(hours=hours),
                escalation_on_breach=escalation_on_breach,
                escalation_on_warning=escalation_on_warning,
            )
        )
        live_keys.add(clock_key)
        started.append(clock_key)
    return started


def _iter_set_fields(actions: object) -> Iterator[tuple[str, str, Any]]:
    """Yield ``(kind, field, value)`` for well-formed ``set_field`` actions.

    ``kind`` is ``"top"`` for ``title``/``description``/``priority``
    and ``"data"`` for structured-data keys. Malformed actions are
    skipped here so :func:`_apply_set_field_actions` stays within the
    branch budget (publish validation is the gate; runtime is
    defense-in-depth).
    """
    if not isinstance(actions, (list, tuple)):
        return
    for action in actions:
        if not isinstance(action, dict) or action.get("type") != "set_field":
            continue
        field = action.get("field")
        if not isinstance(field, str) or not field.strip():
            continue
        if field in {"title", "description", "priority"}:
            yield "top", field, action.get("value")
        else:
            yield "data", field, action.get("value")


async def _apply_set_field_actions(
    case: Case,
    actions: object,
    *,
    tenant_id: UUID,
    case_type_version_repo: CaseTypeVersionRepository | None,
    now: datetime,
) -> tuple[Case, dict[str, Any]]:
    """Apply ``set_field`` transition actions to the case aggregate.

    ``{"type": "set_field", "field": "<name>", "value": <v>}`` writes a
    top-level field (``title`` / ``description`` / ``priority``) or a
    structured-data key (anything else) via :meth:`Case.edit`, which
    keeps optimistic-concurrency and field validation intact. Data-key
    writes need the case's type version for schema validation; without
    a version repository they are skipped (top-level writes still
    apply). Actions without a string ``field`` are skipped (publish
    validation is the gate; runtime is defense-in-depth, matching
    ``create_task``).

    All writes fold into a *single* ``edit`` call: the repository's
    compare-and-swap accepts exactly one token bump per UPDATE, so
    sequential edits would self-conflict. If the combined edit is
    rejected (e.g. an off-enum priority or a schema-violating data
    write), no field lands — the transition still succeeds for the
    state move.

    Returns the updated case and the applied ``{field: value}`` map for
    the audit payload.
    """
    top: dict[str, Any] = {}
    data_writes: dict[str, Any] = {}
    for kind, name, value in _iter_set_fields(actions):
        if kind == "top":
            top[name] = value
        else:
            data_writes[name] = value
    if not top and not data_writes:
        return case, {}
    version = None
    if data_writes and case_type_version_repo is not None:
        version = await case_type_version_repo.get(tenant_id, case.case_type_version_id)
    kwargs: dict[str, Any] = {**top, "now": now}
    if data_writes:
        if version is None:
            # No version to validate against: data writes are skipped
            # while top-level writes still apply below.
            if not top:
                return case, {}
            data_writes = {}
        else:
            kwargs["data"] = {**case.data, **data_writes}
            kwargs["case_type_version"] = version
    try:
        result = case.edit(expected_version_token=case.version_token, **kwargs)
    except (ValidationFailed, InvariantViolation):
        return case, {}
    return result, {**top, **data_writes}


def _new_correlation_id() -> str:
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())
