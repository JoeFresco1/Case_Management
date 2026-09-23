"""Create a new case (P01-US1, FM-005).

The use case is the canonical atomic case-creation flow:

1. **Authorize** the actor against `Action.CASE_CREATE` on the target
   tenant (P11-US2, FM-002). Cross-tenant actors are denied
   unconditionally — this is belt-and-suspenders on top of the explicit
   `tenant_id` filter in the repository.
2. **Validate required fields** against the published `CaseTypeVersion`.
   A missing required field raises `ValidationFailed(field=...)` which
   the HTTP layer maps to 422 (FR-001 acceptance scenario 2).
3. **Generate a monotonic reference** (`C-{TENANT_SHORT}-{YYYY}-{SEQ:06d}`)
   through `CaseRepository.next_reference`.
4. **Construct** the `Case` aggregate in the initial state.
5. **Persist case + audit + outbox** in the *same* DB transaction:
   `cases`, `audit_events` (with chained SHA-256), `outbox`
   (`CaseCreated` event). No partial commit; the boundary commits once.
6. **Idempotency**: the HTTP layer (`IdempotencyMiddleware`) caches the
   response under `Idempotency-Key`. The use case itself does not need
   to know about keys.
7. **Workflow start** (P02-US1 / FM-014 / T070): when the caller supplies
   a `WorkflowStarter` bundle and a published workflow definition exists
   under the `{case_type_key}Workflow` convention key, the workflow
   instance, welcome tasks, and SLA clocks join the same transaction via
   `start_workflow.handle`, and the case carries the initial state. No
   matching workflow means "not configured": intake succeeds without one.

Seams (AGENTS.md §12):

- Audit store: `domain/ports.AuditEventStore` -> `SqlAuditEventStore`.
- Outbox: `domain/ports.OutboxWriter` -> `SqlOutboxWriter`.
- Reference generator: `domain/ports.CaseRepository.next_reference` ->
  `SqlCaseRepository.next_reference` (looks up the tenant short key via
  `SqlTenantRepository`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.application.cases.compute_derived import compute_derived_fields
from app.application.workflow import start_workflow as start_workflow_uc
from app.domain.audit import CASE_CREATED
from app.domain.case import Case
from app.domain.errors import NotFound
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    CaseTypeRepository,
    CaseTypeVersionRepository,
    OutboxWriter,
    SlaClockRepository,
    TaskRepository,
    WorkflowDefinitionRepository,
    WorkflowInstanceRepository,
    WorkflowVersionRepository,
)
from app.domain.workflow import WorkflowVersion, WorkflowVersionStatus

# Subjects for audit + outbox payloads. Stable strings; subscribers
# depend on them.
SUBJECT_TYPE_CASE = "case"


class _Clock(Protocol):
    """Provide the current UTC timestamp to the case-creation use case."""

    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class CreateCaseCommand:
    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_type_key: str
    title: str
    description: str
    data: dict[str, Any]
    priority: str = "normal"
    correlation_id: str = ""
    causation_id: str | None = None
    initial_workflow_state: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowStarter:
    """Optional workflow ports for case intake (P02-US1 / FM-014 / T070).

    When supplied, ``handle`` starts the tenant's workflow for the new
    case in the same transaction (instance + welcome tasks + SLA clocks
    + ``WorkflowStarted`` audit/outbox). When ``None`` the case is
    created without a workflow — unit tests for intake validation use
    this shape so they do not need workflow fakes.
    """

    workflow_repo: WorkflowDefinitionRepository
    workflow_version_repo: WorkflowVersionRepository
    instance_repo: WorkflowInstanceRepository
    task_repo: TaskRepository
    sla_clock_repo: SlaClockRepository | None = None


def workflow_key_for_case_type(case_type_key: str) -> str:
    """Return the v1 workflow-definition key for a case type.

    Convention (T077 seed vertical): the workflow that governs a case
    type is keyed ``{case_type_key}Workflow`` (e.g. ``ServiceRequest``
    -> ``ServiceRequestWorkflow``). A case type with no matching
    workflow definition creates cases without a workflow instance.
    """
    return f"{case_type_key}Workflow"


async def handle(
    cmd: CreateCaseCommand,
    *,
    case_repo: CaseRepository,
    case_type_repo: CaseTypeRepository,
    case_type_version_repo: CaseTypeVersionRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: _Clock,
    workflow: WorkflowStarter | None = None,
) -> Case:
    """Create a case atomically with audit + outbox (and workflow start).

    Failure behavior:
    - Validation/auth/not-found errors are raised before any DB write.
    - The case insert, audit row, and outbox row share one transaction;
      the caller commits once.
    - If the audit or outbox write fails the case insert is rolled back.
    - When ``workflow`` is supplied and a published workflow definition
      exists for the case type, the workflow instance, welcome tasks,
      and SLA clocks join the same transaction via
      ``start_workflow.handle``; a misconfigured (unusable) workflow
      raises ``NotFound`` and the whole intake rolls back.
    """
    # 1. Authorization. The policy raises `Forbidden` on deny; the HTTP
    # layer maps it to 403.
    await authorize(
        Action.CASE_CREATE,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="case_type", id=None),
    )

    # 2. Resolve the published case type version. A missing case type
    # within the actor's tenant is a NotFound; a case type that exists
    # only in *another* tenant must surface as Forbidden so we never
    # leak the existence of cross-tenant configuration (Constitution V).
    case_type = await case_type_repo.get_by_key(cmd.tenant_id, cmd.case_type_key)
    if case_type is None:
        raise NotFound(f"case type {cmd.case_type_key!r} not found")
    if case_type.current_version_id is None:
        raise NotFound(f"case type {cmd.case_type_key!r} has no published version")
    version = await case_type_version_repo.get(cmd.tenant_id, case_type.current_version_id)
    if version is None:
        raise NotFound(f"published case type version {case_type.current_version_id} not found")

    # 3. Reference + initial state. `Case.create` validates required
    # fields (raises ValidationFailed) and tenant match. The case type
    # key feeds the tenant's reference template (FM-009 `{TYPE}`).
    now = clock.now()
    reference = await case_repo.next_reference(cmd.tenant_id, now=now, type_key=case_type.key)

    # 3a. Workflow pre-resolution (T070). When a workflow bundle is
    # supplied, resolve the published definition *before* staging the
    # case so a misconfigured workflow fails intake instead of
    # stranding a workflow-less case. Absent definition (or no
    # published version) means "no workflow configured": the case is
    # created without one.
    workflow_key: str | None = None
    initial_state: str | None = None
    if workflow is not None:
        workflow_key = workflow_key_for_case_type(cmd.case_type_key)
        definition = await workflow.workflow_repo.get_by_key(cmd.tenant_id, workflow_key)
        if definition is not None and definition.current_version_id is not None:
            wf_version = await workflow.workflow_version_repo.get(
                cmd.tenant_id, definition.current_version_id
            )
            if wf_version is not None and wf_version.status is WorkflowVersionStatus.PUBLISHED:
                initial_state = _initial_state_key(wf_version)

    case = Case.create(
        tenant_id=cmd.tenant_id,
        reference=reference,
        case_type_version=version,
        title=cmd.title,
        description=cmd.description,
        data=cmd.data,
        priority=cmd.priority,
        created_by=cmd.actor_id,
        now=now,
        sensitivity=case_type.sensitivity_default,
    )
    # FM-012 / P05-US6: replace caller-supplied derived field values with
    # the schema-computed values so the stored data is the contract.
    case = replace(case, data=compute_derived_fields(case.data, version.schema))

    # If the caller supplied an initial workflow state, attach it now.
    # Otherwise attach the resolved workflow initial state (if any).
    # `set_workflow_state` on a fresh case keeps `version_token=1`
    # (see `Case.set_workflow_state`), so the assignment is part of
    # creation and is staged by the single `add` below.
    resolved_state = cmd.initial_workflow_state or initial_state
    if resolved_state:
        case = case.set_workflow_state(state=resolved_state, now=now)

    # 4. Persist (case + audit + outbox) in the same transaction.
    # The repository `add` only stages the row on the session; the
    # caller commits once.
    await case_repo.add(case)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=CASE_CREATED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_CASE,
        subject_id=case.id,
        case_id=case.id,
        correlation_id=cmd.correlation_id or _new_correlation_id(),
        causation_id=cmd.causation_id,
        payload={
            "reference": case.reference.value,
            "case_type_key": cmd.case_type_key,
            "case_type_version_id": str(case.case_type_version_id),
            "priority": case.priority,
            "title": case.title,
            "status": case.status.value,
            "initial_workflow_state": case.current_workflow_state,
        },
        occurred_at=now,
    )

    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=CASE_CREATED,
        event_id=uuid4(),
        payload={
            "case_id": str(case.id),
            "reference": case.reference.value,
            "case_type_key": cmd.case_type_key,
            "tenant_id": str(cmd.tenant_id),
            "actor_id": str(cmd.actor_id),
            "occurred_at": now.isoformat(),
        },
    )

    # 5. Workflow start (T070). The instance, welcome tasks, SLA clocks,
    # and `WorkflowStarted` audit/outbox join the caller's transaction —
    # `start_workflow.handle` only stages rows; the HTTP layer commits
    # once. `initial_state is not None` implies the definition + published
    # version resolved above, so the delegate's `NotFound` paths are
    # unreachable here (defense-in-depth, not control flow).
    if workflow is not None and workflow_key is not None and initial_state is not None:
        await start_workflow_uc.handle(
            start_workflow_uc.StartWorkflowCommand(
                tenant_id=cmd.tenant_id,
                actor_id=cmd.actor_id,
                case_id=case.id,
                workflow_key=workflow_key,
                correlation_id=cmd.correlation_id or _new_correlation_id(),
            ),
            workflow_repo=workflow.workflow_repo,
            workflow_version_repo=workflow.workflow_version_repo,
            instance_repo=workflow.instance_repo,
            task_repo=workflow.task_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
            sla_clock_repo=workflow.sla_clock_repo,
        )

    return case


# ----- helpers --------------------------------------------------------------


def _initial_state_key(version: WorkflowVersion) -> str | None:
    """Return the initial state key of a workflow version, if any.

    The publish-time validator guarantees exactly one initial state;
    a version without one is treated as unusable (no workflow start)
    rather than failing intake.
    """
    for state in version.states:
        if state.is_initial:
            return state.key
    return None


def _user_context(cmd: CreateCaseCommand) -> Any:
    """Build a minimal `UserContext`-shaped object for the policy.

    The application layer does not import `app.deps.UserContext` (that
    would pull FastAPI into the domain/application boundary). Instead
    we use duck typing: `authorize` only reads `tenant_id`, `roles`,
    and `is_system`.
    """
    return _UserProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )


@dataclass(frozen=True, slots=True)
class _UserProxy:
    """Duck-typed `UserContext` for the policy.

    Why a local dataclass: the policy's `evaluate` reads three fields
    (``tenant_id``, ``roles``, ``is_system``) and we want `create_case`
    to be unit-testable without dragging in `app.deps` (which itself
    imports FastAPI).
    """

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


def _new_correlation_id() -> str:
    # PyPI name ``uuid7`` ships its module as ``uuid_extensions``
    # (uuid7 was a placeholder during early publishing). Lazy import
    # keeps cold-start light and survives a missing pkg.
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())


__all__ = [
    "CreateCaseCommand",
    "WorkflowStarter",
    "handle",
    "workflow_key_for_case_type",
]
