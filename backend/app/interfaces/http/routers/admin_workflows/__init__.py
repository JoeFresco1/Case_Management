"""Admin API for workflow configuration lifecycle (FM-013 / FR-015, P02-US2).

# INTEGRATION_SEAM: HTTP entry

Formerly a single ``admin_workflows.py`` module (722 lines) — past the
R2 600-line soft limit. This package is the only HTTP entry to the
admin workflow configuration lifecycle surface. Split by route-group
responsibility into :mod:`common` (shared ``router`` + read-projection
helpers), :mod:`definitions` (``POST/GET /api/v1/admin/workflows``),
:mod:`versions` (view / validate / diff a persisted version),
:mod:`draft` (draft a version and validate a candidate definition by
key), :mod:`publish` (both publish endpoints), and :mod:`simulate`
(dry-run a version against a synthetic case). This ``__init__`` is a
pure re-export shim: ``__all__`` and every previously visible
attribute — public and private, including names the old module merely
imported, such as ``Any``/``UUID``/``status``/``Action`` — are
re-exported unchanged, so no consumer import line needs editing
(``from ...admin_workflows import router`` in ``app/main.py:151`` and
``tests/interfaces/test_admin_workflows_*.py:25/27`` keeps working, as
does ``mock.patch("app.interfaces.http.routers.admin_workflows.X")``).
The route set, route order-relevant invariants (the canonical
``/{id}/versions/{n}/publish`` handler stays registered before the
key+version-id twin with the identical Starlette pattern — see
:mod:`publish`), and OpenAPI operation ids are unchanged.

Endpoints:

- ``POST /api/v1/admin/workflows/{key}/versions`` — draft a new
  ``WorkflowVersion`` of an existing ``WorkflowDefinition``. The
  body carries the ``definition`` (states + transitions). The use
  case runs the validator at draft time so a broken definition is
  never persisted.
- ``POST /api/v1/admin/workflows/{key}/versions:validate`` — run the
  validator against the supplied ``definition`` without persisting
  anything. Returns the parsed ``state_keys`` / ``transition_keys``
  so the admin UI can render what would be published.
- ``POST /api/v1/admin/workflows/{id}/versions/{n}/simulate`` — dry-run
  a draft or published ``WorkflowVersion`` against a synthetic case
  (FM-015 / P02-US5). The endpoint never mutates the version, never
  persists the synthetic case, and never writes audit events. It is
  the companion to the ``execute_transition`` runtime engine and
  answers "what would happen if this case took these transitions?"
- ``POST /api/v1/admin/workflows/{key}/versions/{version_id}/publish``
  — validate + publish the draft. Re-publishing a published version
  raises ``InvariantViolation`` because the row's status is no longer
  ``draft`` (FR-015 / AC-007).
- ``POST /api/v1/admin/workflows`` / ``GET /api/v1/admin/workflows`` —
  create and list workflow definition headers.
- ``GET /api/v1/admin/workflows/{id}/versions/{n}``,
  ``POST .../validate``, ``GET .../diff`` — read-only inspection of a
  persisted version.

# Authorization

All endpoints require ``Action.WORKFLOW_PUBLISH``; the
``process_admin`` static role grants it
(``app/application/authorization.py``). Fail-closed: missing role ->
``403 Forbidden``.

# Transaction shape

Draft and publish both run inside the same DB transaction as their
``ConfigurationDrafted`` / ``ConfigurationPublished`` audit event so the
status flip, the current-version pointer, and the audit row commit
atomically (Constitution IV / plan §4.R12). The HTTP boundary commits
once after the use case returns. Simulation, view, validate, and diff
are read-only and commit nothing.

# INTEGRATION_SEAM: SQL adapters

The SQL adapters (``SqlWorkflowDefinitionRepository``,
``SqlWorkflowVersionRepository``, ``SqlQueueRepository``) are **stub
seams** for T066-T077 — every method raises ``NotImplementedError``
until the real adapter ships. The router is mounted and the OpenAPI
surface is live; the next dispatch closes the SQL adapter. Tests
exercise the use case through in-memory fakes; they do not depend on
the stub's behaviour.
"""

from __future__ import annotations

from .common import (  # noqa: F401
    APIRouter,
    HTTPException,
    WorkflowReadRepository,
    WorkflowStateResponse,
    WorkflowTransitionResponse,
    WorkflowVersion,
    WorkflowVersionResponse,
    _read_repository,
    _version_response,
    router,
    status,
)
from .definitions import (  # noqa: F401
    UUID,
    Action,
    AdminWorkflowsHttpDependencyProvider,
    AsyncSession,
    Clock,
    CorrelationContext,
    CreateWorkflowRequest,
    Depends,
    Resource,
    UserContext,
    WorkflowDefinitionResponse,
    WorkflowSummaryResponse,
    authorize,
    create_workflow,
    create_workflow_definition,
    get_admin_workflows_dependencies,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
    list_workflows,
)
from .draft import (  # noqa: F401
    DraftWorkflowResultResponse,
    DraftWorkflowVersionRequest,
    NotFound,
    Path,
    WorkflowState,
    WorkflowTransition,
    WorkflowValidationResponse,
    _to_domain_states,
    _to_domain_transitions,
    draft_workflow,
    draft_workflow_version,
    validate_workflow_definition,
    validate_workflow_definition_endpoint,
)
from .publish import (  # noqa: F401
    PublishWorkflowResultResponse,
    publish_canonical_workflow_version,
    publish_workflow,
    publish_workflow_version,
)
from .simulate import (  # noqa: F401
    Any,
    SimulatedGuardResultDomain,
    SimulatedGuardResultResponse,
    SimulatedStepDomain,
    SimulatedStepResponse,
    SimulateWorkflowVersionRequest,
    SimulateWorkflowVersionResponse,
    SimulationBlockerDomain,
    SimulationBlockerResponse,
    SimulationResult,
    _blocker_to_response,
    _guard_to_response,
    _step_to_response,
    simulate_workflow_use_case,
    simulate_workflow_version,
)
from .versions import (  # noqa: F401
    WorkflowDiffResponse,
    diff_workflow_version,
    get_workflow_version,
    validate_persisted_workflow_version,
)

__all__ = ["router"]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, and packages additionally carry ``__path__`` — neither
# existed on the pre-split single-file module. Popping them keeps
# ``dir(app.interfaces.http.routers.admin_workflows)`` byte-identical to the
# old module's attribute surface — the R2 refactor must be invisible. The
# submodules stay importable (``...admin_workflows.publish`` resolves through
# ``sys.modules``, which is untouched); only the redundant package-level
# attributes go away.
for _submodule in (
    "common",
    "definitions",
    "draft",
    "publish",
    "simulate",
    "versions",
    "__path__",
):
    globals().pop(_submodule, None)
del _submodule
