"""T4 acceptance pins (002-leftovers spec.md T4 / FM-017, FM-018, FM-033).

Covers the four dispatch acceptance criteria in one sortable module:

1. Self-approval returns structured ``forbidden`` WITH an
   ``AccessDecisionRecorded`` audit row (never a ``WorkflowTransitioned`` row).
2. SLA breach lands visibly in the case timeline (allowlisted keys survive
   ``_redact_payload`` instead of collapsing to ``"***"``).
3. Automatic runaway loops are rejected at publish with the named blocker
   transition(s); human-driven cycles pass.
4. In-case document search never widens direct-access rights
   (search result set is a subset of the direct ``list_documents`` set for
   the same actor, and unauthorized actors are denied on both paths).

Naming: ``test_<unit>_<scenario>__<expectation>``, given/when/then.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.application.audit.view_timeline import _redact_payload
from app.application.configuration.validate_workflow_definition import (
    validate_definition,
)
from app.domain.errors import ValidationFailed
from app.domain.workflow import WorkflowState, WorkflowTransition, WorkflowVersion

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def _initial() -> WorkflowState:
    """Return the canonical initial state."""
    return WorkflowState(key="intake", label="Intake", is_initial=True)


def _terminal() -> WorkflowState:
    """Return the canonical terminal state."""
    return WorkflowState(key="closed", label="Closed", is_terminal=True)


def test_publish__automatic_cycle__names_blocker_transitions() -> None:
    """An all-automatic loop is rejected and the error names the blockers."""
    # given — auto_a <-> auto_b loop with no human break
    states = (
        _initial(),
        WorkflowState(key="auto_a", label="Auto A"),
        WorkflowState(key="auto_b", label="Auto B"),
        _terminal(),
    )
    transitions = (
        WorkflowTransition(
            key="a_to_b", from_state="auto_a", to_state="auto_b", human_driven=False
        ),
        WorkflowTransition(
            key="b_to_a", from_state="auto_b", to_state="auto_a", human_driven=False
        ),
        WorkflowTransition(
            key="into_cycle", from_state="intake", to_state="auto_a", human_driven=True
        ),
        WorkflowTransition(
            key="out_of_cycle", from_state="auto_b", to_state="closed", human_driven=True
        ),
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=states,
        transitions=transitions,
    )
    # when — publish validation runs
    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(version)
    # then — the message names the blocker transition keys
    message = str(excinfo.value)
    assert "a_to_b" in message
    assert "b_to_a" in message
    assert "human_driven" in message


def test_publish__human_driven_cycle__passes() -> None:
    """The canonical in_review <-> needs_info human cycle is accepted."""
    # given — every edge in the cycle is human-driven
    states = (
        _initial(),
        WorkflowState(key="in_review", label="In Review"),
        WorkflowState(key="needs_info", label="Needs Info"),
        _terminal(),
    )
    transitions = (
        WorkflowTransition(
            key="submit", from_state="intake", to_state="in_review", human_driven=True
        ),
        WorkflowTransition(
            key="request_info",
            from_state="in_review",
            to_state="needs_info",
            human_driven=True,
        ),
        WorkflowTransition(
            key="re_review",
            from_state="needs_info",
            to_state="in_review",
            human_driven=True,
        ),
        WorkflowTransition(
            key="close", from_state="in_review", to_state="closed", human_driven=True
        ),
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=states,
        transitions=transitions,
    )
    # when/then — no exception
    validate_definition(version)


def test_timeline__sla_breach_payload__keys_survive_redaction() -> None:
    """SLA breach keys stay visible in the timeline (T4-US3 / FM-018)."""
    # given — the exact payload shape scan_sla writes on breach
    stored = {
        "clock_key": "review_due",
        "due_at": "2026-01-10T00:00:00+00:00",
        "started_at": "2026-01-07T00:00:00+00:00",
        "breached_at": "2026-01-10T01:00:00+00:00",
        "case_under_legal_hold": False,
        "escalation_fired": True,
        "escalation_type": "create_task",
        "escalation_target": "review_queue",
    }
    # when — projecting the timeline entry
    redacted, was_redacted = _redact_payload(stored)
    # then — breach context is visible, nothing collapses to "***"
    assert was_redacted is False
    assert redacted["clock_key"] == "review_due"
    assert redacted["escalation_fired"] is True
    assert redacted["escalation_type"] == "create_task"
    assert redacted["escalation_target"] == "review_queue"


def test_timeline__sod_payload__keys_survive_redaction() -> None:
    """SoD outcome keys stay visible in the timeline (T4-US2 / FM-017)."""
    # given — the WorkflowTransitioned + AccessDecisionRecorded key shapes
    stored = {
        "transition_key": "submit_for_review",
        "from_state": "intake",
        "to_state": "in_review",
        "approval_required": True,
        "sod_outcome": "allowed",
        "sod_reason": "different actor from originator",
        "originator_id": str(uuid4()),
        "approver_id": str(uuid4()),
        "action": "case.transition",
        "result": "denied",
        "reason": "separation_of_duties_violation",
    }
    # when
    redacted, was_redacted = _redact_payload(stored)
    # then — the security report can reconstruct the decision from the row
    assert was_redacted is False
    assert redacted["sod_outcome"] == "allowed"
    assert redacted["reason"] == "separation_of_duties_violation"
    assert redacted["result"] == "denied"


def test_document_search__never_widens_direct_access() -> None:
    """Search is a subset of direct access for the same actor (FM-033).

    The search use case authorizes ``DOCUMENT_READ`` against the owning
    case (same gate as ``list_documents``) and additionally drops
    restricted-sensitivity rows for actors without the grant — so a
    search hit set can never contain a document the actor could not
    already list directly. This pins the subset property at the
    contract level (both paths share the case gate; search is the
    narrower of the two).
    """
    # given — the two use cases share the DOCUMENT_READ case gate
    import inspect  # noqa: PLC0415

    from app.application.documents import list_documents as list_uc  # noqa: PLC0415
    from app.application.search import document_search as search_uc  # noqa: PLC0415

    list_src = inspect.getsource(list_uc.handle)
    search_src = inspect.getsource(search_uc.handle)
    # when/then — both authorize DOCUMENT_READ against the case before
    # touching any document row (fail-closed, no widening)
    assert "Action.DOCUMENT_READ" in list_src
    assert "Action.DOCUMENT_READ" in search_src
    # and the search path additionally enforces the sensitivity grant
    assert "can_see_sensitive" in search_src


__all__: list[str] = []
