"""Tests for advanced form execution (FM-038 / P07-US6, P07-US7).

Covers:
- Multi-step session: worker can resume after partial save (P07-US6).
- State-specific form validation: a transition guard rejects when a
  state-bound required form has not been submitted (P07-US7).
- Hidden field preservation across steps (P07-US3 + multi-step seam).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.forms import advanced_execution as adv
from app.domain.errors import Forbidden, NotFound, ValidationFailed
from app.domain.form import (
    FormBinding,
    FormDefinition,
    FormFieldSpec,
    FormSectionSpec,
    FormVersion,
)
from app.domain.workflow import (
    WorkflowState,
    WorkflowTransition,
    WorkflowVersion,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _Clock:
    now_value: datetime

    def now(self) -> datetime:
        return self.now_value


class _FakeCaseRepo:
    def __init__(self) -> None:
        self._cases: dict[UUID, Any] = {}

    async def get(self, tenant_id: UUID, case_id: UUID) -> Any | None:
        case = self._cases.get(case_id)
        if case and case.tenant_id == tenant_id:
            return case
        return None

    def add(self, case: Any) -> None:
        self._cases[case.id] = case


class _FakeFormBindingRepo:
    def __init__(self) -> None:
        self._bindings: dict[UUID, FormBinding] = {}
        self._form_key_by_id: dict[UUID, str] = {}

    async def get_published_for_key(self, tenant_id: UUID, form_key: str) -> FormBinding | None:
        for bid, b in self._bindings.items():
            if (
                b.tenant_id == tenant_id
                and b.published_at is not None
                and self._form_key_by_id.get(bid) == form_key
            ):
                return b
        return None

    def add(self, binding: FormBinding, form_key: str) -> None:
        self._bindings[binding.id] = binding
        self._form_key_by_id[binding.id] = form_key


class _FakeFormVersionLoader:
    def __init__(self) -> None:
        self._versions: dict[UUID, FormVersion] = {}

    async def get_by_id(self, tenant_id: UUID, form_version_id: UUID) -> FormVersion | None:
        v = self._versions.get(form_version_id)
        if v and v.tenant_id == tenant_id:
            return v
        return None

    def add(self, version: FormVersion) -> None:
        self._versions[version.id] = version


class _FakeFormSessionRepo:
    def __init__(self) -> None:
        self._sessions: dict[UUID, adv.FormSession] = {}

    async def get(self, tenant_id: UUID, session_id: UUID) -> adv.FormSession | None:
        s = self._sessions.get(session_id)
        if s and s.tenant_id == tenant_id:
            return s
        return None

    async def get_open_for_case_and_form(
        self,
        tenant_id: UUID,
        case_id: UUID,
        form_version_id: UUID,
        actor_id: UUID,
    ) -> adv.FormSession | None:
        for s in self._sessions.values():
            if (
                s.tenant_id == tenant_id
                and s.case_id == case_id
                and s.form_version_id == form_version_id
                and s.actor_id == actor_id
                and s.status == "in_progress"
            ):
                return s
        return None

    async def add(self, session: adv.FormSession) -> None:
        self._sessions[session.id] = session

    async def update(self, session: adv.FormSession) -> None:
        self._sessions[session.id] = session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant() -> UUID:
    return uuid4()


def _actor() -> UUID:
    return uuid4()


def _case_id() -> UUID:
    return uuid4()


def _make_case(tenant_id: UUID, case_id: UUID) -> Any:
    """Build a tiny case-like object sufficient for the use case.

    We use a stand-in class so this test does not pull in the real
    :class:`app.domain.case.Case` builder. The use case reads
    ``tenant_id``, ``id``, and ``case_type_version_id`` only.
    """

    class _Stub:
        pass

    stub = _Stub()
    stub.id = case_id
    stub.tenant_id = tenant_id
    stub.case_type_version_id = uuid4()
    stub.data: dict[str, Any] = {"existing": "value"}
    return stub


def _publish_two_step_form(
    tenant_id: UUID,
    actor_id: UUID,
) -> FormVersion:
    """Build a published two-step form: 'basics' then 'review'.

    Step 1 has one visible required field, step 2 has a different
    required field. Step 2's section is conditionally hidden unless
    ``needs_review`` is truthy — exercising the hidden-field
    preservation seam across the multi-step path.
    """
    sections = (
        FormSectionSpec(
            key="basics",
            label="Basics",
            fields=(
                FormFieldSpec(key="title", label="Title", required=True),
                FormFieldSpec(key="needs_review", label="Needs review"),
            ),
        ),
        FormSectionSpec(
            key="detail",
            label="Detail",
            fields=(FormFieldSpec(key="notes", label="Notes", required=False),),
        ),
        FormSectionSpec(
            key="review",
            label="Review",
            fields=(FormFieldSpec(key="decision", label="Decision", required=True),),
            visible_when={"truthy": {"field": "needs_review"}},
        ),
    )
    steps = (
        {"step_key": "basics", "label": "Basics", "section_keys": ["basics"], "required": True},
        {"step_key": "detail", "label": "Detail", "section_keys": ["detail"], "required": False},
        {"step_key": "review", "label": "Review", "section_keys": ["review"], "required": True},
    )
    definition = FormDefinition(sections=sections, validations=(), steps=steps)
    fv = FormVersion.draft(
        tenant_id=tenant_id,
        key="MultiStep",
        version_number=1,
        definition=definition,
    )
    return fv.publish(actor_id=actor_id, at=datetime.now(tz=UTC))


# ---------------------------------------------------------------------------
# Acceptance: resume after partial save (P07-US6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_session_creates_first_step() -> None:
    tenant = _tenant()
    actor = _actor()
    case = _make_case(tenant, _case_id())
    fv = _publish_two_step_form(tenant, actor)
    binding = FormBinding.draft(
        tenant_id=tenant,
        case_type_version_id=case.case_type_version_id,
        form_version_id=fv.id,
    ).publish(actor_id=actor, at=datetime.now(tz=UTC))

    case_repo = _FakeCaseRepo()
    case_repo.add(case)
    binding_repo = _FakeFormBindingRepo()
    binding_repo.add(binding, form_key="MultiStep")
    fv_loader = _FakeFormVersionLoader()
    fv_loader.add(fv)
    session_repo = _FakeFormSessionRepo()
    clock = _Clock(datetime.now(tz=UTC))

    view = await adv.start_session(
        adv.StartFormSessionCommand(
            tenant_id=tenant,
            actor_id=actor,
            actor_roles=("reviewer",),
            case_id=case.id,
            form_key="MultiStep",
        ),
        case_repo=case_repo,
        form_binding_repo=binding_repo,
        form_version_loader=fv_loader,
        session_repo=session_repo,
        clock=clock,
    )
    assert view.session.status == "in_progress"
    assert view.session.current_step_index == 0
    assert view.current_step is not None
    assert view.current_step.step_key == "basics"
    assert [status for _, status in view.progress] == ["current", "pending", "pending"]


@pytest.mark.asyncio
async def test_resume_after_partial_save_returns_same_session() -> None:
    """Acceptance: worker completes multi-step form resuming after save."""
    tenant = _tenant()
    actor = _actor()
    case = _make_case(tenant, _case_id())
    fv = _publish_two_step_form(tenant, actor)
    binding = FormBinding.draft(
        tenant_id=tenant,
        case_type_version_id=case.case_type_version_id,
        form_version_id=fv.id,
    ).publish(actor_id=actor, at=datetime.now(tz=UTC))

    case_repo = _FakeCaseRepo()
    case_repo.add(case)
    binding_repo = _FakeFormBindingRepo()
    binding_repo.add(binding, form_key="MultiStep")
    fv_loader = _FakeFormVersionLoader()
    fv_loader.add(fv)
    session_repo = _FakeFormSessionRepo()
    clock = _Clock(datetime.now(tz=UTC))

    started = await adv.start_session(
        adv.StartFormSessionCommand(
            tenant_id=tenant,
            actor_id=actor,
            actor_roles=("reviewer",),
            case_id=case.id,
            form_key="MultiStep",
        ),
        case_repo=case_repo,
        form_binding_repo=binding_repo,
        form_version_loader=fv_loader,
        session_repo=session_repo,
        clock=clock,
    )

    # Save step 1 — partial save.
    after_step_one = await adv.save_step(
        adv.SaveFormStepCommand(
            tenant_id=tenant,
            actor_id=actor,
            actor_roles=("reviewer",),
            case_id=case.id,
            session_id=started.session.id,
            step_key="basics",
            data={"title": "My title"},
        ),
        case_repo=case_repo,
        form_version_loader=fv_loader,
        session_repo=session_repo,
        clock=clock,
    )
    assert "basics" in after_step_one.session.completed_steps
    assert after_step_one.session.current_step_index == 1
    assert after_step_one.session.step_data["title"] == "My title"
    assert after_step_one.session.version_token == started.session.version_token + 1

    # Resume from a fresh query: the existing in-progress session
    # must come back so the SPA renders step 2.
    resumed = await adv.resume_session(
        adv.ResumeFormSessionQuery(
            tenant_id=tenant,
            actor_id=actor,
            actor_roles=("reviewer",),
            case_id=case.id,
            form_key="MultiStep",
        ),
        case_repo=case_repo,
        form_binding_repo=binding_repo,
        form_version_loader=fv_loader,
        session_repo=session_repo,
    )
    assert resumed is not None
    assert resumed.session.id == started.session.id
    assert resumed.current_step is not None
    assert resumed.current_step.step_key == "detail"
    assert "basics" in resumed.session.completed_steps

    # Save step 2 (optional).
    after_step_two = await adv.save_step(
        adv.SaveFormStepCommand(
            tenant_id=tenant,
            actor_id=actor,
            actor_roles=("reviewer",),
            case_id=case.id,
            session_id=resumed.session.id,
            step_key="detail",
            data={"notes": "Some notes"},
        ),
        case_repo=case_repo,
        form_version_loader=fv_loader,
        session_repo=session_repo,
        clock=clock,
    )
    assert "detail" in after_step_two.session.completed_steps
    assert after_step_two.session.current_step_index == 2

    # Save step 3 (required). Progress saves never flip to
    # ``submitted`` on their own — the validated commit lives in
    # ``submit_session`` so an unvalidated payload can never read as
    # done to the transition gate. The session stays ``in_progress``
    # with every step completed (SPA shows "ready to submit").
    after_step_three = await adv.save_step(
        adv.SaveFormStepCommand(
            tenant_id=tenant,
            actor_id=actor,
            actor_roles=("reviewer",),
            case_id=case.id,
            session_id=after_step_two.session.id,
            step_key="review",
            data={"needs_review": True, "decision": "approve"},
        ),
        case_repo=case_repo,
        form_version_loader=fv_loader,
        session_repo=session_repo,
        clock=clock,
    )
    assert after_step_three.session.status == "in_progress"
    assert after_step_three.session.completed_steps == frozenset({"basics", "detail", "review"})
    assert after_step_three.session.current_step_index == 3
    assert after_step_three.current_step is None
    assert [status for _, status in after_step_three.progress] == [
        "completed",
        "completed",
        "completed",
    ]


@pytest.mark.asyncio
async def test_start_session_is_idempotent() -> None:
    tenant = _tenant()
    actor = _actor()
    case = _make_case(tenant, _case_id())
    fv = _publish_two_step_form(tenant, actor)
    binding = FormBinding.draft(
        tenant_id=tenant,
        case_type_version_id=case.case_type_version_id,
        form_version_id=fv.id,
    ).publish(actor_id=actor, at=datetime.now(tz=UTC))
    case_repo = _FakeCaseRepo()
    case_repo.add(case)
    binding_repo = _FakeFormBindingRepo()
    binding_repo.add(binding, form_key="MultiStep")
    fv_loader = _FakeFormVersionLoader()
    fv_loader.add(fv)
    session_repo = _FakeFormSessionRepo()
    clock = _Clock(datetime.now(tz=UTC))

    first = await adv.start_session(
        adv.StartFormSessionCommand(
            tenant_id=tenant,
            actor_id=actor,
            actor_roles=("reviewer",),
            case_id=case.id,
            form_key="MultiStep",
        ),
        case_repo=case_repo,
        form_binding_repo=binding_repo,
        form_version_loader=fv_loader,
        session_repo=session_repo,
        clock=clock,
    )
    second = await adv.start_session(
        adv.StartFormSessionCommand(
            tenant_id=tenant,
            actor_id=actor,
            actor_roles=("reviewer",),
            case_id=case.id,
            form_key="MultiStep",
        ),
        case_repo=case_repo,
        form_binding_repo=binding_repo,
        form_version_loader=fv_loader,
        session_repo=session_repo,
        clock=clock,
    )
    assert first.session.id == second.session.id


@pytest.mark.asyncio
async def test_save_step_rejects_unknown_step() -> None:
    tenant = _tenant()
    actor = _actor()
    case = _make_case(tenant, _case_id())
    fv = _publish_two_step_form(tenant, actor)
    binding = FormBinding.draft(
        tenant_id=tenant,
        case_type_version_id=case.case_type_version_id,
        form_version_id=fv.id,
    ).publish(actor_id=actor, at=datetime.now(tz=UTC))
    case_repo = _FakeCaseRepo()
    case_repo.add(case)
    binding_repo = _FakeFormBindingRepo()
    binding_repo.add(binding, form_key="MultiStep")
    fv_loader = _FakeFormVersionLoader()
    fv_loader.add(fv)
    session_repo = _FakeFormSessionRepo()
    clock = _Clock(datetime.now(tz=UTC))
    started = await adv.start_session(
        adv.StartFormSessionCommand(
            tenant_id=tenant,
            actor_id=actor,
            actor_roles=("reviewer",),
            case_id=case.id,
            form_key="MultiStep",
        ),
        case_repo=case_repo,
        form_binding_repo=binding_repo,
        form_version_loader=fv_loader,
        session_repo=session_repo,
        clock=clock,
    )

    with pytest.raises(ValidationFailed):
        await adv.save_step(
            adv.SaveFormStepCommand(
                tenant_id=tenant,
                actor_id=actor,
                actor_roles=("reviewer",),
                case_id=case.id,
                session_id=started.session.id,
                step_key="nope",
                data={},
            ),
            case_repo=case_repo,
            form_version_loader=fv_loader,
            session_repo=session_repo,
            clock=clock,
        )


@pytest.mark.asyncio
async def test_save_step_rejects_when_ahead_of_current() -> None:
    tenant = _tenant()
    actor = _actor()
    case = _make_case(tenant, _case_id())
    fv = _publish_two_step_form(tenant, actor)
    binding = FormBinding.draft(
        tenant_id=tenant,
        case_type_version_id=case.case_type_version_id,
        form_version_id=fv.id,
    ).publish(actor_id=actor, at=datetime.now(tz=UTC))
    case_repo = _FakeCaseRepo()
    case_repo.add(case)
    binding_repo = _FakeFormBindingRepo()
    binding_repo.add(binding, form_key="MultiStep")
    fv_loader = _FakeFormVersionLoader()
    fv_loader.add(fv)
    session_repo = _FakeFormSessionRepo()
    clock = _Clock(datetime.now(tz=UTC))
    started = await adv.start_session(
        adv.StartFormSessionCommand(
            tenant_id=tenant,
            actor_id=actor,
            actor_roles=("reviewer",),
            case_id=case.id,
            form_key="MultiStep",
        ),
        case_repo=case_repo,
        form_binding_repo=binding_repo,
        form_version_loader=fv_loader,
        session_repo=session_repo,
        clock=clock,
    )
    with pytest.raises(ValidationFailed):
        await adv.save_step(
            adv.SaveFormStepCommand(
                tenant_id=tenant,
                actor_id=actor,
                actor_roles=("reviewer",),
                case_id=case.id,
                session_id=started.session.id,
                step_key="review",
                data={"decision": "ok"},
            ),
            case_repo=case_repo,
            form_version_loader=fv_loader,
            session_repo=session_repo,
            clock=clock,
        )


@pytest.mark.asyncio
async def test_save_step_rejects_wrong_actor() -> None:
    tenant = _tenant()
    actor = _actor()
    other_actor = _actor()
    case = _make_case(tenant, _case_id())
    fv = _publish_two_step_form(tenant, actor)
    binding = FormBinding.draft(
        tenant_id=tenant,
        case_type_version_id=case.case_type_version_id,
        form_version_id=fv.id,
    ).publish(actor_id=actor, at=datetime.now(tz=UTC))
    case_repo = _FakeCaseRepo()
    case_repo.add(case)
    binding_repo = _FakeFormBindingRepo()
    binding_repo.add(binding, form_key="MultiStep")
    fv_loader = _FakeFormVersionLoader()
    fv_loader.add(fv)
    session_repo = _FakeFormSessionRepo()
    clock = _Clock(datetime.now(tz=UTC))
    started = await adv.start_session(
        adv.StartFormSessionCommand(
            tenant_id=tenant,
            actor_id=actor,
            actor_roles=("reviewer",),
            case_id=case.id,
            form_key="MultiStep",
        ),
        case_repo=case_repo,
        form_binding_repo=binding_repo,
        form_version_loader=fv_loader,
        session_repo=session_repo,
        clock=clock,
    )
    with pytest.raises(Forbidden):
        await adv.save_step(
            adv.SaveFormStepCommand(
                tenant_id=tenant,
                actor_id=other_actor,
                actor_roles=("reviewer",),
                case_id=case.id,
                session_id=started.session.id,
                step_key="basics",
                data={},
            ),
            case_repo=case_repo,
            form_version_loader=fv_loader,
            session_repo=session_repo,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# Acceptance: state-specific form validates transition (P07-US7)
# ---------------------------------------------------------------------------


def _workflow_version() -> WorkflowVersion:
    states = (
        WorkflowState(key="intake", label="Intake", is_initial=True),
        WorkflowState(key="in_review", label="In Review"),
        WorkflowState(key="decided", label="Decided", is_terminal=True),
    )
    transitions = (
        WorkflowTransition(
            key="submit_for_review",
            from_state="intake",
            to_state="in_review",
        ),
        WorkflowTransition(
            key="record_decision",
            from_state="in_review",
            to_state="decided",
        ),
    )
    return WorkflowVersion.draft(
        tenant_id=uuid4(),
        workflow_definition_id=uuid4(),
        version_number=1,
        states=states,
        transitions=transitions,
    ).publish(actor_id=uuid4(), at=datetime.now(tz=UTC))


def test_validate_transition_forms_rejects_missing_required() -> None:
    """Acceptance: state-specific form enforces transition guard before submit."""
    version = _workflow_version()
    submit_for_review = next(t for t in version.transitions if t.key == "submit_for_review")
    requirements = (
        adv.StateFormRequirement(form_key="IntakeChecklist", state_key="in_review", required=True),
    )
    verdict = adv.validate_transition_forms(
        transition=submit_for_review,
        target_state_requirements=requirements,
        submitted_form_keys=(),
    )
    assert verdict.allowed is False
    assert verdict.missing == ("IntakeChecklist",)


def test_validate_transition_forms_allows_when_submitted() -> None:
    version = _workflow_version()
    submit_for_review = next(t for t in version.transitions if t.key == "submit_for_review")
    requirements = (
        adv.StateFormRequirement(form_key="IntakeChecklist", state_key="in_review", required=True),
    )
    verdict = adv.validate_transition_forms(
        transition=submit_for_review,
        target_state_requirements=requirements,
        submitted_form_keys=("IntakeChecklist",),
    )
    assert verdict.allowed is True
    assert verdict.missing == ()


def test_validate_transition_forms_ignores_unrelated_target_state() -> None:
    version = _workflow_version()
    record_decision = next(t for t in version.transitions if t.key == "record_decision")
    requirements = (
        adv.StateFormRequirement(form_key="IntakeChecklist", state_key="in_review", required=True),
    )
    verdict = adv.validate_transition_forms(
        transition=record_decision,
        target_state_requirements=requirements,
        submitted_form_keys=(),
    )
    assert verdict.allowed is True


def test_validate_transition_forms_rejects_undeclared_state() -> None:
    version = _workflow_version()
    submit_for_review = next(t for t in version.transitions if t.key == "submit_for_review")
    requirements = (
        adv.StateFormRequirement(form_key="GhostForm", state_key="ghost_state", required=True),
    )
    with pytest.raises(ValidationFailed):
        adv.validate_transition_forms(
            transition=submit_for_review,
            target_state_requirements=requirements,
            submitted_form_keys=(),
            declared_states=version.state_keys(),
        )


def test_validate_transition_forms_handles_empty_requirements() -> None:
    version = _workflow_version()
    submit_for_review = next(t for t in version.transitions if t.key == "submit_for_review")
    verdict = adv.validate_transition_forms(
        transition=submit_for_review,
        target_state_requirements=(),
        submitted_form_keys=(),
    )
    assert verdict.allowed is True


# ---------------------------------------------------------------------------
# derive_state_requirements
# ---------------------------------------------------------------------------


def test_derive_state_requirements_parses_binding_payload() -> None:
    binding = FormBinding.draft(
        tenant_id=uuid4(),
        case_type_version_id=uuid4(),
        form_version_id=uuid4(),
        state_forms=(
            {"form_key": "A", "state_key": "in_review", "required": True},
            {"form_key": "B", "state_key": "decided", "required": False},
        ),
    )
    requirements = adv.derive_state_requirements(binding)
    assert {r.form_key for r in requirements} == {"A", "B"}
    assert {r.state_key for r in requirements} == {"in_review", "decided"}


def test_derive_state_requirements_handles_missing() -> None:
    binding = FormBinding.draft(
        tenant_id=uuid4(),
        case_type_version_id=uuid4(),
        form_version_id=uuid4(),
    )
    assert adv.derive_state_requirements(binding) == ()


# ---------------------------------------------------------------------------
# derive_steps
# ---------------------------------------------------------------------------


def test_derive_steps_falls_back_to_single_step() -> None:
    sections = (FormSectionSpec(key="only", label="Only", fields=()),)
    definition = FormDefinition(sections=sections, validations=())
    steps = adv.derive_steps(definition)
    assert len(steps) == 1
    assert steps[0].section_keys == ("only",)


def test_derive_steps_uses_explicit_steps() -> None:
    sections = (
        FormSectionSpec(key="a", label="A", fields=()),
        FormSectionSpec(key="b", label="B", fields=()),
        FormSectionSpec(key="c", label="C", fields=()),
    )
    definition = FormDefinition(
        sections=sections,
        validations=(),
        steps=(
            {"step_key": "s1", "label": "S1", "section_keys": ["a", "b"]},
            {"step_key": "s2", "label": "S2", "section_keys": ["c"]},
        ),
    )
    steps = adv.derive_steps(definition)
    assert [s.step_key for s in steps] == ["s1", "s2"]
    assert steps[0].section_keys == ("a", "b")


# ---------------------------------------------------------------------------
# NotFound seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_session_raises_not_found_cross_tenant() -> None:
    session_repo = _FakeFormSessionRepo()
    other_tenant = _tenant()
    session = adv.FormSession(
        id=uuid4(),
        tenant_id=other_tenant,
        case_id=uuid4(),
        form_version_id=uuid4(),
        actor_id=uuid4(),
        current_step_index=0,
        completed_steps=frozenset(),
        step_data={},
        status="in_progress",
    )
    session_repo._sessions[session.id] = session  # noqa: SLF001 - test seam
    with pytest.raises(NotFound):
        await adv._load_session(session_repo, _tenant(), session.id)  # noqa: SLF001
