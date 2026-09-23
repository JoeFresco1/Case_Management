"""Domain tests for ``WorkflowDefinition`` and ``WorkflowVersion`` (P02-US2 / FM-013).

Naming follows AGENTS.md §4.R5 (``test_<unit>_<scenario>__<expectation>``).
The acceptance scenarios for this slice are:

1. **Construction invariants**: bad keys/labels/states raise
   ``InvariantViolation`` at ``__post_init__`` time.
2. **Draft factory**: ``WorkflowVersion.draft`` produces a version with
   ``status=DRAFT``, no published stamps, and the supplied
   ``states``/``transitions``.
3. **Publish lifecycle**: ``WorkflowVersion.publish`` flips
   ``draft → published``, stamps ``published_at`` / ``published_by``,
   and is **idempotent across re-invocation** (a second publish on the
   resulting version raises ``InvariantViolation`` because its status
   is no longer ``draft``).
4. **Immutability guard**: ``WorkflowVersion.assert_mutable`` raises on
   non-draft versions. This is the primary guard used by the SQL
   adapter to refuse UPDATE attempts on published rows (FR-015).
5. **Retire lifecycle**: ``WorkflowVersion.retire`` flips
   ``published → retired`` and refuses non-published inputs.
6. **with_published_version**: ``WorkflowDefinition.with_published_version``
   promotes a published version to the header's ``current_version_id``
   and rejects unpublished / cross-aggregate inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.domain.errors import InvariantViolation
from app.domain.workflow import (
    WorkflowDefinition,
    WorkflowState,
    WorkflowTransition,
    WorkflowVersion,
    WorkflowVersionStatus,
)

# ---------------------------------------------------------------------------
# Two distinct tenants for tenant-mismatch assertions
# ---------------------------------------------------------------------------

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
BETA_TENANT = UUID("22222222-2222-2222-2222-222222222222")

ACTOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers — small, healthy building blocks for the assertion scenarios
# ---------------------------------------------------------------------------


def _initial_state() -> WorkflowState:
    return WorkflowState(key="intake", label="Intake", is_initial=True)


def _terminal_state() -> WorkflowState:
    return WorkflowState(key="closed", label="Closed", is_terminal=True)


def _healthy_states() -> tuple[WorkflowState, ...]:
    return (
        _initial_state(),
        WorkflowState(key="in_review", label="In Review"),
        _terminal_state(),
    )


def _healthy_transitions() -> tuple[WorkflowTransition, ...]:
    return (
        WorkflowTransition(
            key="submit_for_review",
            from_state="intake",
            to_state="in_review",
            human_driven=True,
        ),
        WorkflowTransition(
            key="close",
            from_state="in_review",
            to_state="closed",
            human_driven=True,
        ),
    )


def _healthy_workflow_definition_id() -> UUID:
    return uuid4()


# ---------------------------------------------------------------------------
# 1. Construction invariants
# ---------------------------------------------------------------------------


def test_workflow_state__empty_key__raises_invariant() -> None:
    """An empty state key raises ``InvariantViolation``."""
    with pytest.raises(InvariantViolation):
        WorkflowState(key="", label="Intake", is_initial=True)


def test_workflow_state__non_alphanumeric_key__raises_invariant() -> None:
    """A state key with whitespace or punctuation is rejected."""
    with pytest.raises(InvariantViolation):
        WorkflowState(key="intake state", label="Intake", is_initial=True)


def test_workflow_state__empty_label__raises_invariant() -> None:
    """An empty or whitespace label is rejected."""
    with pytest.raises(InvariantViolation):
        WorkflowState(key="intake", label="   ", is_initial=True)


def test_workflow_state__initial_and_terminal__raises_invariant() -> None:
    """A state cannot be both initial and terminal (it would deadlock)."""
    with pytest.raises(InvariantViolation):
        WorkflowState(
            key="start",
            label="Start",
            is_initial=True,
            is_terminal=True,
        )


def test_workflow_transition__self_loop__raises_invariant() -> None:
    """A transition whose ``from_state`` equals ``to_state`` is rejected."""
    with pytest.raises(InvariantViolation):
        WorkflowTransition(
            key="loop",
            from_state="intake",
            to_state="intake",
        )


def test_workflow_definition__empty_key__raises_invariant() -> None:
    """An empty workflow key raises ``InvariantViolation``."""
    with pytest.raises(InvariantViolation):
        WorkflowDefinition.create(
            tenant_id=ACME_TENANT,
            key="",
            name="Workflow",
        )


def test_workflow_definition__empty_name__raises_invariant() -> None:
    """An empty workflow name raises ``InvariantViolation``."""
    with pytest.raises(InvariantViolation):
        WorkflowDefinition.create(
            tenant_id=ACME_TENANT,
            key="ServiceRequestWorkflow",
            name="   ",
        )


# ---------------------------------------------------------------------------
# 2. Draft factory
# ---------------------------------------------------------------------------


def test_workflow_version_draft__factory__returns_draft_status_with_no_publish_stamps() -> None:
    """``WorkflowVersion.draft`` produces a draft version whose
    ``published_at`` and ``published_by`` are both ``None``."""
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=_healthy_workflow_definition_id(),
        version_number=1,
        states=_healthy_states(),
        transitions=_healthy_transitions(),
        now=FIXED_NOW,
    )

    assert version.status is WorkflowVersionStatus.DRAFT
    assert version.published_at is None
    assert version.published_by is None
    assert version.created_at == FIXED_NOW
    assert version.version_number == 1
    assert len(version.state_keys()) == 3
    assert len(version.transition_keys()) == 2


def test_workflow_version_draft__empty_states__raises_invariant() -> None:
    """A workflow with no states is rejected at construction."""
    with pytest.raises(InvariantViolation):
        WorkflowVersion.draft(
            tenant_id=ACME_TENANT,
            workflow_definition_id=_healthy_workflow_definition_id(),
            version_number=1,
            states=(),
            transitions=(),
            now=FIXED_NOW,
        )


def test_workflow_version_draft__invalid_version_number__raises_invariant() -> None:
    """``version_number`` must be ``>= 1`` (the draft factory is the
    monotonic-version anchor)."""
    with pytest.raises(InvariantViolation):
        WorkflowVersion.draft(
            tenant_id=ACME_TENANT,
            workflow_definition_id=_healthy_workflow_definition_id(),
            version_number=0,
            states=_healthy_states(),
            transitions=_healthy_transitions(),
            now=FIXED_NOW,
        )


# ---------------------------------------------------------------------------
# 3. Publish lifecycle + immutability guard
# ---------------------------------------------------------------------------


def test_workflow_version_publish__flips_status_and_stamps_publish_metadata() -> None:
    """``publish`` flips ``draft → published`` and stamps
    ``published_at`` / ``published_by`` from the arguments."""
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=_healthy_workflow_definition_id(),
        version_number=1,
        states=_healthy_states(),
        transitions=_healthy_transitions(),
        now=FIXED_NOW,
    )

    published = version.publish(actor_id=ACTOR, at=FIXED_NOW)

    assert published.status is WorkflowVersionStatus.PUBLISHED
    assert published.published_at == FIXED_NOW
    assert published.published_by == ACTOR
    # The original is unchanged (frozen dataclass).
    assert version.status is WorkflowVersionStatus.DRAFT
    assert version.published_at is None


def test_workflow_version_publish__re_publish_published__raises_invariant() -> None:
    """A second publish call on a published version raises
    ``InvariantViolation`` — the persistence-layer primary guard for
    FR-015."""
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=_healthy_workflow_definition_id(),
        version_number=1,
        states=_healthy_states(),
        transitions=_healthy_transitions(),
        now=FIXED_NOW,
    )
    published = version.publish(actor_id=ACTOR, at=FIXED_NOW)

    with pytest.raises(InvariantViolation):
        published.publish(actor_id=ACTOR, at=FIXED_NOW)


def test_workflow_version_publish__published_version_is_immutable__assert_mutable_raises() -> None:
    """``assert_mutable`` raises on a non-draft version — this is the
    SQL adapter's defense-in-depth check that refuses UPDATE on a
    published row."""
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=_healthy_workflow_definition_id(),
        version_number=1,
        states=_healthy_states(),
        transitions=_healthy_transitions(),
        now=FIXED_NOW,
    )
    published = version.publish(actor_id=ACTOR, at=FIXED_NOW)

    with pytest.raises(InvariantViolation):
        published.assert_mutable()


def test_workflow_version_assert_mutable__draft_version__does_not_raise() -> None:
    """A draft version's ``assert_mutable`` is a no-op (the SQL
    adapter's UPDATE path is allowed on drafts only)."""
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=_healthy_workflow_definition_id(),
        version_number=1,
        states=_healthy_states(),
        transitions=_healthy_transitions(),
        now=FIXED_NOW,
    )
    version.assert_mutable()  # should not raise


# ---------------------------------------------------------------------------
# 4. Retire lifecycle
# ---------------------------------------------------------------------------


def test_workflow_version_retire__flips_published_to_retired() -> None:
    """``retire`` flips ``published → retired`` and preserves
    ``published_at`` / ``published_by`` (the historical record)."""
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=_healthy_workflow_definition_id(),
        version_number=1,
        states=_healthy_states(),
        transitions=_healthy_transitions(),
        now=FIXED_NOW,
    )
    published = version.publish(actor_id=ACTOR, at=FIXED_NOW)
    retired = published.retire()

    assert retired.status is WorkflowVersionStatus.RETIRED
    assert retired.published_at == FIXED_NOW
    assert retired.published_by == ACTOR


def test_workflow_version_retire__on_draft__raises_invariant() -> None:
    """A draft version cannot be retired — only published versions
    transition to retired."""
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=_healthy_workflow_definition_id(),
        version_number=1,
        states=_healthy_states(),
        transitions=_healthy_transitions(),
        now=FIXED_NOW,
    )

    with pytest.raises(InvariantViolation):
        version.retire()


def test_workflow_version_retire__on_already_retired__raises_invariant() -> None:
    """A retired version cannot be retired again — the lifecycle is
    one-way after publish."""
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=_healthy_workflow_definition_id(),
        version_number=1,
        states=_healthy_states(),
        transitions=_healthy_transitions(),
        now=FIXED_NOW,
    )
    published = version.publish(actor_id=ACTOR, at=FIXED_NOW)
    retired = published.retire()

    with pytest.raises(InvariantViolation):
        retired.retire()


# ---------------------------------------------------------------------------
# 5. WorkflowDefinition.with_published_version
# ---------------------------------------------------------------------------


def test_workflow_definition_with_published_version__promotes_to_current() -> None:
    """Promoting a published version sets the header's
    ``current_version_id`` and stamps ``updated_at``."""
    workflow = WorkflowDefinition.create(
        tenant_id=ACME_TENANT,
        key="ServiceRequestWorkflow",
        name="Service Request Workflow",
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=workflow.id,
        version_number=1,
        states=_healthy_states(),
        transitions=_healthy_transitions(),
        now=FIXED_NOW,
    )
    published = version.publish(actor_id=ACTOR, at=FIXED_NOW)

    promoted = workflow.with_published_version(published)

    assert promoted.current_version_id == published.id
    assert promoted.updated_at == FIXED_NOW
    # The original is unchanged (frozen dataclass).
    assert workflow.current_version_id is None


def test_workflow_definition_with_published_version__draft_version__raises_invariant() -> None:
    """Promoting a draft version is rejected — only published versions
    may become current."""
    workflow = WorkflowDefinition.create(
        tenant_id=ACME_TENANT,
        key="ServiceRequestWorkflow",
        name="Service Request Workflow",
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=workflow.id,
        version_number=1,
        states=_healthy_states(),
        transitions=_healthy_transitions(),
        now=FIXED_NOW,
    )

    with pytest.raises(InvariantViolation):
        workflow.with_published_version(version)


def test_workflow_definition_with_published_version__cross_tenant__raises_invariant() -> None:
    """Promoting a version that belongs to a different tenant is
    rejected — cross-tenant aggregates are never observable."""
    workflow = WorkflowDefinition.create(
        tenant_id=ACME_TENANT,
        key="ServiceRequestWorkflow",
        name="Service Request Workflow",
    )
    version = WorkflowVersion.draft(
        tenant_id=BETA_TENANT,
        workflow_definition_id=workflow.id,
        version_number=1,
        states=_healthy_states(),
        transitions=_healthy_transitions(),
        now=FIXED_NOW,
    )
    published = version.publish(actor_id=ACTOR, at=FIXED_NOW)

    with pytest.raises(InvariantViolation):
        workflow.with_published_version(published)


def test_workflow_definition_with_published_version__cross_aggregate__raises_invariant() -> None:
    """Promoting a version that belongs to a different
    WorkflowDefinition is rejected — ``current_version_id`` must point
    at a row that owns its aggregate root."""
    workflow = WorkflowDefinition.create(
        tenant_id=ACME_TENANT,
        key="ServiceRequestWorkflow",
        name="Service Request Workflow",
    )
    other_workflow = WorkflowDefinition.create(
        tenant_id=ACME_TENANT,
        key="OtherWorkflow",
        name="Other Workflow",
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=other_workflow.id,
        version_number=1,
        states=_healthy_states(),
        transitions=_healthy_transitions(),
        now=FIXED_NOW,
    )
    published = version.publish(actor_id=ACTOR, at=FIXED_NOW)

    with pytest.raises(InvariantViolation):
        workflow.with_published_version(published)


# ---------------------------------------------------------------------------
# 6. Accessors
# ---------------------------------------------------------------------------


def test_workflow_version_state_keys__returns_keys_in_declaration_order() -> None:
    """``state_keys`` returns the keys in the order they were
    declared (tests rely on this for the validate-only endpoint
    response)."""
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=_healthy_workflow_definition_id(),
        version_number=1,
        states=_healthy_states(),
        transitions=(),
        now=FIXED_NOW,
    )
    assert version.state_keys() == ("intake", "in_review", "closed")


def test_workflow_version_outgoing__returns_transitions_from_a_state() -> None:
    """``outgoing(state_key)`` returns the transitions whose
    ``from_state`` matches."""
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=_healthy_workflow_definition_id(),
        version_number=1,
        states=_healthy_states(),
        transitions=_healthy_transitions(),
        now=FIXED_NOW,
    )

    intake_out = version.outgoing("intake")
    assert len(intake_out) == 1
    assert intake_out[0].key == "submit_for_review"

    closed_out = version.outgoing("closed")
    assert closed_out == ()
