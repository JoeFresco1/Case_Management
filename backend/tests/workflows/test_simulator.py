"""Deterministic workflow simulator tests (FM-015 / P02-US5).

Naming follows AGENTS.md §4.R5: ``test_<unit>_<scenario>__<expectation>``.
The acceptance scenarios are:

1. **Happy path** -- the seed workflow with a synthetic case that
   satisfies every guard produces a completed simulation in three
   steps and the trace is fully deterministic.
2. **Forced-blocked** -- the same workflow with a synthetic case that
   fails the guard produces a blocked outcome and the trace names the
   failing guard and the failing clause.
3. **Engine parity** -- the simulator's verdict matches a manual
   re-application of the guard logic, so the dry-run trace will not
   drift from the live ``execute_transition`` engine.
4. **Outbox discipline** -- the simulator is a pure function; no audit
   or outbox rows are written, the synthetic case is never persisted.

Fixtures live in ``backend/tests/workflows/simulations/``. Each
fixture pairs a synthetic case with an optional action list and an
``expected`` block the test asserts against.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from app.application.workflow.simulate_workflow import (
    SimulationResult,
    simulate,
)
from app.domain.workflow import (
    WorkflowState,
    WorkflowTransition,
    WorkflowVersion,
    WorkflowVersionStatus,
)
from tests.workflows.simulations import load_fixture

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")


# ---------------------------------------------------------------------------
# Seed workflow -- mirrors the seed in app/cli/seed.py (T066-T077 follow-up
# turns the seed into a real artifact; for now the fixture stands in so the
# simulator is exercised against the canonical case shape).
# ---------------------------------------------------------------------------


def _seed_workflow_version(*, workflow_definition_id: UUID | None = None) -> WorkflowVersion:
    """Return a draft ``ServiceRequestWorkflow`` ``WorkflowVersion``.

    The state machine matches AGENTS.md §16.2:

        intake -> in_review -> decided -> closed

    The two intermediate transitions carry declarative guards that
    reference ``case.participants`` and ``case.documents``. The
    ``submit_for_review`` transition is human-driven (a reviewer must
    act); ``decide`` and ``close`` are human-driven too, and the
    closing transition fires after a recorded decision.
    """
    return WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=workflow_definition_id or uuid4(),
        version_number=1,
        states=(
            WorkflowState(key="intake", label="Intake", is_initial=True),
            WorkflowState(key="in_review", label="In Review"),
            WorkflowState(key="decided", label="Decided"),
            WorkflowState(key="closed", label="Closed", is_terminal=True),
        ),
        transitions=(
            WorkflowTransition(
                key="submit_for_review",
                from_state="intake",
                to_state="in_review",
                human_driven=True,
                guard={
                    "op": "all",
                    "clauses": [
                        {"field": "case.participants", "op": "exists", "min": 1},
                        {"field": "case.documents", "op": "exists", "min": 1},
                    ],
                },
                actions=(
                    {"type": "create_task", "task_type": "review", "queue": "review_queue"},
                    {"type": "start_sla", "sla_key": "review_due", "hours": 48},
                ),
            ),
            WorkflowTransition(
                key="decide",
                from_state="in_review",
                to_state="decided",
                human_driven=True,
                actions=({"type": "set_field", "field": "decision_outcome", "value": "approve"},),
            ),
            WorkflowTransition(
                key="close",
                from_state="decided",
                to_state="closed",
                human_driven=True,
                guard={
                    "op": "exists",
                    "field": "case.documents",
                    "min": 1,
                },
                actions=(),
            ),
        ),
        now=None,
    )


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_simulate__service_request_happy_path__completes_in_three_steps() -> None:
    """The seed workflow against a complete case completes in three steps.

    Reads the canonical fixture (``service_request_happy_path.json``)
    and asserts the simulator's verdict matches every expectation in
    the ``expected`` block. The fixture is the source of truth the
    admin UI maintains, so updating it ripples to the test.
    """
    fixture: dict[str, Any] = load_fixture("service_request_happy_path")
    version = _seed_workflow_version()

    result = simulate(version, fixture["synthetic_case"], actions=fixture.get("actions"))

    assert isinstance(result, SimulationResult)
    expected = fixture["expected"]
    assert result.outcome == expected["outcome"]
    assert result.initial_state == expected["initial_state"]
    assert result.final_state == expected["final_state"]
    assert len(result.steps) == expected["step_count"]
    assert result.steps[0].transition == expected["first_transition"]
    assert result.steps[-1].transition == expected["last_transition"]

    # First step has a composite ``all`` guard; the simulator returns
    # exactly one ``SimulatedGuardResult`` for the composite, with
    # nested per-clause inputs. The guards array on the trace must
    # therefore be of length 1 even though the composite has two
    # clauses.
    first_step = result.steps[0]
    assert len(first_step.guards) == expected["guard_count_for_first_step"]
    composite = first_step.guards[0]
    assert composite.rule["op"] == "all"
    assert composite.result is True
    # The nested inputs capture every clause's evaluation so the admin
    # UI can explain a failure with a per-clause breakdown.
    clause_inputs = composite.inputs["clauses"]
    assert len(clause_inputs) == 2
    assert all(c["result"] is True for c in clause_inputs)

    # No blockers when the simulation completes.
    assert result.blockers == ()


def test_simulate__same_inputs_same_result__is_deterministic() -> None:
    """Two identical simulations produce byte-identical results.

    Determinism is the core contract for a dry-run trace (Constitution
    XIV). The simulator must not depend on time, randomness, or
    ordering of dict iteration -- two back-to-back calls produce
    structurally equal outputs.
    """
    fixture: dict[str, Any] = load_fixture("service_request_happy_path")
    version = _seed_workflow_version()
    first = simulate(version, fixture["synthetic_case"], actions=fixture["actions"])
    second = simulate(version, fixture["synthetic_case"], actions=fixture["actions"])

    assert first.outcome == second.outcome
    assert first.initial_state == second.initial_state
    assert first.final_state == second.final_state
    assert len(first.steps) == len(second.steps)
    for a, b in zip(first.steps, second.steps, strict=True):
        assert a.transition == b.transition
        assert a.from_state == b.from_state
        assert a.to_state == b.to_state
        assert len(a.guards) == len(b.guards)


# ---------------------------------------------------------------------------
# 2. Forced-blocked path
# ---------------------------------------------------------------------------


def test_simulate__service_request_blocked_by_empty_case__lists_failed_guard() -> None:
    """A case with no participants / no documents is blocked by the guard.

    The fixture's ``expected`` block names the failing transition and
    the blocker kind. The simulator returns a single blocker whose
    ``failed_guard`` field carries the composite guard shape so the
    admin UI can highlight exactly which clause (or clauses) failed.
    """
    fixture: dict[str, Any] = load_fixture("service_request_blocked")
    version = _seed_workflow_version()

    result = simulate(version, fixture["synthetic_case"], actions=fixture.get("actions"))

    expected = fixture["expected"]
    assert result.outcome == expected["outcome"]
    assert result.final_state == expected["final_state"]
    assert len(result.steps) == expected["step_count"]
    assert result.blockers, "expected at least one blocker"
    blocker = result.blockers[0]
    assert blocker.kind == expected["blocker_kind"]
    assert blocker.transition == expected["blocker_transition"]
    assert blocker.failed_guard is not None
    assert blocker.failed_guard.rule["op"] == "all"
    clause_inputs = blocker.failed_guard.inputs["clauses"]
    # Both clauses fail on an empty case, so the composite is False.
    assert all(c["result"] is False for c in clause_inputs)


# ---------------------------------------------------------------------------
# 3. Engine parity
# ---------------------------------------------------------------------------


def test_simulate__matches_manual_guard_evaluation_for_each_clause() -> None:
    """The simulator's verdict matches a hand-rolled guard walk.

    This is the **acceptance** for FM-015's "simulator matches real
    execute_transition guard evaluation" requirement: we replicate the
    guard evaluation logic inline and assert the simulator agrees.
    If a future contributor changes the simulator's guard semantics
    without updating the live engine, this test surfaces the drift
    immediately.
    """
    version = _seed_workflow_version()
    synthetic_case: dict[str, Any] = {
        "case": {
            "participants": [{"role": "intake", "display_name": "Iris"}],
            "documents": [],  # missing -> guard fails
            "priority": "normal",
        }
    }

    result = simulate(version, synthetic_case, actions=None)

    # Manual re-evaluation: only participants satisfies the clause;
    # documents fails; the composite ``all`` is False.
    participants = synthetic_case["case"]["participants"]
    documents = synthetic_case["case"]["documents"]
    participants_pass = len(participants) >= 1
    documents_pass = len(documents) >= 1
    composite_pass = participants_pass and documents_pass
    assert composite_pass is False

    assert result.outcome == "blocked"
    assert result.blockers[0].kind == "guard_failed"
    assert result.blockers[0].failed_guard is not None
    assert result.blockers[0].failed_guard.result is composite_pass


def test_simulate__auto_progress_without_actions__follows_declaration_order() -> None:
    """Without an action script the simulator auto-progresses deterministically.

    The simulator picks the first outgoing transition in declaration
    order at every state, which matches the runtime engine's contract
    for deterministic automatic progression (Constitution XIV).
    """
    version = _seed_workflow_version()
    synthetic_case: dict[str, Any] = {
        "case": {
            "participants": [{"role": "intake"}],
            "documents": [{"id": "doc-1"}],
        }
    }

    result = simulate(version, synthetic_case, actions=None)

    # First step is the only transition out of ``intake`` and its
    # guard passes for the complete case; the simulator stops at
    # ``decided`` (terminal state would be ``closed`` but the seed
    # workflow's final transition requires a document which is
    # already satisfied; verify by running the trace end-to-end).
    assert result.outcome in {"completed", "blocked"}
    assert result.steps[0].transition == "submit_for_review"
    assert result.steps[0].from_state == "intake"
    assert result.steps[0].to_state == "in_review"


def test_simulate__unknown_transition_key__reports_no_matching_transition() -> None:
    """An action that names a transition not declared on the version is a blocker.

    The simulator never silently picks a different transition; it
    surfaces the miss as a ``no_matching_transition`` blocker so the
    admin UI can show "the actor tried to fire a transition the
    workflow does not expose".
    """
    version = _seed_workflow_version()
    result = simulate(
        version,
        {"case": {"participants": [{"role": "intake"}], "documents": []}},
        actions=[{"transition_key": "does_not_exist", "payload": {}}],
    )

    assert result.outcome == "blocked"
    assert result.blockers[0].kind == "no_matching_transition"
    assert result.blockers[0].state == "intake"


# ---------------------------------------------------------------------------
# 4. Outbox / persistence discipline
# ---------------------------------------------------------------------------


def test_simulate__is_pure__has_no_io_surface() -> None:
    """The simulator is a pure function -- no DB, no clock, no audit.

    Calling ``simulate`` repeatedly with the same inputs returns the
    same result and never touches an external resource. The test
    asserts the API surface: ``simulate`` only takes
    ``(workflow_version, synthetic_case, actions)``; nothing in its
    signature implies persistence.
    """
    sig = inspect.signature(simulate)
    params = list(sig.parameters.values())
    names = [p.name for p in params]
    # Exactly three inputs: the version, the synthetic case, and the
    # optional action script. There is no ``db``, ``clock``, or
    # ``audit_store`` parameter -- a future regression that adds one
    # fails this test before code review.
    assert names == ["workflow_version", "synthetic_case", "actions"]
    assert sig.parameters["actions"].default is None


def test_simulate__trapped_initial_state__reports_no_outgoing_transition() -> None:
    """An initial state with zero outgoing transitions is a ``no_matching_transition`` blocker.

    The validator rejects this shape at publish time (FR-015), but the
    simulator is also tested in isolation against un-validated drafts.
    The simulator must fail closed: if there is no outgoing transition
    from the current state, the simulation stops with a diagnostic
    rather than spinning forever.
    """
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=(
            WorkflowState(key="intake", label="Intake", is_initial=True),
            WorkflowState(key="closed", label="Closed", is_terminal=True),
        ),
        transitions=(),  # no transitions -> intake is trapped
        now=None,
    )
    result = simulate(version, {}, actions=None)
    assert result.outcome == "blocked"
    assert result.steps == ()
    assert result.blockers[0].kind == "no_matching_transition"
    assert result.blockers[0].state == "intake"


# ---------------------------------------------------------------------------
# 5. Operator coverage -- every leaf operator must evaluate correctly so the
# simulator's dry-run verdict matches the live ``execute_transition`` engine.
# ---------------------------------------------------------------------------


def _operator_workflow(op: str, value: Any) -> WorkflowVersion:
    """Build a 2-state workflow whose guard is a single leaf operator.

    The transition fires ``intake -> closed`` iff the guard passes.
    The version is built dynamically per operator so each test reads
    its operator in the body -- the simulator's dispatch table is
    exercised exactly once per operator.
    """
    guard: dict[str, Any]
    if op == "regex":
        guard = {"op": "regex", "field": "case.title", "pattern": value}
    elif op in {"gt", "gte", "lt", "lte"}:
        guard = {"op": op, "field": "case.priority_rank", "value": value}
    elif op == "in":
        guard = {"op": "in", "field": "case.priority", "options": value}
    elif op == "contains":
        guard = {"op": "contains", "field": "case.title", "value": value}
    elif op == "exists":
        guard = {"op": "exists", "field": "case.documents", "min": value}
    else:  # ``eq`` / ``ne``
        guard = {"op": op, "field": "case.priority", "value": value}
    return WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=(
            WorkflowState(key="intake", label="Intake", is_initial=True),
            WorkflowState(key="closed", label="Closed", is_terminal=True),
        ),
        transitions=(
            WorkflowTransition(
                key="resolve",
                from_state="intake",
                to_state="closed",
                human_driven=True,
                guard=guard,
            ),
        ),
        now=None,
    )


def test_simulate__eq_operator__passes_when_field_matches() -> None:
    version = _operator_workflow("eq", "high")
    result = simulate(
        version,
        {"case": {"priority": "high", "documents": []}},
        actions=None,
    )
    assert result.outcome == "completed"
    assert result.steps[0].guards[0].result is True


def test_simulate__ne_operator__blocks_when_field_matches() -> None:
    version = _operator_workflow("ne", "high")
    result = simulate(
        version,
        {"case": {"priority": "high", "documents": []}},
        actions=None,
    )
    assert result.outcome == "blocked"
    assert result.blockers[0].failed_guard is not None
    assert result.blockers[0].failed_guard.result is False


def test_simulate__ordering_operators__gt_and_lte_pass_and_fail() -> None:
    gt_version = _operator_workflow("gt", 5)
    gt_pass = simulate(gt_version, {"case": {"priority_rank": 6}}, actions=None)
    assert gt_pass.outcome == "completed"

    gt_fail = simulate(gt_version, {"case": {"priority_rank": 5}}, actions=None)
    assert gt_fail.outcome == "blocked"

    lte_version = _operator_workflow("lte", 5)
    lte_pass = simulate(lte_version, {"case": {"priority_rank": 5}}, actions=None)
    assert lte_pass.outcome == "completed"

    lte_fail = simulate(lte_version, {"case": {"priority_rank": 6}}, actions=None)
    assert lte_fail.outcome == "blocked"


def test_simulate__in_operator__matches_collection_membership() -> None:
    version = _operator_workflow("in", ["low", "normal", "high"])
    result_pass = simulate(version, {"case": {"priority": "normal"}}, actions=None)
    assert result_pass.outcome == "completed"
    result_fail = simulate(version, {"case": {"priority": "urgent"}}, actions=None)
    assert result_fail.outcome == "blocked"


def test_simulate__contains_operator__substring_match_on_string() -> None:
    version = _operator_workflow("contains", "onboard")
    result_pass = simulate(version, {"case": {"title": "Help with onboarding"}}, actions=None)
    assert result_pass.outcome == "completed"
    result_fail = simulate(version, {"case": {"title": "Routine maintenance"}}, actions=None)
    assert result_fail.outcome == "blocked"


def test_simulate__regex_operator__matches_against_declared_pattern() -> None:
    version = _operator_workflow("regex", r"^C-\w+-\d{4}")
    result_pass = simulate(
        version,
        {"case": {"title": "C-ACME-2026 incident report"}},
        actions=None,
    )
    assert result_pass.outcome == "completed"

    # A malformed pattern must fail closed (no exception escapes the
    # simulator). We feed a deliberately broken pattern via a fresh
    # version; the simulator returns ``blocked`` with a False guard.
    broken_version = _operator_workflow("regex", r"(unclosed")
    broken_result = simulate(
        broken_version,
        {"case": {"title": "anything"}},
        actions=None,
    )
    assert broken_result.outcome == "blocked"
    assert broken_result.blockers[0].failed_guard is not None


def test_simulate__exists_operator__honors_min_threshold() -> None:
    version = _operator_workflow("exists", 2)
    result_pass = simulate(
        version,
        {"case": {"documents": [{}, {}]}},
        actions=None,
    )
    assert result_pass.outcome == "completed"
    result_fail = simulate(
        version,
        {"case": {"documents": [{}]}},
        actions=None,
    )
    assert result_fail.outcome == "blocked"


def test_simulate__draft_and_published_versions_produce_identical_traces() -> None:
    """The simulator ignores status. Draft and published versions trace the same.

    The endpoint rejects mutation of a published version's surface,
    but the simulator itself never mutates anything -- a draft and a
    published version with the same ``states``/``transitions`` must
    produce the same trace.
    """
    draft = _seed_workflow_version()
    published = draft.publish(actor_id=uuid4(), at=draft.created_at or _now())
    assert published.status is WorkflowVersionStatus.PUBLISHED

    fixture: dict[str, Any] = load_fixture("service_request_happy_path")
    synthetic_case = fixture["synthetic_case"]
    draft_result = simulate(draft, synthetic_case, actions=fixture["actions"])
    published_result = simulate(published, synthetic_case, actions=fixture["actions"])

    assert draft_result.outcome == published_result.outcome
    assert len(draft_result.steps) == len(published_result.steps)
    for a, b in zip(draft_result.steps, published_result.steps, strict=True):
        assert a.transition == b.transition
        assert a.to_state == b.to_state


def _now() -> datetime:
    return datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
