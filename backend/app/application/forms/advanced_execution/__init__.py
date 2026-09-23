"""Advanced form execution use cases (P07-US6 / P07-US7, FM-038).

# Why a package (R2 structural split)

Formerly a single ``advanced_execution.py`` module (~1060 lines) — past
the R2 soft limit of 600. Split by responsibility into
:mod:`session_model` (session aggregate, step derivation, repository
port, commands/view, shared helpers), :mod:`session_use_cases`
(start/save/submit/resume orchestration), and :mod:`transition_forms`
(P07-US7 pure transition guard). This ``__init__`` is a pure re-export
shim: ``__all__`` and every previously visible attribute (public *and*
private) are re-exported unchanged, so no consumer import line and no
test ``patch`` target needs editing.

# Two related capabilities

1. **Resumable multi-step forms** (P07-US6): a single form is partitioned
   into an ordered list of *steps* by a ``steps`` array on
   :class:`app.domain.form.FormDefinition`. A worker starts a
   :class:`FormSession`, saves progress on every step, and submits only
   on the final step. The session is the durable record of progress — if
   the worker closes the browser mid-form they resume from the last
   completed step on next load. Sessions are tenant-scoped and bound to a
   case; the data layer treats the session as a port so the SQL
   adapter lives behind :class:`FormSessionRepository`.

2. **State-specific workflow forms with transition validation** (P07-US7):
   a form is bound to a single workflow *state* via ``state_forms`` on
   :class:`app.domain.form.FormBinding`. When a transition is requested,
   the runtime must verify that every required form for the *target*
   state has been submitted (or skip-flagged).
   :func:`validate_transition_forms` is a pure-function guard that the
   workflow engine calls before a transition can fire. The guard is
   pluggable: it composes with the existing
   :func:`app.application.workflow.execute_transition.handle` path without
   coupling to its internals (the runtime asks the guard "would this
   transition be allowed under the form rules?"; the engine owns the
   commit).

Both surfaces are deliberately seam-clean so the SQL adapters land in
follow-up tasks (FM-038 H3 is deferred in :file:`AGENTS.md`; the
application surface ships now with in-memory fakes and the tests pin
the contracts).

# INTEGRATION_SEAM

Repositories are domain ports (:mod:`app.domain.ports`). The local
adapter lives in
:file:`app/infrastructure/persistence/repositories/form_session_repository.py`
(future); the Azure production adapter is a future module behind the
same Protocol (AGENTS.md §12).
"""

from __future__ import annotations

# why origin imports here: the pre-split module imported these names at module
# scope, and dir() must stay byte-identical. Importing them from their origins
# (not via the submodules) also keeps mypy --strict happy — a submodule would
# need explicit ``X as X`` re-exports for every borrowed name.
from collections.abc import Iterable, Mapping  # noqa: F401
from dataclasses import dataclass  # noqa: F401
from datetime import datetime  # noqa: F401
from typing import Any, Protocol  # noqa: F401
from uuid import UUID, uuid4  # noqa: F401

from app.application.authorization import Action, Resource, authorize  # noqa: F401
from app.application.forms.intake_submission import (  # noqa: F401
    apply_calculated_fields,
    validate_visible_fields_deterministic,
)
from app.domain.audit import CASE_UPDATED  # noqa: F401
from app.domain.errors import Conflict, Forbidden, NotFound, ValidationFailed  # noqa: F401
from app.domain.form import (  # noqa: F401
    FormBinding,
    FormDefinition,
    FormSectionSpec,
    FormVersion,
    FormVersionStatus,
    apply_submission_preservation,
    read_only_field_keys,
)
from app.domain.workflow import WorkflowTransition  # noqa: F401

from .session_model import (  # noqa: F401
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
    _ActorProxy,
    _ClockLike,
    _hidden_keys_for_sections,
    _index_of_step,
    _load_session,
    _merge_step_data,
    _sections_for_step,
    _step_section_keys,
    _to_view,
    derive_steps,
)
from .session_use_cases import (  # noqa: F401
    _new_correlation_id,
    _require_all_required_steps_complete,
    resume_session,
    save_step,
    start_session,
    submit_session,
)
from .transition_forms import (  # noqa: F401
    StateFormRequirement,
    TransitionFormCheckResult,
    derive_state_requirements,
    validate_transition_forms,
)

__all__ = [
    "FormSession",
    "FormSessionRepository",
    "FormSessionView",
    "FormStep",
    "ResumeFormSessionQuery",
    "SaveFormStepCommand",
    "StartFormSessionCommand",
    "StateFormRequirement",
    "SubmitFormSessionCommand",
    "SubmitFormSessionResult",
    "TransitionFormCheckResult",
    "derive_state_requirements",
    "derive_steps",
    "resume_session",
    "save_step",
    "start_session",
    "submit_session",
    "validate_transition_forms",
]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, but the pre-split single-file module exposed no such
# attributes. Popping them keeps ``dir(app.application.forms.advanced_execution)``
# byte-identical to the old module's attribute surface — the R2 refactor must
# be invisible. ``__path__`` is dropped for the same reason (a file module has
# no ``__path__``); every submodule is already in ``sys.modules`` after the
# imports above, so later ``import ...advanced_execution.<sub>`` hits the
# module cache without consulting the package path. The submodules stay
# importable (``...advanced_execution.session_model`` resolves through
# ``sys.modules``, which is untouched); only the redundant package-level
# attributes go away.
for _submodule in (
    "session_model",
    "session_use_cases",
    "transition_forms",
):
    globals().pop(_submodule, None)
del _submodule
globals().pop("__path__", None)
