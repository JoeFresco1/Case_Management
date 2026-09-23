"""Multi-step form session model, ports, commands, and shared helpers (P07-US6).

Holds the durable shape of resumable form progress: :class:`FormStep`,
:class:`FormSession`, the step-derivation pure functions over a
:class:`~app.domain.form.FormDefinition`, the :class:`FormSessionRepository`
port, the command/view dataclasses exchanged with the HTTP layer, and the
internal helpers every use case shares (view projection, hidden-key
computation, step merge, session load, actor proxy).

The use-case functions themselves live in :mod:`session_use_cases`; the
P07-US7 transition guard lives in :mod:`transition_forms`. This module
imports only domain symbols — no interfaces/infrastructure (AGENTS.md §4.R10).

# INTEGRATION_SEAM

:class:`FormSessionRepository` is a domain-facing Protocol; the SQL adapter
lives in ``app.infrastructure.persistence.repositories.form_repository`` and
the Azure production adapter would sit behind the same Protocol (AGENTS.md §12).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.domain.errors import NotFound
from app.domain.form import FormDefinition, FormSectionSpec, FormVersion

# ---------------------------------------------------------------------------
# Multi-step form session model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FormStep:
    """A single step in a multi-step form.

    Steps are the unit of *resumable* progress (P07-US6). A step is
    keyed by a stable machine identifier (``step_key``) — labels may
    rename across versions without breaking history, exactly like
    field keys on :class:`app.domain.form.FormFieldSpec`. A step is
    considered *complete* when the worker has saved or submitted data
    for every section whose ``section_key`` is in ``section_keys`` and
    the data passes form-level required-field validation.
    """

    step_key: str
    label: str
    section_keys: tuple[str, ...] = ()
    required: bool = True


@dataclass(frozen=True, slots=True)
class FormSession:
    """A worker's progress through a multi-step form (P07-US6).

    The session is the durable record of "where the worker stopped".
    ``current_step_index`` is the next step the worker should see on
    resume; ``completed_steps`` is the set of step_keys the worker has
    already finished. ``step_data`` is the partial payload accumulated
    across steps — a worker's draft at step N contains everything they
    saved up to step N.

    Lifecycle: ``draft`` -> ``in_progress`` -> ``submitted`` (terminal).
    ``submitted`` sessions are immutable; reopening is a new session.
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    form_version_id: UUID
    actor_id: UUID
    current_step_index: int
    completed_steps: frozenset[str]
    step_data: dict[str, Any]
    status: str
    version_token: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    submitted_at: datetime | None = None


def _step_section_keys(definition: FormDefinition) -> tuple[str, ...]:
    """Return every section key declared on the form, in order.

    Used to fall back to a single-step form when the author did not
    declare an explicit ``steps`` array on the form definition (so the
    multi-step machinery is a strict superset of the single-step
    experience).
    """
    return tuple(section.key for section in definition.sections)


def derive_steps(definition: FormDefinition) -> tuple[FormStep, ...]:
    """Resolve the effective list of steps for a form definition.

    A definition may declare ``steps`` explicitly — an ordered list of
    ``{"step_key", "label", "section_keys", "required"}`` entries. When
    present, those drive the progress UI. When absent (or empty), the
    form degenerates to a single step that contains every declared
    section, preserving the single-step experience.

    Why derive on demand: storing steps as a dedicated column would
    require a migration + a version-shape bump; the JSON form
    definition is already versioned and immutable-on-publish, so a
    pure function over ``definition`` is enough for the application
    surface. The seam stays a single helper.
    """
    raw_steps = getattr(definition, "steps", None) or ()
    if raw_steps:
        steps: list[FormStep] = []
        for entry in raw_steps:
            if not isinstance(entry, Mapping):
                continue
            key = str(entry.get("step_key") or entry.get("key") or "").strip()
            if not key:
                continue
            section_keys_raw = entry.get("section_keys") or ()
            if isinstance(section_keys_raw, (list, tuple)):
                section_keys = tuple(str(s) for s in section_keys_raw)
            else:
                section_keys = ()
            label = str(entry.get("label") or key)
            required = bool(entry.get("required", True))
            steps.append(
                FormStep(
                    step_key=key,
                    label=label,
                    section_keys=section_keys,
                    required=required,
                )
            )
        if steps:
            return tuple(steps)
    section_keys = _step_section_keys(definition)
    if not section_keys:
        return ()
    return (
        FormStep(
            step_key="default",
            label="Form",
            section_keys=section_keys,
            required=True,
        ),
    )


def _sections_for_step(definition: FormDefinition, step: FormStep) -> tuple[FormSectionSpec, ...]:
    """Return the sections that belong to ``step``.

    An explicit step declares its ``section_keys``; an implicit step
    (the single-step fallback) carries every section. The set is
    resolved here so the session handlers never reach into the
    definition's internals.
    """
    if not step.section_keys:
        return definition.sections
    wanted = frozenset(step.section_keys)
    return tuple(s for s in definition.sections if s.key in wanted)


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


class FormSessionRepository(Protocol):
    """Persistence seam for :class:`FormSession` (P07-US6).

    The application layer never imports SQLAlchemy; this Protocol is
    the only contract the use case depends on. The local SQL adapter
    is a follow-up task in the FM-038 backlog; the tests use an
    in-memory fake that satisfies the same shape.
    """

    async def get(self, tenant_id: UUID, session_id: UUID) -> FormSession | None: ...

    async def get_open_for_case_and_form(
        self, tenant_id: UUID, case_id: UUID, form_version_id: UUID, actor_id: UUID
    ) -> FormSession | None: ...

    async def add(self, session: FormSession) -> None: ...

    async def update(self, session: FormSession) -> None: ...


class _ClockLike(Protocol):
    """Minimal clock protocol. The application layer must not import
    ``app.deps.Clock`` (that would pull FastAPI in); a duck-typed
    fixed clock satisfies this shape in tests."""

    def now(self) -> Any: ...


# ---------------------------------------------------------------------------
# Commands and read-side view
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StartFormSessionCommand:
    """Input to :func:`session_use_cases.start_session`.

    One open session per (case, form_version, actor). Calling start
    again for the same triple returns the existing session — no
    duplicate progress records (idempotent per FM-038 acceptance
    scenarios).
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    form_key: str
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class FormSessionView:
    """Read-side projection returned to the router.

    ``steps`` carries the resolved step list (so the SPA does not
    re-derive it), and ``progress`` is a tuple of ``(step_key,
    status)`` tuples the SPA can render deterministically.

    ``visible_step_data`` is the only payload the router may echo to
    the worker: fields living in currently-hidden sections are
    stripped (Constitution V — conditional answers must not leak to
    a worker who cannot see the section). The full accumulation
    stays server-side on ``session.step_data`` so hidden values are
    preserved across steps (P07-US3) without ever crossing the wire.
    """

    session: FormSession
    steps: tuple[FormStep, ...]
    progress: tuple[tuple[str, str], ...]
    current_step: FormStep | None
    visible_step_data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SaveFormStepCommand:
    """Input to :func:`session_use_cases.save_step`.

    ``step_key`` identifies which step the worker just finished.
    ``data`` is the partial payload for the sections in that step;
    it is merged into the session's accumulated ``step_data``.
    ``expected_version_token`` is the ``If-Match`` value the worker
    observed: when supplied and stale, the save is refused with
    ``Conflict`` instead of applying onto newer progress.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    session_id: UUID
    step_key: str
    data: Mapping[str, Any]
    correlation_id: str = ""
    expected_version_token: int | None = None


@dataclass(frozen=True, slots=True)
class SubmitFormSessionCommand:
    """Input to :func:`session_use_cases.submit_session`.

    The final validated commit of a multi-step session (P07-US6). The
    worker has completed every required step via :func:`save_step`;
    this call validates the accumulated payload with the same
    deterministic pipeline as the single-step submit path (E-4) and,
    only on success, writes it to the case and marks the session
    ``submitted``.

    ``expected_session_token`` is the ``If-Match`` value the worker
    observed on the session; ``expected_case_version_token`` is the
    ``If-Match`` value observed on the case. Either stale token
    raises :class:`Conflict` and nothing is written.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    session_id: UUID
    expected_session_token: int
    expected_case_version_token: int
    correlation_id: str = ""
    causation_id: str | None = None


@dataclass(frozen=True, slots=True)
class SubmitFormSessionResult:
    """Outcome of :func:`session_use_cases.submit_session`.

    ``hidden_keys`` names the conditionally-hidden fields that kept
    their stored values (audit transparency, never content).
    """

    session_view: FormSessionView
    case: Any
    hidden_keys: frozenset[str]


@dataclass(frozen=True, slots=True)
class ResumeFormSessionQuery:
    """Input to :func:`session_use_cases.resume_session`.

    Returns the existing open session for the (case, form, actor)
    triple — the SPA calls this on mount to render where the worker
    left off.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    form_key: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _index_of_step(steps: tuple[FormStep, ...], step_key: str) -> int | None:
    """Return the index of ``step_key`` in ``steps`` or ``None``."""
    for index, step in enumerate(steps):
        if step.step_key == step_key:
            return index
    return None


def _hidden_keys_for_sections(
    definition: FormDefinition, visible_section_keys: frozenset[str]
) -> frozenset[str]:
    """Mirror of :func:`submit_form._hidden_field_keys` for the session path.

    Lives in this module instead of being imported to keep the
    application surfaces decoupled — the form-submission module
    already has its helper but importing it would tangle the
    multi-step use case into the single-step atomic-commit path.
    """
    hidden: set[str] = set()
    for section in definition.sections:
        if section.key in visible_section_keys:
            continue
        for field in section.fields:
            hidden.add(field.key)
    return frozenset(hidden)


def _merge_step_data(
    *,
    previous: Mapping[str, Any],
    submitted: Mapping[str, Any],
    hidden_keys: frozenset[str],
) -> dict[str, Any]:
    """Merge submitted step data over the session's accumulated data.

    Hidden keys are preserved exactly like the canonical
    :func:`submit_form` path (P07-US3). A field that is hidden on this
    step retains the value the worker stored on a previous step.
    """
    merged: dict[str, Any] = dict(previous)
    for key, value in submitted.items():
        if key in hidden_keys:
            continue
        merged[key] = value
    return merged


def _to_view(
    session: FormSession,
    steps: tuple[FormStep, ...],
    *,
    form_version: FormVersion,
) -> FormSessionView:
    """Build the read-side projection used by routers and the SPA.

    Progress is derived in step-declaration order so two workers see
    the identical sequence (deterministic). ``visible_step_data``
    strips every field that lives in a conditionally-hidden section
    (evaluated against the session's own accumulated data): the SPA
    renders only what the worker may see, while ``session.step_data``
    keeps the full server-side accumulation for preservation.
    """
    progress: list[tuple[str, str]] = []
    for step in steps:
        if step.step_key in session.completed_steps:
            progress.append((step.step_key, "completed"))
        elif steps.index(step) == session.current_step_index and (session.status == "in_progress"):
            progress.append((step.step_key, "current"))
        else:
            progress.append((step.step_key, "pending"))
    current_step = (
        steps[session.current_step_index] if 0 <= session.current_step_index < len(steps) else None
    )
    # why: visibility is data-dependent (``visible_when`` predicates
    # read earlier answers), so the leak filter must evaluate against
    # the same accumulated payload the merge step preserves.
    visible_sections = form_version.visible_section_keys(visible_data=session.step_data)
    hidden_keys = _hidden_keys_for_sections(form_version.definition, visible_sections)
    visible_step_data = {
        key: value for key, value in session.step_data.items() if key not in hidden_keys
    }
    return FormSessionView(
        session=session,
        steps=steps,
        progress=tuple(progress),
        current_step=current_step,
        visible_step_data=visible_step_data,
    )


async def _load_session(
    session_repo: FormSessionRepository,
    tenant_id: UUID,
    session_id: UUID,
) -> FormSession:
    """Resolve a session, raising :class:`NotFound` when missing.

    Cross-tenant ids surface as ``NotFound`` so the API never
    confirms cross-tenant existence (Constitution V).
    """
    session = await session_repo.get(tenant_id, session_id)
    if session is None:
        raise NotFound(f"form session {session_id} not found")
    return session


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy.

    Same pattern as :file:`submit_form.py` — the application layer
    must not import :class:`app.deps.UserContext` because that
    module pulls FastAPI in.
    """

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


def _actor_proxy(
    cmd: StartFormSessionCommand
    | SaveFormStepCommand
    | ResumeFormSessionQuery
    | SubmitFormSessionCommand,
) -> Any:
    """Build the policy actor from any session command/query (fields are shared)."""
    return _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
