"""WorkflowDefinition and WorkflowVersion aggregates.

A ``WorkflowDefinition`` is the named, tenant-scoped template (e.g.,
``"ServiceRequest"``). A ``WorkflowVersion`` is an immutable, published
snapshot of its states and transitions. New versions are created by
drafting a new ``WorkflowVersion`` against the same WorkflowDefinition;
existing workflow instances (bound to a case via ``workflow_instances``)
retain the version they were started on (P02-US2 / FM-013 / FR-015).

# Lifecycle (FM-013 / P02-US2 / P10-US1)

``WorkflowVersion.status`` is one of:

- ``draft`` — being authored. Mutable.
- ``published`` — frozen. The application layer rejects any attempt to
  mutate ``definition_json``, status, or metadata on a published version.
  The repository treats UPDATE attempts on a published version as a bug.
- ``retired`` — historical. New workflow instances may not be started
  against a retired version; existing instances continue to execute
  against the version they were pinned to (P02 pillar §2).

# Definition shape (AGENTS.md §16.2, plan §5 Workflow Engine)

The version's ``definition_json`` is a JSON document with two top-level
keys:

- ``states``: list of state definitions. Each state carries a stable
  machine ``key``, a human ``label``, and optional flags ``is_initial``
  / ``is_terminal``.
- ``transitions``: list of allowed moves between states. Each transition
  carries a stable ``key``, a ``from`` state, a ``to`` state, an
  optional declarative ``guard`` (Pillar P05 §2 — same operator set as
  CaseType rules), an optional list of ``actions`` (configurable
  effects; the engine that interprets them is a later slice), and a
  ``human_driven`` flag.

The ``human_driven`` flag distinguishes cycles the workflow author
considers breakable by a human or external event from cycles that fire
automatically with no breakable guard. The validator (see
``application/configuration/validate_definition_workflow.py``) accepts
human-driven cycles and rejects automatic-only cycles that would trap
a case without termination (plan §5).

# Composition (FM-019 / P02-US7, P02-US8)

A ``WorkflowVersion`` may optionally carry:

- ``subprocesses``: reusable fragments (a fragment is itself a small
  state machine referenced by ``key``). A transition may invoke a
  subprocess by setting ``subprocess_key``; the engine mounts the
  fragment in-place and the called fragment's initial state receives
  the case at the moment of the calling transition. Subprocesses do
  not introduce a new persistence row — they live alongside the
  version's own ``states`` / ``transitions`` and the validator checks
  that the keys are unique and that any referenced subprocess is
  declared.
- ``branches``: parallel composition. A transition may declare
  ``parallel=...`` with a list of ``BranchSpec``s; each branch lists
  the ``from_state`` it activates when the transition fires and an
  optional ``terminal_state`` that marks that branch as complete. The
  transition also carries a ``join`` (:class:`JoinSpec`) which defines
  when the parallel section is considered complete and which state the
  case moves to next. ``JoinSpec.mode='all'`` (default) requires every
  declared branch to reach its terminal before the join fires;
  ``'any'`` fires as soon as one branch reaches its terminal; ``'count'``
  fires after exactly ``min_completions`` branches have reached their
  terminal states (the remaining branches are abandoned).

The validator (FM-019) rejects:

- subprocesses with duplicate keys,
- transitions whose ``subprocess_key`` references an undeclared
  fragment,
- parallel branches whose ``from_state`` or ``terminal_state`` is not
  declared on the version,
- a join whose ``mode='count'`` carries ``min_completions`` outside
  ``[1, len(branches)]``,
- parallel transitions whose terminal states cannot themselves reach a
  workflow terminal (they would trap a branch and the join would
  never fire under ``mode='all'``).

INTEGRATION_SEAM: ``WorkflowDefinitionRepository`` and
``WorkflowVersionRepository`` are domain ports
(``app/domain/ports.py``). The local adapters live in
``app/infrastructure/persistence/repositories/`` and are wired through
``app/deps.py``. The Azure production adapters are a future module
behind the same Protocols.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation


class WorkflowVersionStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"


class JoinMode(StrEnum):
    """How a parallel section's branches come back together.

    - ``ALL`` (default): the join fires only after **every** declared
      branch has reached its declared ``terminal_state``. Any branch
      that fails to do so leaves the parallel section open and the
      case is stuck in the parallel span.
    - ``ANY``: the join fires as soon as **one** branch reaches its
      declared terminal state. The remaining branches are abandoned
      (no further transitions fire from them).
    - ``COUNT``: the join fires after exactly ``min_completions``
      branches have reached their declared terminal states. The
      remaining branches are abandoned.

    Why three modes rather than only ``ALL``: real processes often need
    "first-finished-wins" or "k-of-n" joins (e.g. one approver out of
    three is enough, or two independent reviews must complete).
    """

    ALL = "all"
    ANY = "any"
    COUNT = "count"


@dataclass(frozen=True, slots=True)
class Subprocess:
    """A reusable, named fragment of a workflow (P02-US7 / FM-019).

    A subprocess is itself a small state machine: a set of ``states``
    and ``transitions``. The author may invoke a subprocess from any
    transition by setting ``WorkflowTransition.subprocess_key``; the
    engine mounts the fragment at the calling transition's
    ``to_state`` (treated as the subprocess entry) and the fragment's
    exit transitions bring the case back to the rest of the workflow
    (the validator treats the fragment's exit states the same way it
    treats ordinary ``is_terminal`` states).

    The fragment is purely declarative — no separate version row, no
    audit row distinct from the calling transition. Subprocesses are
    versioned implicitly with the workflow version that declares them.
    """

    key: str
    states: tuple[WorkflowState, ...]
    transitions: tuple[WorkflowTransition, ...]

    def __post_init__(self) -> None:
        if not self.key or not self.key.replace("_", "").replace("-", "").isalnum():
            raise InvariantViolation(f"invalid subprocess key: {self.key!r}")
        if not self.states:
            raise InvariantViolation(f"subprocess {self.key!r} requires at least one state")
        keys = [s.key for s in self.states]
        if len(set(keys)) != len(keys):
            raise InvariantViolation(f"subprocess {self.key!r} has duplicate state keys")
        trans_keys = [t.key for t in self.transitions]
        if len(set(trans_keys)) != len(trans_keys):
            raise InvariantViolation(f"subprocess {self.key!r} has duplicate transition keys")


@dataclass(frozen=True, slots=True)
class BranchSpec:
    """A single parallel branch attached to a transition.

    Each branch declares the ``from_state`` it activates when the
    parallel transition fires and the ``terminal_state`` that marks
    the branch as complete (the join uses this signal). ``branch_key``
    is the stable identifier the runtime uses to track branch
    completion independently of state keys.
    """

    branch_key: str
    from_state: str
    terminal_state: str

    def __post_init__(self) -> None:
        if not self.branch_key or not self.branch_key.replace("_", "").replace("-", "").isalnum():
            raise InvariantViolation(f"invalid branch key: {self.branch_key!r}")
        if not self.from_state:
            raise InvariantViolation(f"branch {self.branch_key!r} requires from_state")
        if not self.terminal_state:
            raise InvariantViolation(f"branch {self.branch_key!r} requires terminal_state")
        if self.from_state == self.terminal_state:
            raise InvariantViolation(
                f"branch {self.branch_key!r} cannot have from_state == "
                "terminal_state (it would never report completion)"
            )


@dataclass(frozen=True, slots=True)
class JoinSpec:
    """How a parallel section's branches join (P02-US8 / FM-019).

    ``target_state`` is the state the case moves to once the join
    fires. ``mode`` is one of :class:`JoinMode`. ``min_completions``
    is required only for ``mode='count'`` and must lie in
    ``[1, len(branches)]``.
    """

    target_state: str
    mode: JoinMode = JoinMode.ALL
    min_completions: int | None = None

    def __post_init__(self) -> None:
        if not self.target_state:
            raise InvariantViolation("join requires a target_state")
        if self.mode is JoinMode.COUNT and (
            self.min_completions is None or self.min_completions < 1
        ):
            raise InvariantViolation("join mode 'count' requires min_completions >= 1")


@dataclass(frozen=True, slots=True)
class WorkflowState:
    """A single state in a workflow version's state machine.

    Carries a stable machine ``key``, a human ``label``, and optional
    flags. ``is_initial`` marks the entry state for new workflow
    instances (exactly one is required per definition); ``is_terminal``
    marks a state that has no outgoing transitions (zero or more are
    allowed, but the validator insists the state machine is reachable
    and that every non-terminal state can reach a terminal state).
    """

    key: str
    label: str
    is_initial: bool = False
    is_terminal: bool = False

    def __post_init__(self) -> None:
        if not self.key or not self.key.replace("_", "").replace("-", "").isalnum():
            raise InvariantViolation(f"invalid state key: {self.key!r}")
        if not self.label.strip():
            raise InvariantViolation(f"state label is required for {self.key!r}")
        if self.is_initial and self.is_terminal:
            raise InvariantViolation(f"state {self.key!r} cannot be both initial and terminal")


@dataclass(frozen=True, slots=True)
class WorkflowTransition:
    """A single configured transition between two states.

    Carries a stable ``key`` (the API name a caller submits), the
    ``from`` and ``to`` state keys, an optional declarative ``guard``
    document (Pillar P05 §2 — the same operator set as CaseType rules),
    optional ``actions`` (configured effects; execution lands in a
    later slice), a ``human_driven`` flag, and optional approval /
    separation-of-duties flags (P02-US4 / FM-017 / P11-US5).

    Why ``human_driven`` lives on the transition: the validator uses it
    to tell a breakable cycle (a reviewer can decide to break the loop)
    from a runaway automatic cycle (no human or external event can
    interrupt the loop, so the case is trapped forever — plan §5).
    """

    key: str
    from_state: str
    to_state: str
    human_driven: bool = False
    guard: dict[str, Any] | None = None
    actions: tuple[dict[str, Any], ...] = ()
    subprocess_key: str | None = None
    branches: tuple[BranchSpec, ...] = ()
    join: JoinSpec | None = None
    # Approval / separation-of-duties configuration (P02-US4 / FM-017 /
    # P11-US5). When ``approval_required`` is True the transition is
    # "consequential" and the engine refuses self-approval: the actor
    # must differ from the case's ``created_by`` (originator). The
    # ``eligible_approver_roles`` list is the (optional) role gate the
    # approver must hold; an empty tuple means "any role with the
    # ``case.transition`` grant who is not the originator". The runtime
    # stamps both the SoD outcome (allowed / denied) and the reason
    # onto the ``WorkflowTransitioned`` audit row so the timeline
    # carries an explainable record of every consequential decision
    # (P09 / Constitution V).
    approval_required: bool = False
    eligible_approver_roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.key or not self.key.replace("_", "").replace("-", "").isalnum():
            raise InvariantViolation(f"invalid transition key: {self.key!r}")
        if not self.from_state or not self.to_state:
            raise InvariantViolation(f"transition {self.key!r} requires from_state and to_state")
        if self.from_state == self.to_state:
            raise InvariantViolation(f"transition {self.key!r} cannot be a self-loop")
        if self.subprocess_key is not None and self.branches:
            raise InvariantViolation(
                f"transition {self.key!r} cannot declare both a "
                "subprocess_key and parallel branches"
            )
        if (self.branches and self.join is None) or (self.join is not None and not self.branches):
            raise InvariantViolation(
                f"transition {self.key!r} must declare both branches and join, or neither"
            )
        if self.branches:
            keys = [b.branch_key for b in self.branches]
            if len(set(keys)) != len(keys):
                raise InvariantViolation(f"transition {self.key!r} has duplicate branch keys")
        if self.approval_required and any(
            not role or not role.replace("_", "").replace("-", "").isalnum()
            for role in self.eligible_approver_roles
        ):
            raise InvariantViolation(f"transition {self.key!r} has invalid eligible_approver_roles")


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """A named workflow template (e.g., ``"ServiceRequest"``).

    A ``WorkflowDefinition`` aggregates many ``WorkflowVersion``s. Only
    one is marked as the current (effective) version; that is the
    version new workflow instances pin at creation time. Existing
    workflow instances remain bound to their original version
    (FM-013 / P02-US2).
    """

    id: UUID
    tenant_id: UUID
    key: str
    name: str
    description: str = ""
    current_version_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.key or not self.key.replace("_", "").replace("-", "").isalnum():
            raise InvariantViolation(f"invalid workflow key: {self.key!r}")
        if not self.name.strip():
            raise InvariantViolation("workflow name is required")

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        key: str,
        name: str,
        description: str = "",
        now: datetime | None = None,
    ) -> WorkflowDefinition:
        return WorkflowDefinition(
            id=uuid4(),
            tenant_id=tenant_id,
            key=key,
            name=name,
            description=description,
            current_version_id=None,
            created_at=now,
            updated_at=now,
        )

    def with_published_version(self, version: WorkflowVersion) -> WorkflowDefinition:
        """Promote a published version to the WorkflowDefinition's current version.

        Validates tenant ownership and that the version belongs to this
        WorkflowDefinition. The result carries an updated ``updated_at``
        so the audit row can read it.
        """
        if version.tenant_id != self.tenant_id:
            raise InvariantViolation("tenant mismatch on workflow version")
        if version.workflow_definition_id != self.id:
            raise InvariantViolation("version does not belong to this workflow")
        if version.status is not WorkflowVersionStatus.PUBLISHED:
            raise InvariantViolation("only published versions can be set as current")
        return replace(
            self,
            current_version_id=version.id,
            updated_at=version.published_at,
        )


@dataclass(frozen=True, slots=True)
class WorkflowVersion:
    """A single version of a workflow's state machine.

    Lifecycle: ``draft`` → ``published`` → ``retired``. ``publish()`` is
    irreversible: the resulting instance is frozen with a ``published_at``
    timestamp and a ``published_by`` actor. Once published, the
    version's ``definition_json`` is immutable — any subsequent mutation
    attempt raises ``InvariantViolation`` (FR-015, AC-007).
    """

    id: UUID
    tenant_id: UUID
    workflow_definition_id: UUID
    version_number: int
    states: tuple[WorkflowState, ...]
    transitions: tuple[WorkflowTransition, ...]
    status: WorkflowVersionStatus
    subprocesses: tuple[Subprocess, ...] = ()
    published_at: datetime | None = None
    published_by: UUID | None = None
    created_at: datetime | None = None

    # ----- factories --------------------------------------------------------

    @staticmethod
    def draft(
        *,
        tenant_id: UUID,
        workflow_definition_id: UUID,
        version_number: int,
        states: tuple[WorkflowState, ...],
        transitions: tuple[WorkflowTransition, ...],
        subprocesses: tuple[Subprocess, ...] = (),
        now: datetime | None = None,
    ) -> WorkflowVersion:
        if version_number < 1:
            raise InvariantViolation("version_number must be >= 1")
        if not states:
            raise InvariantViolation("workflow definition requires at least one state")
        keys = [s.key for s in subprocesses]
        if len(set(keys)) != len(keys):
            raise InvariantViolation("duplicate subprocess keys")
        return WorkflowVersion(
            id=uuid4(),
            tenant_id=tenant_id,
            workflow_definition_id=workflow_definition_id,
            version_number=version_number,
            states=states,
            transitions=transitions,
            subprocesses=subprocesses,
            status=WorkflowVersionStatus.DRAFT,
            published_at=None,
            published_by=None,
            created_at=now,
        )

    # ----- transitions ------------------------------------------------------

    def publish(self, *, actor_id: UUID, at: datetime) -> WorkflowVersion:
        """Transition ``draft`` → ``published``. Irreversible.

        Returns a new immutable instance with the ``published_at`` and
        ``published_by`` fields stamped. Raises ``InvariantViolation``
        if the version is not currently in ``draft`` status.
        """
        if self.status is not WorkflowVersionStatus.DRAFT:
            raise InvariantViolation(f"cannot publish a {self.status.value} workflow version")
        return WorkflowVersion(
            id=self.id,
            tenant_id=self.tenant_id,
            workflow_definition_id=self.workflow_definition_id,
            version_number=self.version_number,
            states=self.states,
            transitions=self.transitions,
            subprocesses=self.subprocesses,
            status=WorkflowVersionStatus.PUBLISHED,
            published_at=at,
            published_by=actor_id,
            created_at=self.created_at,
        )

    def retire(self) -> WorkflowVersion:
        """Transition ``published`` → ``retired``. Irreversible.

        Retired versions are historical; no new workflow instances may
        be created against them, but existing instances continue to
        execute against the version they were pinned to (P02 pillar
        §2 — running cases remain pinned).
        """
        if self.status is not WorkflowVersionStatus.PUBLISHED:
            raise InvariantViolation(
                f"only published versions can be retired (got {self.status.value})"
            )
        return WorkflowVersion(
            id=self.id,
            tenant_id=self.tenant_id,
            workflow_definition_id=self.workflow_definition_id,
            version_number=self.version_number,
            states=self.states,
            transitions=self.transitions,
            subprocesses=self.subprocesses,
            status=WorkflowVersionStatus.RETIRED,
            published_at=self.published_at,
            published_by=self.published_by,
            created_at=self.created_at,
        )

    # ----- immutability guard ----------------------------------------------

    def assert_mutable(self) -> None:
        """Raise if the version is not in ``draft`` status.

        Used by repository helpers that guard against accidental UPDATE
        on a published version (FR-015). Database-level immutability is
        added in a later phase via a trigger + ORM listener; the domain
        invariant is the primary mechanism.
        """
        if self.status is not WorkflowVersionStatus.DRAFT:
            raise InvariantViolation(
                f"workflow version is {self.status.value} and cannot be modified"
            )

    # ----- accessors --------------------------------------------------------

    def state_keys(self) -> tuple[str, ...]:
        return tuple(s.key for s in self.states)

    def transition_keys(self) -> tuple[str, ...]:
        return tuple(t.key for t in self.transitions)

    def outgoing(self, state_key: str) -> tuple[WorkflowTransition, ...]:
        return tuple(t for t in self.transitions if t.from_state == state_key)

    def subprocess_keys(self) -> tuple[str, ...]:
        return tuple(s.key for s in self.subprocesses)

    def get_subprocess(self, key: str) -> Subprocess | None:
        """Return the subprocess with ``key`` or ``None`` if undeclared.

        Used by the validator and the runtime join engine.
        """
        for sub in self.subprocesses:
            if sub.key == key:
                return sub
        return None

    def parallel_transitions(self) -> tuple[WorkflowTransition, ...]:
        """Return every transition that declares ``branches``.

        Used by the parallel-join runtime to enumerate parallel
        sections and by the validator to check join shape.
        """
        return tuple(t for t in self.transitions if t.branches)


@dataclass(frozen=True, slots=True)
class WorkflowTransitionRecord:
    """One executed transition (T069 runtime log row).

    Distinct from the config-level :class:`WorkflowTransition` (the
    declared edge): this is the *fact* that the edge fired for a
    workflow instance, with actor, timestamp, and the action inputs.
    Append-only — corrections are compensating ``WorkflowTransitioned``
    audit rows, never UPDATE/DELETE here.
    """

    id: UUID
    tenant_id: UUID
    workflow_instance_id: UUID
    from_state: str
    to_state: str
    transition_key: str
    actor_id: UUID
    occurred_at: datetime
    payload: dict[str, Any]
    correlation_id: str
    causation_id: str | None = None


__all__ = [
    "BranchSpec",
    "JoinMode",
    "JoinSpec",
    "Subprocess",
    "WorkflowDefinition",
    "WorkflowState",
    "WorkflowTransition",
    "WorkflowTransitionRecord",
    "WorkflowVersion",
    "WorkflowVersionStatus",
]
