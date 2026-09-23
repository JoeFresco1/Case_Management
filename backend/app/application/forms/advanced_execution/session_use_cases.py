"""Multi-step form session use cases: start / save / submit / resume (P07-US6).

The four verbs a worker's SPA drives over a :class:`FormSession`. All
commands, the view projection, and shared helpers live in
:mod:`session_model`; this module holds only the async orchestration so
each file stays well under the R2 soft limit.

Transaction shape: every mutation here writes through the
:class:`~session_model.FormSessionRepository` port in the caller's
transaction; the final :func:`submit_session` additionally appends the
audit row + outbox row + case update atomically (the caller commits once).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.application.authorization import Action, Resource, authorize
from app.application.forms.intake_submission import (
    apply_calculated_fields,
    validate_visible_fields_deterministic,
)
from app.domain.audit import CASE_UPDATED
from app.domain.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.domain.form import FormVersionStatus, apply_submission_preservation, read_only_field_keys

from .session_model import (
    FormSession,
    FormSessionRepository,
    FormSessionView,
    FormStep,
    ResumeFormSessionQuery,
    SaveFormStepCommand,
    StartFormSessionCommand,
    SubmitFormSessionCommand,
    SubmitFormSessionResult,
    _actor_proxy,
    _ClockLike,
    _hidden_keys_for_sections,
    _index_of_step,
    _load_session,
    _merge_step_data,
    _sections_for_step,
    _to_view,
    derive_steps,
)


async def start_session(
    cmd: StartFormSessionCommand,
    *,
    case_repo: Any,
    form_binding_repo: Any,
    form_version_loader: Any,
    session_repo: FormSessionRepository,
    clock: _ClockLike,
) -> FormSessionView:
    """Begin or resume a multi-step form session (P07-US6).

    Reuses an existing open session when one already exists for the
    (case, form, actor) triple — that is the "resume after partial"
    acceptance criterion. A published form is required; the same
    binding contract as :func:`submit_form.handle` applies.

    Raises:
        Forbidden: actor lacks ``CASE_UPDATE`` on the case.
        NotFound: case, binding, or form version missing.
        ValidationFailed: form is not published, binding does not
            match the case's case-type version, or form has no steps.
    """
    await authorize(
        Action.CASE_UPDATE,
        _actor_proxy(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="case", id=cmd.case_id),
    )
    case = await case_repo.get(cmd.tenant_id, cmd.case_id)
    if case is None:
        raise NotFound(f"case {cmd.case_id} not found")
    binding = await form_binding_repo.get_published_for_key(cmd.tenant_id, cmd.form_key)
    if binding is None:
        raise ValidationFailed(f"no published form binding for {cmd.form_key!r}")
    form_version = await form_version_loader.get_by_id(cmd.tenant_id, binding.form_version_id)
    if form_version is None:
        raise NotFound(f"form version {binding.form_version_id} not found")
    if form_version.status is not FormVersionStatus.PUBLISHED:
        raise ValidationFailed(
            f"form {form_version.key!r} is not published (status={form_version.status.value})"
        )
    if binding.case_type_version_id != case.case_type_version_id:
        raise ValidationFailed("form binding does not match the case's case-type version")
    existing = await session_repo.get_open_for_case_and_form(
        cmd.tenant_id,
        case.id,
        form_version.id,
        cmd.actor_id,
    )
    if existing is not None:
        steps = derive_steps(form_version.definition)
        return _to_view(existing, steps, form_version=form_version)
    steps = derive_steps(form_version.definition)
    if not steps:
        raise ValidationFailed("form has no steps to begin a session with")
    now = clock.now()
    session = FormSession(
        id=uuid4(),
        tenant_id=cmd.tenant_id,
        case_id=case.id,
        form_version_id=form_version.id,
        actor_id=cmd.actor_id,
        current_step_index=0,
        completed_steps=frozenset(),
        step_data={},
        status="in_progress",
        version_token=1,
        created_at=now,
        updated_at=now,
        submitted_at=None,
    )
    await session_repo.add(session)
    return _to_view(session, steps, form_version=form_version)


async def save_step(
    cmd: SaveFormStepCommand,
    *,
    case_repo: Any,
    form_version_loader: Any,
    session_repo: FormSessionRepository,
    clock: _ClockLike,
) -> FormSessionView:
    """Persist partial progress on a multi-step form (P07-US6).

    Advances ``current_step_index`` by one and marks ``step_key`` as
    completed. Concurrency: the session carries a ``version_token``
    the caller must echo via ``If-Match``; a stale token raises
    :class:`Conflict` so two simultaneous saves cannot lose updates.

    Required-field validation is *per step* (a worker can save an
    in-progress step with partial data; only the final
    :func:`submit_session` enforces the full set of visible required
    fields and commits to the case). Saving the last step leaves the
    session ``in_progress`` with every step completed — the SPA then
    shows "ready to submit". Hidden-field preservation still applies
    because we use the same merge helper the single-step submit path
    uses. The returned view exposes only ``visible_step_data`` so
    conditionally-hidden answers never leak over the wire.
    """
    session = await _load_session(session_repo, cmd.tenant_id, cmd.session_id)
    if session.actor_id != cmd.actor_id:
        raise Forbidden("form session belongs to a different actor")
    if session.case_id != cmd.case_id:
        raise NotFound("form session does not match case")
    if session.status != "in_progress":
        raise ValidationFailed(f"cannot save step on a {session.status} form session")
    if (
        cmd.expected_version_token is not None
        and session.version_token != cmd.expected_version_token
    ):
        raise Conflict(
            f"stale concurrency token for form session {session.id} "
            f"(expected {session.version_token}, got {cmd.expected_version_token})"
        )
    case = await case_repo.get(cmd.tenant_id, session.case_id)
    if case is None:
        raise NotFound(f"case {session.case_id} not found")
    form_version = await form_version_loader.get_by_id(cmd.tenant_id, session.form_version_id)
    if form_version is None:
        raise NotFound(f"form version {session.form_version_id} not found")
    steps = derive_steps(form_version.definition)
    if not steps:
        raise ValidationFailed("form has no steps")
    step_index = _index_of_step(steps, cmd.step_key)
    if step_index is None:
        raise ValidationFailed(f"unknown step {cmd.step_key!r}")
    if step_index > session.current_step_index:
        raise ValidationFailed(
            f"step {cmd.step_key!r} is ahead of the current step "
            f"(current index {session.current_step_index})"
        )
    step = steps[step_index]
    sections = _sections_for_step(form_version.definition, step)
    visible_section_keys = frozenset(s.key for s in sections)
    hidden_keys = _hidden_keys_for_sections(form_version.definition, visible_section_keys)
    merged = _merge_step_data(
        previous=session.step_data,
        submitted=cmd.data,
        hidden_keys=hidden_keys,
    )
    completed = set(session.completed_steps)
    completed.add(cmd.step_key)
    next_index = min(step_index + 1, len(steps))
    # why: the validated commit lives in :func:`submit_session`, not
    # here. Flipping to ``submitted`` on the last save would mark a
    # potentially-invalid payload as done and let the transition gate
    # treat it as a submitted form. Progress saves stay
    # ``in_progress``; the SPA submits explicitly when every step
    # reads ``completed``.
    new_session = FormSession(
        id=session.id,
        tenant_id=session.tenant_id,
        case_id=session.case_id,
        form_version_id=session.form_version_id,
        actor_id=session.actor_id,
        current_step_index=next_index,
        completed_steps=frozenset(completed),
        step_data=merged,
        status="in_progress",
        version_token=session.version_token + 1,
        created_at=session.created_at,
        updated_at=clock.now(),
        submitted_at=None,
    )
    await session_repo.update(new_session)
    return _to_view(new_session, steps, form_version=form_version)


def _require_all_required_steps_complete(steps: tuple[FormStep, ...], session: FormSession) -> None:
    """Raise :class:`ValidationFailed` unless every required step is complete.

    Missing step keys sort deterministically so two workers see the
    identical message. Extracted from :func:`submit_session` to keep
    the commit path under the branch budget (R2).
    """
    missing_steps = sorted(
        step.step_key
        for step in steps
        if step.required and step.step_key not in session.completed_steps
    )
    if missing_steps:
        raise ValidationFailed(
            "multi-step form is incomplete; required steps missing: " + ", ".join(missing_steps),
            field=",".join(missing_steps),
        )


def _new_correlation_id() -> str:
    """Mint a correlation id for audit/outbox rows when the caller sent none."""
    return str(uuid4())


async def submit_session(
    cmd: SubmitFormSessionCommand,
    *,
    case_repo: Any,
    case_type_version_repo: Any,
    form_version_loader: Any,
    session_repo: FormSessionRepository,
    audit: Any,
    outbox: Any,
    clock: _ClockLike,
) -> SubmitFormSessionResult:
    """Validate a completed multi-step session and commit it to the case.

    Deterministic pipeline (shared with the E-4 single-step path):
    hidden + read-only/system/calculated inbound values are dropped,
    calculated formulas are re-evaluated, then visible required +
    per-field type/range/pattern/enum + form-level rules are
    collected into one field-named error. The case UPDATE, audit row,
    outbox row, and session flip share one transaction; the caller
    commits once.

    Failure behavior:

    - ``Conflict`` (stale session or case token) is raised before any
      write.
    - ``ValidationFailed`` (incomplete required steps, or an invalid
      accumulated form) is raised before any write: the session stays
      ``in_progress`` with progress intact so the worker fixes exactly
      the named fields and retries — entered data is never lost.
    - Re-submitting an already-``submitted`` session is idempotent:
      the stored view is returned with no second case write.

    Raises:
        Forbidden: actor lacks ``CASE_UPDATE`` or the session belongs
            to a different actor.
        NotFound: session, case, case-type version, or form version
            missing (cross-tenant ids surface as ``NotFound``).
        ValidationFailed: session not ``in_progress``, required steps
            incomplete, form unpublished, or the accumulated payload
            fails deterministic validation.
        Conflict: stale session or case concurrency token.
    """
    await authorize(
        Action.CASE_UPDATE,
        _actor_proxy(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="case", id=cmd.case_id),
    )
    session = await _load_session(session_repo, cmd.tenant_id, cmd.session_id)
    if session.actor_id != cmd.actor_id:
        raise Forbidden("form session belongs to a different actor")
    if session.case_id != cmd.case_id:
        raise NotFound("form session does not match case")
    form_version = await form_version_loader.get_by_id(cmd.tenant_id, session.form_version_id)
    if form_version is None:
        raise NotFound(f"form version {session.form_version_id} not found")
    steps = derive_steps(form_version.definition)
    if session.status == "submitted":
        # why: SPA retries and double-clicks must not duplicate the
        # case write. The first commit already landed; replay returns
        # the stored terminal view.
        case = await case_repo.get(cmd.tenant_id, session.case_id)
        if case is None:
            raise NotFound(f"case {session.case_id} not found")
        return SubmitFormSessionResult(
            session_view=_to_view(session, steps, form_version=form_version),
            case=case,
            hidden_keys=frozenset(),
        )
    if session.status != "in_progress":
        raise ValidationFailed(f"cannot submit a {session.status} form session")
    if session.version_token != cmd.expected_session_token:
        raise Conflict(
            f"stale concurrency token for form session {session.id} "
            f"(expected {session.version_token}, got {cmd.expected_session_token})"
        )
    case = await case_repo.get(cmd.tenant_id, session.case_id)
    if case is None:
        raise NotFound(f"case {session.case_id} not found")
    case_type_version = await case_type_version_repo.get(cmd.tenant_id, case.case_type_version_id)
    if case_type_version is None:
        raise NotFound(f"case type version {case.case_type_version_id} not found")
    if form_version.status is not FormVersionStatus.PUBLISHED:
        raise ValidationFailed(
            f"form {form_version.key!r} is not published (status={form_version.status.value})"
        )
    _require_all_required_steps_complete(steps, session)
    definition = form_version.definition
    visible_sections = form_version.visible_section_keys(
        visible_data=case.data,
        submission_data=session.step_data,
    )
    hidden_keys = _hidden_keys_for_sections(definition, visible_sections)
    merged = apply_submission_preservation(
        stored_data=case.data,
        submitted_data=session.step_data,
        hidden_keys=hidden_keys,
        read_only_keys=read_only_field_keys(definition),
    )
    merged, _calculated_keys = apply_calculated_fields(definition, merged)
    validate_visible_fields_deterministic(
        definition=definition,
        visible_sections=visible_sections,
        merged_data=merged,
    )
    now = clock.now()
    # why: the aggregate owns the case token (same primitive as
    # ``PATCH /cases/{id}``), so a peer's concurrent write surfaces
    # as ``Conflict`` here instead of a silent overwrite.
    new_case = case.edit(
        expected_version_token=cmd.expected_case_version_token,
        data=merged,
        case_type_version=case_type_version,
        now=now,
    )
    if new_case.version_token != case.version_token:
        await case_repo.update(new_case)
        await audit.append(
            tenant_id=cmd.tenant_id,
            event_type=CASE_UPDATED,
            actor_id=cmd.actor_id,
            actor_type="user",
            subject_type="case",
            subject_id=new_case.id,
            case_id=new_case.id,
            correlation_id=cmd.correlation_id or _new_correlation_id(),
            causation_id=cmd.causation_id,
            payload={
                "form_key": form_version.key,
                "form_version_id": str(form_version.id),
                "form_session_id": str(session.id),
                "multi_step": True,
                "hidden_keys": sorted(hidden_keys),
                "version_token": new_case.version_token,
                "previous_version_token": case.version_token,
            },
            occurred_at=now,
        )
        await outbox.enqueue(
            tenant_id=cmd.tenant_id,
            event_type=CASE_UPDATED,
            event_id=uuid4(),
            payload={
                "case_id": str(new_case.id),
                "tenant_id": str(cmd.tenant_id),
                "actor_id": str(cmd.actor_id),
                "form_key": form_version.key,
                "form_session_id": str(session.id),
                "version_token": new_case.version_token,
                "occurred_at": now.isoformat(),
            },
        )
    else:
        new_case = case
    submitted_session = FormSession(
        id=session.id,
        tenant_id=session.tenant_id,
        case_id=session.case_id,
        form_version_id=session.form_version_id,
        actor_id=session.actor_id,
        current_step_index=len(steps),
        completed_steps=session.completed_steps,
        step_data=session.step_data,
        status="submitted",
        version_token=session.version_token + 1,
        created_at=session.created_at,
        updated_at=now,
        submitted_at=now,
    )
    await session_repo.update(submitted_session)
    return SubmitFormSessionResult(
        session_view=_to_view(submitted_session, steps, form_version=form_version),
        case=new_case,
        hidden_keys=hidden_keys,
    )


async def resume_session(
    query: ResumeFormSessionQuery,
    *,
    case_repo: Any,
    form_binding_repo: Any,
    form_version_loader: Any,
    session_repo: FormSessionRepository,
) -> FormSessionView | None:
    """Return the open session for the (case, form, actor) triple, or ``None``.

    No side effects: callers use this to decide between starting a new
    session and restoring the in-progress one. Resume is idempotent —
    repeated calls return the same session with the same progress and
    never create, advance, or duplicate work.

    Fail-closed: the actor must hold ``CASE_UPDATE`` on the case and
    the case must exist in-tenant; otherwise ``Forbidden``/``None``
    (never a cross-tenant existence oracle). A session stored against
    a different case never matches this query's case.
    """
    await authorize(
        Action.CASE_UPDATE,
        _actor_proxy(query),
        Resource(tenant_id=query.tenant_id, kind="case", id=query.case_id),
    )
    case = await case_repo.get(query.tenant_id, query.case_id)
    if case is None:
        return None
    binding = await form_binding_repo.get_published_for_key(query.tenant_id, query.form_key)
    if binding is None:
        return None
    form_version = await form_version_loader.get_by_id(query.tenant_id, binding.form_version_id)
    if form_version is None:
        return None
    session = await session_repo.get_open_for_case_and_form(
        query.tenant_id,
        query.case_id,
        form_version.id,
        query.actor_id,
    )
    if session is None:
        return None
    if session.case_id != query.case_id:
        return None
    return _to_view(session, derive_steps(form_version.definition), form_version=form_version)
