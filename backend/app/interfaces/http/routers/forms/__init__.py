"""Forms router package: submission, published reads, sessions, transition checks.

# INTEGRATION_SEAM: HTTP entry

POST ``/api/v1/cases/{id}/forms`` is the canonical entry point for
P07-US1 / FM-036. It requires an ``Idempotency-Key`` header
(AGENTS.md §10) and delegates to
``app.application.forms.submit_form.handle``, which performs
authorization, binding resolution, visibility evaluation,
hidden-field preservation, validation, and the atomic
case+audit+outbox write.

GET ``/api/v1/forms`` lists published form versions for the current
tenant. GET ``/api/v1/forms/{form_key}/bindings`` lists published
bindings for a form.

The middleware layer (``IdempotencyMiddleware``) handles replay; the
handler does not need to know about keys.

# Endpoints

- ``POST /api/v1/forms/cases/{case_id}`` — submit a published form
  (P07-US1 / FM-036; ``Idempotency-Key`` + ``If-Match`` required).
- ``GET /api/v1/forms`` — list published form versions.
- ``GET /api/v1/forms/{form_key}`` — latest published version by key.
- ``GET /api/v1/forms/{form_key}/bindings`` — published bindings for
  a form.
- ``POST /api/v1/forms/cases/{case_id}/sessions`` — start or resume a
  multi-step session (P07-US6 / FM-038).
- ``PATCH /api/v1/forms/cases/{case_id}/sessions/{session_id}/steps/{step_key}``
  — save step progress.
- ``GET /api/v1/forms/cases/{case_id}/sessions`` — resume the open
  session for a form key.
- ``POST /api/v1/forms/bindings/{form_key}/transitions/check`` —
  state-bound form guard (P07-US7 / FM-038).

# Why a package (R2 structural split)

Formerly a single ``forms.py`` module of 720 lines — past the R2
600-line soft limit. Split by route-group responsibility into
:mod:`common` (shared ``router`` + published-form response
converters), :mod:`submission` (single-step submit), :mod:`versions`
(published version/binding reads), :mod:`sessions` (multi-step
start/save/resume + the ``FormSessionRepository`` 501 seam guard), and
:mod:`transitions` (the workflow transition form check). This
``__init__`` is a pure re-export shim: every previously visible
attribute — public and private, including names the old module merely
imported, such as ``Annotated``/``status``/``adv``/``submit_form_uc``
— is re-exported unchanged, each from exactly one source submodule
(names shared by several submodules are bound once, from the first
module that owns them), so no consumer import line needs editing
(``from ...forms import router``,
``forms_router_module.router``, and
``mock.patch("app.interfaces.http.routers.forms.X")`` keep working).
The old module defined no ``__all__``; none is introduced here, so the
attribute surface stays byte-identical once the submodule attributes
and ``__path__`` are popped below. Route registration order across the
sibling route modules is irrelevant: no two form paths overlap at the
same segment depth and method, so Starlette's first-match rule
resolves identically regardless of import order. The route set, route
paths, and OpenAPI operation ids are unchanged.
"""

from __future__ import annotations

from .common import (  # noqa: F401
    APIRouter,
    FormBinding,
    FormBindingResponse,
    FormVersion,
    FormVersionResponse,
    _form_binding_to_response,
    _form_version_to_response,
    router,
)
from .sessions import (  # noqa: F401
    UUID,
    AsyncSession,
    Clock,
    Conflict,
    Depends,
    FormSessionRepository,
    FormSessionResponse,
    FormsHttpDependencyProvider,
    FormStepSpec,
    Header,
    HTTPException,
    NotFound,
    SaveFormStepRequest,
    StartFormSessionRequest,
    UserContext,
    _form_session_repo_or_501,
    _session_view_to_response,
    adv,
    get_clock,
    get_current_user,
    get_db,
    get_forms_dependencies,
    resume_form_session,
    save_form_step,
    start_form_session,
    status,
)
from .submission import (  # noqa: F401
    SubmitFormRequest,
    SubmitFormResponse,
    submit_form,
    submit_form_uc,
)
from .transitions import (  # noqa: F401
    Action,
    Resource,
    TransitionFormCheckRequest,
    TransitionFormCheckResponse,
    WorkflowTransition,
    authorize,
    check_transition_forms,
)
from .versions import (  # noqa: F401
    Annotated,
    FormBindingListItem,
    FormListItem,
    Page,
    Query,
    get_form_version,
    list_form_bindings,
    list_form_versions,
)

# why this loop: importing the submodules above bound each one as an attribute
# of this package, and packages additionally carry ``__path__`` — neither
# existed on the pre-split single-file module. Popping them keeps
# ``dir(app.interfaces.http.routers.forms)`` byte-identical to the old
# module's attribute surface — the R2 refactor must be invisible. The
# submodules stay importable (``...forms.common`` resolves through
# ``sys.modules``, which is untouched); only the redundant package-level
# attributes go away.
for _submodule in (
    "common",
    "sessions",
    "submission",
    "transitions",
    "versions",
    "__path__",
):
    globals().pop(_submodule, None)
del _submodule
