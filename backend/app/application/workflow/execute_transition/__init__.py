"""Execute a workflow transition (P02-US1 / FM-014 / FR-009 / FR-010 / T071).

The transition engine is the canonical workflow behavior in the first
slice. It does five things, in order:

1. **Authorize** the actor against ``Action.CASE_TRANSITION`` on the
   target case (P11-US2 / FM-002). Cross-tenant actors are denied
   unconditionally — the repository never matches a case from another
   tenant so the policy is belt-and-suspenders on the actor's own
   tenant_id.
2. **Resolve the case** (tenant-scoped) and the case's
   ``WorkflowInstance``. Missing or cross-tenant ids surface as
   ``NotFound``; the two cases are indistinguishable so the API never
   confirms cross-tenant existence (Constitution V).
3. **Pick the transition** by ``transition_key`` from the published
   ``WorkflowVersion`` the instance is pinned to. A key that does not
   match an outgoing transition from the instance's current state
   raises ``UnsupportedTransition`` (HTTP 409 ``unsupported_transition``)
   so the caller can distinguish "I asked for the wrong key" from
   "the guard rejected it".
4. **Evaluate the guard** (deterministic, pure function —
   ``app.domain.rules``). A failed guard raises
   :class:`GuardBlocked` with a safe explanation that names the
   missing requirement (FR-010) — never the data the actor cannot see.
5. **Apply the transition atomically** — the WorkflowInstance
   pointer, the configured ``create_task`` / ``set_field`` /
   ``start_sla`` actions, the ``WorkflowTransitioned`` audit row, and
   the outbox event all share one transaction. If any step raises,
   the boundary rolls back so the case never lands in a partial
   transition state (Constitution III).

# Idempotency (FR-011 / AGENTS.md §10)

The HTTP ``Idempotency-Key`` middleware replays the cached response on
a duplicate request. The use case itself is **not** idempotent at the
domain layer — the instance pointer moves every call. The middleware
is the canonical replay mechanism; a duplicate call without the
cached response lands as a second transition, which is correct
behavior for a retry that lost its 200.

# INTEGRATION_SEAM: ports

- ``WorkflowInstanceRepository`` — instance pointer
  (``app/domain/ports.py``).
- ``WorkflowVersionRepository`` — read the published version the
  instance is pinned to.
- ``TaskRepository`` — materializes the transition's ``create_task``
  actions.
- ``CaseRepository`` — updates the case's ``current_workflow_state`` so
  the read path reflects the new state without a join.
- ``AuditEventStore`` + ``OutboxWriter`` — the immutable audit + outbox
  pair.

# Package layout (R2 split)

Implementation lives in sibling submodules — ``_types`` (commands,
results, ``GuardBlocked``, ``evaluate_join``), ``_guards`` (guard
context/evaluation, state-bound form gate), ``_actions``
(``create_task`` / ``set_field`` / ``start_sla`` materializers),
``_sod`` (separation-of-duties), ``handle`` (the orchestrator). The
names below are re-exported so every pre-split import path
(``from app.application.workflow.execute_transition import ...``,
including private helpers used by ``preflight_transitions``) keeps
working unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.application.workflow.execute_transition._actions import (
    _apply_set_field_actions,
    _as_queue,
    _as_uuid,
    _fire_start_sla_actions,
    _iter_set_fields,
    _materialize_create_task_actions,
    _new_correlation_id,
    _positive_hours,
    _tasks_for_case,
)
from app.application.workflow.execute_transition._guards import (
    _build_guard_context,
    _document_counts,
    _enforce_state_form_gate,
    _evaluate_document_guard,
    _evaluate_transition_guard,
    _guard_failure_details,
    _guard_failure_reason,
    _guard_requires_documents,
    _is_document_requirements_guard,
)
from app.application.workflow.execute_transition._sod import (
    _SYSTEM_ROLE_MARKER,
    _evaluate_separation_of_duties,
    _is_system,
)
from app.application.workflow.execute_transition._types import (
    SUBJECT_TYPE_CASE,
    SUBJECT_TYPE_WORKFLOW_INSTANCE,
    ExecuteTransitionCommand,
    ExecuteTransitionResult,
    GuardBlocked,
    JoinDecision,
    _ClockLike,
    evaluate_join,
)
from app.application.workflow.execute_transition.handle import (
    _actor_proxy,
    _ActorProxy,
    _find_transition,
    _record_transition,
    handle,
)
from app.domain.audit import ACCESS_DECISION_RECORDED, WORKFLOW_TRANSITIONED
from app.domain.case import Case
from app.domain.errors import (
    Forbidden,
    InvariantViolation,
    NotFound,
    UnsupportedTransition,
    ValidationFailed,
)
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    CaseTypeVersionRepository,
    DocumentRepository,
    OutboxWriter,
    SlaClockRepository,
    TaskRepository,
    WorkflowInstanceRepository,
    WorkflowTransitionLog,
    WorkflowVersionRepository,
)
from app.domain.rules import RuleEvaluationError, evaluate
from app.domain.sla import SlaClock, SlaClockStatus
from app.domain.task import Task, TaskStatus
from app.domain.workflow import (
    JoinMode,
    WorkflowTransition,
    WorkflowTransitionRecord,
    WorkflowVersion,
    WorkflowVersionStatus,
)

# Re-export every pre-split module attribute (public and private) so
# ``dir(execute_transition)`` and all ``from ... import ...`` paths stay
# identical to the flat module. Private names are intentional: module
# docstrings (e.g. ``execute_transition._evaluate_separation_of_duties``)
# and ``preflight_transitions`` reference them.
__all__ = [
    "ACCESS_DECISION_RECORDED",
    "Action",
    "Any",
    "AuditEventStore",
    "Case",
    "CaseRepository",
    "CaseTypeVersionRepository",
    "DocumentRepository",
    "ExecuteTransitionCommand",
    "ExecuteTransitionResult",
    "Forbidden",
    "GuardBlocked",
    "InvariantViolation",
    "Iterable",
    "Iterator",
    "JoinDecision",
    "JoinMode",
    "Mapping",
    "NotFound",
    "OutboxWriter",
    "Protocol",
    "Resource",
    "RuleEvaluationError",
    "SUBJECT_TYPE_CASE",
    "SUBJECT_TYPE_WORKFLOW_INSTANCE",
    "SlaClock",
    "SlaClockRepository",
    "SlaClockStatus",
    "Task",
    "TaskRepository",
    "TaskStatus",
    "UUID",
    "UnsupportedTransition",
    "ValidationFailed",
    "WORKFLOW_TRANSITIONED",
    "WorkflowInstanceRepository",
    "WorkflowTransition",
    "WorkflowTransitionLog",
    "WorkflowTransitionRecord",
    "WorkflowVersion",
    "WorkflowVersionRepository",
    "WorkflowVersionStatus",
    "_ActorProxy",
    "_ClockLike",
    "_SYSTEM_ROLE_MARKER",
    "_actor_proxy",
    "_apply_set_field_actions",
    "_as_queue",
    "_as_uuid",
    "_build_guard_context",
    "_document_counts",
    "_enforce_state_form_gate",
    "_evaluate_document_guard",
    "_evaluate_separation_of_duties",
    "_evaluate_transition_guard",
    "_find_transition",
    "_fire_start_sla_actions",
    "_guard_failure_details",
    "_guard_failure_reason",
    "_guard_requires_documents",
    "_is_document_requirements_guard",
    "_is_system",
    "_iter_set_fields",
    "_materialize_create_task_actions",
    "_new_correlation_id",
    "_positive_hours",
    "_record_transition",
    "_tasks_for_case",
    "annotations",
    "authorize",
    "dataclass",
    "datetime",
    "evaluate",
    "evaluate_join",
    "field",
    "handle",
    "timedelta",
    "uuid4",
]

# why: the import machinery binds each submodule as a package attribute when
# it is first loaded (``_actions``, ``_guards``, ``_sod``, ``_types``). The
# pre-split flat module had no such attributes, so drop them here to keep
# ``dir(execute_transition)`` byte-identical for the refactor gate. The
# modules stay registered in ``sys.modules``, so every explicit
# ``from ..._types import x`` path (and the relative imports inside the
# package) keeps resolving; only the package-level attribute is removed.
# ``globals().pop`` (not ``del <name>``) so mypy strict does not flag the
# names as undefined — they are bound dynamically by the import system.
for _submodule_attr in ("_actions", "_guards", "_sod", "_types"):
    globals().pop(_submodule_attr, None)
del _submodule_attr
