"""Acceptance tests for resumable multi-step submission + transition gate.

Covers the task acceptance criteria:

- resume is idempotent (repeat resume/start returns the same session,
  no side effects);
- stale version returns conflict (session token on save and submit,
  case token on submit);
- transition commits only with a valid form (incomplete/invalid
  session payloads fail before any write; the workflow engine
  rejects transitions whose target state has unsubmitted required
  forms);
- hidden fields do not leak (conditionally-hidden answers stay
  server-side; the read projection exposes only visible data);
- workflow behavior is deterministic (missing-form verdicts are
  sorted and repeatable).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.forms import advanced_execution as adv
from app.application.workflow import execute_transition as transition_uc
from app.application.workflow.execute_transition import ExecuteTransitionCommand, GuardBlocked
from app.domain.audit import CASE_UPDATED
from app.domain.case import Case, CaseReference
from app.domain.case_type import CaseTypeField, CaseTypeSchema, CaseTypeVersion
from app.domain.errors import Conflict, ValidationFailed
from app.domain.form import (
    FormBinding,
    FormDefinition,
    FormFieldSpec,
    FormSectionSpec,
    FormVersion,
)
from tests.application.workflow._execute_transition_fakes import (
    FixedClock,
    InMemoryAuditStore,
    InMemoryCaseRepository,
    InMemoryOutboxWriter,
    InMemoryTaskRepository,
    InMemoryWorkflowInstanceRepository,
    InMemoryWorkflowVersionRepository,
    StoredInstance,
    make_published_workflow,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
TENANT = UUID("11111111-1111-1111-1111-111111111111")
ACTOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ROLES = ("reviewer",)


# ---------------------------------------------------------------------------
# Fakes (form-session side)
# ---------------------------------------------------------------------------


@dataclass
class _Clock:
    now_value: datetime

    def now(self) -> datetime:
        return self.now_value


class _FakeCaseRepo:
    """Tenant-scoped case store with replace semantics."""

    def __init__(self) -> None:
        self._cases: dict[UUID, Case] = {}

    async def get(self, tenant_id: UUID, case_id: UUID) -> Case | None:
        case = self._cases.get(case_id)
        if case is not None and case.tenant_id == tenant_id:
            return case
        return None

    async def update(self, case: Case) -> None:
        self._cases[case.id] = case

    def add(self, case: Case) -> None:
        self._cases[case.id] = case


class _FakeCaseTypeVersionRepo:
    def __init__(self) -> None:
        self._versions: dict[UUID, CaseTypeVersion] = {}

    async def get(self, tenant_id: UUID, version_id: UUID) -> CaseTypeVersion | None:
        version = self._versions.get(version_id)
        if version is not None and version.tenant_id == tenant_id:
            return version
        return None

    def add(self, version: CaseTypeVersion) -> None:
        self._versions[version.id] = version


class _FakeFormBindingRepo:
    def __init__(self) -> None:
        self._bindings: dict[str, FormBinding] = {}

    async def get_published_for_key(self, tenant_id: UUID, form_key: str) -> FormBinding | None:
        binding = self._bindings.get(form_key)
        if binding is not None and binding.tenant_id == tenant_id:
            return binding
        return None

    def add(self, form_key: str, binding: FormBinding) -> None:
        self._bindings[form_key] = binding


class _FakeFormVersionLoader:
    def __init__(self) -> None:
        self._versions: dict[UUID, FormVersion] = {}

    async def get_by_id(self, tenant_id: UUID, form_version_id: UUID) -> FormVersion | None:
        version = self._versions.get(form_version_id)
        if version is not None and version.tenant_id == tenant_id:
            return version
        return None

    def add(self, version: FormVersion) -> None:
        self._versions[version.id] = version


class _FakeFormSessionRepo:
    def __init__(self) -> None:
        self._sessions: dict[UUID, adv.FormSession] = {}

    async def get(self, tenant_id: UUID, session_id: UUID) -> adv.FormSession | None:
        session = self._sessions.get(session_id)
        if session is not None and session.tenant_id == tenant_id:
            return session
        return None

    async def get_open_for_case_and_form(
        self,
        tenant_id: UUID,
        case_id: UUID,
        form_version_id: UUID,
        actor_id: UUID,
    ) -> adv.FormSession | None:
        for session in self._sessions.values():
            if (
                session.tenant_id == tenant_id
                and session.case_id == case_id
                and session.form_version_id == form_version_id
                and session.actor_id == actor_id
                and session.status == "in_progress"
            ):
                return session
        return None

    async def add(self, session: adv.FormSession) -> None:
        self._sessions[session.id] = session

    async def update(self, session: adv.FormSession) -> None:
        self._sessions[session.id] = session


class _FakeAudit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def append(self, **kwargs: Any) -> None:
        self.rows.append(dict(kwargs))


class _FakeOutbox:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def enqueue(self, **kwargs: Any) -> None:
        self.rows.append(dict(kwargs))


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _two_step_definition() -> FormDefinition:
    """Two required steps: ``basics`` (title + notes) then ``review`` (decision)."""
    sections = (
        FormSectionSpec(
            key="basics",
            label="Basics",
            fields=(
                FormFieldSpec(key="title", label="Title", required=True),
                FormFieldSpec(key="notes", label="Notes"),
            ),
        ),
        FormSectionSpec(
            key="review",
            label="Review",
            fields=(FormFieldSpec(key="decision", label="Decision", required=True),),
        ),
    )
    steps = (
        {"step_key": "basics", "label": "Basics", "section_keys": ["basics"]},
        {"step_key": "review", "label": "Review", "section_keys": ["review"]},
    )
    return FormDefinition(sections=sections, validations=(), steps=steps)


def _conditional_definition() -> FormDefinition:
    """``review`` is hidden unless ``needs_review`` is truthy."""
    sections = (
        FormSectionSpec(
            key="basics",
            label="Basics",
            fields=(
                FormFieldSpec(key="title", label="Title", required=True),
                FormFieldSpec(key="needs_review", label="Needs review", field_type="boolean"),
            ),
        ),
        FormSectionSpec(
            key="review",
            label="Review",
            fields=(FormFieldSpec(key="decision", label="Decision"),),
            visible_when={"truthy": {"field": "needs_review"}},
        ),
    )
    steps = (
        {"step_key": "basics", "label": "Basics", "section_keys": ["basics"]},
        {"step_key": "review", "label": "Review", "section_keys": ["review"]},
    )
    return FormDefinition(sections=sections, validations=(), steps=steps)


@dataclass
class _Fixtures:
    case: Case
    case_repo: _FakeCaseRepo
    ctv_repo: _FakeCaseTypeVersionRepo
    binding_repo: _FakeFormBindingRepo
    version_loader: _FakeFormVersionLoader
    session_repo: _FakeFormSessionRepo
    audit: _FakeAudit
    outbox: _FakeOutbox
    clock: _Clock


def _fixtures(*, definition: FormDefinition | None = None) -> _Fixtures:
    """Build a real case + published binding for the two-step form."""
    schema = CaseTypeSchema(
        fields=(
            CaseTypeField(key="title", label="Title", type="text", required=True),
            CaseTypeField(key="notes", label="Notes", type="text"),
            CaseTypeField(key="decision", label="Decision", type="text"),
            CaseTypeField(key="needs_review", label="Needs review", type="boolean"),
        )
    )
    ctv = CaseTypeVersion.draft(
        tenant_id=TENANT,
        case_type_id=uuid4(),
        version_number=1,
        schema=schema,
        now=NOW,
    ).publish(actor_id=ACTOR, at=NOW)
    form_version = FormVersion.draft(
        tenant_id=TENANT,
        key="MultiStep",
        version_number=1,
        definition=definition or _two_step_definition(),
        now=NOW,
    ).publish(actor_id=ACTOR, at=NOW)
    binding = FormBinding.draft(
        tenant_id=TENANT,
        case_type_version_id=ctv.id,
        form_version_id=form_version.id,
        now=NOW,
    ).publish(actor_id=ACTOR, at=NOW)
    case = Case.create(
        tenant_id=TENANT,
        reference=CaseReference("C-ACME-2026-000001"),
        case_type_version=ctv,
        title="Service request",
        description="",
        data={"title": "Service request"},
        priority="normal",
        created_by=ACTOR,
        now=NOW,
    )
    case_repo = _FakeCaseRepo()
    case_repo.add(case)
    ctv_repo = _FakeCaseTypeVersionRepo()
    ctv_repo.add(ctv)
    binding_repo = _FakeFormBindingRepo()
    binding_repo.add("MultiStep", binding)
    version_loader = _FakeFormVersionLoader()
    version_loader.add(form_version)
    return _Fixtures(
        case=case,
        case_repo=case_repo,
        ctv_repo=ctv_repo,
        binding_repo=binding_repo,
        version_loader=version_loader,
        session_repo=_FakeFormSessionRepo(),
        audit=_FakeAudit(),
        outbox=_FakeOutbox(),
        clock=_Clock(NOW),
    )


async def _start(fx: _Fixtures) -> adv.FormSessionView:
    """Start a session for the standard fixture triple."""
    return await adv.start_session(
        adv.StartFormSessionCommand(
            tenant_id=TENANT,
            actor_id=ACTOR,
            actor_roles=ROLES,
            case_id=fx.case.id,
            form_key="MultiStep",
        ),
        case_repo=fx.case_repo,
        form_binding_repo=fx.binding_repo,
        form_version_loader=fx.version_loader,
        session_repo=fx.session_repo,
        clock=fx.clock,
    )


async def _save(
    fx: _Fixtures,
    session_id: UUID,
    step_key: str,
    data: dict[str, Any],
    *,
    token: int,
) -> adv.FormSessionView:
    """Save one step with an explicit concurrency token."""
    return await adv.save_step(
        adv.SaveFormStepCommand(
            tenant_id=TENANT,
            actor_id=ACTOR,
            actor_roles=ROLES,
            case_id=fx.case.id,
            session_id=session_id,
            step_key=step_key,
            data=data,
            expected_version_token=token,
        ),
        case_repo=fx.case_repo,
        form_version_loader=fx.version_loader,
        session_repo=fx.session_repo,
        clock=fx.clock,
    )


async def _submit(
    fx: _Fixtures,
    session_id: UUID,
    *,
    session_token: int,
    case_token: int,
) -> adv.SubmitFormSessionResult:
    """Submit a session with explicit session + case tokens."""
    return await adv.submit_session(
        adv.SubmitFormSessionCommand(
            tenant_id=TENANT,
            actor_id=ACTOR,
            actor_roles=ROLES,
            case_id=fx.case.id,
            session_id=session_id,
            expected_session_token=session_token,
            expected_case_version_token=case_token,
        ),
        case_repo=fx.case_repo,
        case_type_version_repo=fx.ctv_repo,
        form_version_loader=fx.version_loader,
        session_repo=fx.session_repo,
        audit=fx.audit,
        outbox=fx.outbox,
        clock=fx.clock,
    )


# ---------------------------------------------------------------------------
# Acceptance: resume is idempotent
# ---------------------------------------------------------------------------


async def test_resume_session__repeat_calls__same_session_no_side_effects() -> None:
    """Repeat resume/start returns the same session; progress is untouched."""
    # // given a session with one saved step.
    fx = _fixtures()
    started = await _start(fx)
    after_save = await _save(fx, started.session.id, "basics", {"title": "Draft"}, token=1)

    # // when resuming twice, then starting again.
    query = adv.ResumeFormSessionQuery(
        tenant_id=TENANT,
        actor_id=ACTOR,
        actor_roles=ROLES,
        case_id=fx.case.id,
        form_key="MultiStep",
    )
    first = await adv.resume_session(
        query,
        case_repo=fx.case_repo,
        form_binding_repo=fx.binding_repo,
        form_version_loader=fx.version_loader,
        session_repo=fx.session_repo,
    )
    second = await adv.resume_session(
        query,
        case_repo=fx.case_repo,
        form_binding_repo=fx.binding_repo,
        form_version_loader=fx.version_loader,
        session_repo=fx.session_repo,
    )
    restarted = await _start(fx)

    # // then every handle resolves the same in-progress session.
    assert first is not None
    assert second is not None
    assert first.session.id == after_save.session.id
    assert second.session.id == after_save.session.id
    assert restarted.session.id == after_save.session.id
    assert first.session.version_token == after_save.session.version_token == 2
    assert second.session.version_token == 2
    assert first.session.status == "in_progress"
    assert first.current_step is not None
    assert first.current_step.step_key == "review"
    assert [status for _, status in first.progress] == ["completed", "current"]
    assert len(fx.session_repo._sessions) == 1  # noqa: SLF001 -- single row, no duplicate


async def test_save_step__stale_token__conflict_and_no_write() -> None:
    """A save echoing a superseded session token is a conflict; data stays."""
    # // given one saved step (token now 2).
    fx = _fixtures()
    started = await _start(fx)
    await _save(fx, started.session.id, "basics", {"title": "v1"}, token=1)

    # // when replaying the save with the superseded token.
    with pytest.raises(Conflict):
        await _save(fx, started.session.id, "basics", {"title": "stale"}, token=1)

    # // then the first payload is intact and the token did not move.
    stored = await fx.session_repo.get(TENANT, started.session.id)
    assert stored is not None
    assert stored.step_data["title"] == "v1"
    assert stored.version_token == 2


# ---------------------------------------------------------------------------
# Acceptance: transition commits only with a valid form
# ---------------------------------------------------------------------------


async def test_submit_session__incomplete_steps__validation_failed_case_untouched() -> None:
    """Submitting with a required step missing names it; the case is untouched."""
    # // given progress on step one of two.
    fx = _fixtures()
    started = await _start(fx)
    await _save(fx, started.session.id, "basics", {"title": "Draft"}, token=1)

    # // when submitting before the required review step.
    with pytest.raises(ValidationFailed) as exc_info:
        await _submit(fx, started.session.id, session_token=2, case_token=1)

    # // then the missing step is named and nothing was staged.
    assert "review" in str(exc_info.value)
    stored_case = await fx.case_repo.get(TENANT, fx.case.id)
    assert stored_case is not None
    assert stored_case.data == {"title": "Service request"}
    assert stored_case.version_token == 1
    stored_session = await fx.session_repo.get(TENANT, started.session.id)
    assert stored_session is not None
    assert stored_session.status == "in_progress"
    assert fx.audit.rows == []
    assert fx.outbox.rows == []


async def test_submit_session__missing_required_field__validation_failed_nothing_staged() -> None:
    """All steps saved but a visible required field blank fails; nothing staged."""
    # // given both steps saved, the required decision left blank.
    fx = _fixtures()
    started = await _start(fx)
    await _save(fx, started.session.id, "basics", {"title": "Draft"}, token=1)
    await _save(fx, started.session.id, "review", {}, token=2)

    # // when submitting.
    with pytest.raises(ValidationFailed) as exc_info:
        await _submit(fx, started.session.id, session_token=3, case_token=1)

    # // then the offender is named and the case/audit/outbox are untouched.
    assert "decision" in str(exc_info.value)
    stored_case = await fx.case_repo.get(TENANT, fx.case.id)
    assert stored_case is not None
    assert stored_case.data == {"title": "Service request"}
    assert stored_case.version_token == 1
    assert fx.audit.rows == []
    assert fx.outbox.rows == []


async def test_submit_session__valid_form__commits_case_audit_outbox_and_closes_session() -> None:
    """A valid accumulated payload commits to the case and closes the session."""
    # // given both steps saved with valid data.
    fx = _fixtures()
    started = await _start(fx)
    await _save(fx, started.session.id, "basics", {"title": "New title", "notes": "n"}, token=1)
    await _save(fx, started.session.id, "review", {"decision": "approve"}, token=2)

    # // when submitting with fresh tokens.
    result = await _submit(fx, started.session.id, session_token=3, case_token=1)

    # // then the case carries the merged payload with a bumped token.
    assert result.case.data == {"title": "New title", "notes": "n", "decision": "approve"}
    assert result.case.version_token == 2
    stored_case = await fx.case_repo.get(TENANT, fx.case.id)
    assert stored_case is not None
    assert stored_case.data == result.case.data

    # // then one audit + one outbox row landed in the same transaction.
    assert len(fx.audit.rows) == 1
    assert fx.audit.rows[0]["event_type"] == CASE_UPDATED
    assert fx.audit.rows[0]["payload"]["form_session_id"] == str(started.session.id)
    assert len(fx.outbox.rows) == 1
    assert fx.outbox.rows[0]["event_type"] == CASE_UPDATED

    # // then the session is terminal with deterministic progress.
    assert result.session_view.session.status == "submitted"
    assert result.session_view.session.version_token == 4
    assert result.session_view.current_step is None
    assert [status for _, status in result.session_view.progress] == [
        "completed",
        "completed",
    ]


async def test_submit_session__replay__idempotent_no_second_write() -> None:
    """Re-submitting a submitted session returns the stored view; no second write."""
    # // given a submitted session.
    fx = _fixtures()
    started = await _start(fx)
    await _save(fx, started.session.id, "basics", {"title": "New title"}, token=1)
    await _save(fx, started.session.id, "review", {"decision": "approve"}, token=2)
    first = await _submit(fx, started.session.id, session_token=3, case_token=1)

    # // when submitting again (even echoing the now-superseded token).
    second = await _submit(fx, started.session.id, session_token=3, case_token=1)

    # // then the stored terminal view returns with no further side effects.
    assert second.session_view.session.id == first.session_view.session.id
    assert second.session_view.session.status == "submitted"
    assert second.session_view.session.version_token == 4
    stored_case = await fx.case_repo.get(TENANT, fx.case.id)
    assert stored_case is not None
    assert stored_case.version_token == 2
    assert len(fx.audit.rows) == 1
    assert len(fx.outbox.rows) == 1


async def test_submit_session__stale_tokens__conflict_and_nothing_staged() -> None:
    """Stale session or case tokens are conflicts; the case is untouched."""
    # // given a fully-saved session.
    fx = _fixtures()
    started = await _start(fx)
    await _save(fx, started.session.id, "basics", {"title": "New title"}, token=1)
    await _save(fx, started.session.id, "review", {"decision": "approve"}, token=2)

    # // when submitting with a stale session token.
    with pytest.raises(Conflict):
        await _submit(fx, started.session.id, session_token=1, case_token=1)

    # // when submitting with a stale case token.
    with pytest.raises(Conflict):
        await _submit(fx, started.session.id, session_token=3, case_token=999)

    # // then nothing was staged by either attempt.
    stored_case = await fx.case_repo.get(TENANT, fx.case.id)
    assert stored_case is not None
    assert stored_case.data == {"title": "Service request"}
    assert stored_case.version_token == 1
    stored_session = await fx.session_repo.get(TENANT, started.session.id)
    assert stored_session is not None
    assert stored_session.status == "in_progress"
    assert fx.audit.rows == []
    assert fx.outbox.rows == []


# ---------------------------------------------------------------------------
# Acceptance: hidden fields do not leak
# ---------------------------------------------------------------------------


async def test_session_view__hidden_section_answers__stripped_from_visible_data() -> None:
    """Conditionally-hidden answers stay server-side; the view exposes the rest."""
    # // given a conditional form where review hides while needs_review is falsy.
    fx = _fixtures(definition=_conditional_definition())
    started = await _start(fx)
    await _save(fx, started.session.id, "basics", {"title": "T", "needs_review": False}, token=1)
    view = await _save(fx, started.session.id, "review", {"decision": "sneaky"}, token=2)

    # // then the stored accumulation preserves the answer ...
    assert view.session.step_data["decision"] == "sneaky"
    # // ... but the worker-visible projection strips the hidden section.
    assert "decision" not in view.visible_step_data
    assert view.visible_step_data["title"] == "T"

    # // when the worker flips the predicate, the answer becomes visible again.
    reshown = await _save(fx, started.session.id, "basics", {"needs_review": True}, token=3)
    assert reshown.session.step_data["decision"] == "sneaky"
    assert reshown.visible_step_data["decision"] == "sneaky"


# ---------------------------------------------------------------------------
# Acceptance: transition commits only with a valid form + determinism
# ---------------------------------------------------------------------------


async def _seed_workflow() -> dict[str, Any]:
    """Seed a case + intake workflow instance with the transition fakes."""
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    instance_repo = InMemoryWorkflowInstanceRepository()
    version_repo = InMemoryWorkflowVersionRepository()
    definition, version = make_published_workflow(
        tenant_id=TENANT,
        transition_keys=(("submit_for_review", "intake", "in_review"),),
    )
    ctv = CaseTypeVersion.draft(
        tenant_id=TENANT,
        case_type_id=uuid4(),
        version_number=1,
        schema=CaseTypeSchema(
            fields=(CaseTypeField(key="title", label="Title", type="text", required=True),)
        ),
        now=NOW,
    ).publish(actor_id=ACTOR, at=NOW)
    case = Case.create(
        tenant_id=TENANT,
        reference=CaseReference("C-ACME-2026-000001"),
        case_type_version=ctv,
        title="Service request",
        description="",
        data={"title": "Service request"},
        priority="normal",
        created_by=ACTOR,
        now=NOW,
    )
    case_repo.add(case)
    instance = StoredInstance(
        id=uuid4(),
        tenant_id=TENANT,
        case_id=case.id,
        workflow_definition_id=definition.id,
        workflow_version_id=version.id,
        current_state="intake",
        correlation_id="corr-1",
        version_token=1,
        created_at=NOW,
        updated_at=NOW,
    )
    instance_repo.add(instance)
    await version_repo.add(version)
    return {
        "case": case,
        "instance": instance,
        "case_repo": case_repo,
        "task_repo": task_repo,
        "instance_repo": instance_repo,
        "version_repo": version_repo,
    }


def _transition_cmd(case_id: UUID) -> ExecuteTransitionCommand:
    """Build the intake -> in_review command for the seeded workflow."""
    return ExecuteTransitionCommand(
        tenant_id=TENANT,
        actor_id=ACTOR,
        actor_roles=ROLES,
        case_id=case_id,
        transition_key="submit_for_review",
        correlation_id="corr-form-gate",
    )


async def test_execute_transition__missing_required_form__blocked_no_state_change() -> None:
    """A transition landing where a required form is unsubmitted is rejected cleanly."""
    # // given a transition whose target state requires an unsubmitted form.
    seed = await _seed_workflow()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock(NOW)

    # // when executing without the required form submitted.
    with pytest.raises(GuardBlocked) as exc_info:
        await transition_uc.handle(
            _transition_cmd(seed["case"].id),
            case_repo=seed["case_repo"],
            instance_repo=seed["instance_repo"],
            workflow_version_repo=seed["version_repo"],
            task_repo=seed["task_repo"],
            audit=audit,
            outbox=outbox,
            clock=clock,
            state_form_requirements=(
                {"form_key": "IntakeChecklist", "state_key": "in_review", "required": True},
            ),
            submitted_form_keys=(),
            declared_workflow_states=("intake", "in_review", "decided", "closed"),
        )

    # // then the verdict names the missing form and nothing was staged.
    assert exc_info.value.reason == "missing_required_form"
    assert exc_info.value.details["missing_form_keys"] == ["IntakeChecklist"]
    stored_instance = await seed["instance_repo"].get_for_case(TENANT, seed["case"].id)
    assert stored_instance is not None
    assert stored_instance.current_state == "intake"
    stored_case = await seed["case_repo"].get(TENANT, seed["case"].id)
    assert stored_case is not None
    assert stored_case.current_workflow_state != "in_review"
    assert audit.events == []
    assert outbox.records == []


async def test_execute_transition__required_form_submitted__advances() -> None:
    """The same transition commits once the required form is submitted."""
    # // given the required form recorded as submitted.
    seed = await _seed_workflow()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock(NOW)

    # // when executing with the form key present.
    result = await transition_uc.handle(
        _transition_cmd(seed["case"].id),
        case_repo=seed["case_repo"],
        instance_repo=seed["instance_repo"],
        workflow_version_repo=seed["version_repo"],
        task_repo=seed["task_repo"],
        audit=audit,
        outbox=outbox,
        clock=clock,
        state_form_requirements=(
            {"form_key": "IntakeChecklist", "state_key": "in_review", "required": True},
        ),
        submitted_form_keys=("IntakeChecklist",),
        declared_workflow_states=("intake", "in_review", "decided", "closed"),
    )

    # // then the transition committed.
    assert result.to_state == "in_review"
    assert len(audit.events) == 1
    assert len(outbox.records) == 1


async def test_execute_transition__form_gate__deterministic_and_sorted() -> None:
    """Repeat gate rejections are identical; multiple missing keys sort stably."""
    # // given two missing required forms declared in reverse order.
    requirements = (
        {"form_key": "ZebraForm", "state_key": "in_review", "required": True},
        {"form_key": "AlphaForm", "state_key": "in_review", "required": True},
    )
    first: GuardBlocked | None = None
    for _ in range(2):
        seed = await _seed_workflow()
        with pytest.raises(GuardBlocked) as exc_info:
            await transition_uc.handle(
                _transition_cmd(seed["case"].id),
                case_repo=seed["case_repo"],
                instance_repo=seed["instance_repo"],
                workflow_version_repo=seed["version_repo"],
                task_repo=seed["task_repo"],
                audit=InMemoryAuditStore(),
                outbox=InMemoryOutboxWriter(),
                clock=FixedClock(NOW),
                state_form_requirements=requirements,
                submitted_form_keys=(),
                declared_workflow_states=("intake", "in_review"),
            )
        # // then missing keys sort deterministically on every attempt.
        assert exc_info.value.details["missing_form_keys"] == ["AlphaForm", "ZebraForm"]
        if first is None:
            first = exc_info.value
        else:
            assert str(exc_info.value) == str(first)
            assert exc_info.value.details == first.details


async def test_execute_transition__no_form_gate__legacy_callers_unchanged() -> None:
    """Without form requirements the engine behaves exactly as before."""
    # // given a plain transition with no gate wired.
    seed = await _seed_workflow()

    # // when executing.
    result = await transition_uc.handle(
        _transition_cmd(seed["case"].id),
        case_repo=seed["case_repo"],
        instance_repo=seed["instance_repo"],
        workflow_version_repo=seed["version_repo"],
        task_repo=seed["task_repo"],
        audit=InMemoryAuditStore(),
        outbox=InMemoryOutboxWriter(),
        clock=FixedClock(NOW),
    )

    # // then it commits.
    assert result.to_state == "in_review"
