"""Synthetic data seed CLI (AGENTS.md §7 + §18).

# Why a package (R2 structural split)

Formerly a single ``seed.py`` module of ~690 lines — past the R2
600-line soft limit. Split by responsibility into :mod:`dataset`
(tenant constants, synthetic users, case-type schema), :mod:`blueprint`
(workflow blueprint), :mod:`runner` (the async seed orchestration), and
:mod:`cli` (the Typer app + ``seed`` command). This ``__init__`` is a
pure re-export shim: ``__all__`` and every previously visible attribute
are re-exported unchanged, so no consumer import line needs editing
(``app.cli.__main__`` still does ``from app.cli.seed import seed``).

# What the seed does

Creates the local development / demo dataset for the Case Management
Platform. The dataset is **synthetic** — every email is ``example.test``,
display names are obviously fake, and no PII / PHI is emitted. The CLI
must never be used to load enterprise data; ``SYNTHETIC ENVIRONMENT`` is
logged at startup so operators see the marker before this runs.

Invoked via ``uv run python -m app.cli seed``. Idempotent: re-running
``seed`` produces the same outcome without duplicating rows.

What lands in the DB (Phase 0-1 scope):

- ``tenants`` row ``ACME`` (synthetic short key).
- Three ``users`` under ``ACME``: ``intake``, ``reviewer``, ``supervisor``.
  Each has a stable UUID and an ``X-Dev-User`` key wired through
  ``app.deps.register_local_user`` so the local dev-header IdP can
  resolve them.
- One ``CaseType`` ``ServiceRequest`` with a single published
  ``CaseTypeVersion`` carrying the synthetic schema from AGENTS.md
  §16.2 (no PHI fields).
- A validated ``WorkflowVersion`` blueprint for the ``intake →
  in_review → decided → closed`` vertical (P03-US5, FM-024) whose
  ``submit_for_review`` transition is guarded by ``case.documents``
  being present (P03-US5 — "required-document rule"). The blueprint is
  validated at seed time so a malformed guard fails the seed rather
  than passing through to a workflow that traps a case at runtime.

The CLI delegates to the application layer's publish use case rather
than bypassing it: this exercises the same code path a tenant
administrator would hit, so seed-time regressions surface as seed
failures, not as silent corruption.

# Demo case + workflow vertical

The seed persists the full first-slice vertical in one transaction scope:

- published ``ServiceRequest`` case type (via the publish use case),
- published ``ServiceRequestWorkflow`` (``intake → in_review → decided →
  closed`` with ``submit_for_review`` / ``record_decision`` / ``close``
  transitions, validated at seed time),
- ``review_queue`` / ``intake_queue`` rows the workflow references,
- one demo case (``current_workflow_state=intake``) started through
  ``start_workflow.handle``,
- one initial ``review`` task plus a 48h ``review_due`` SLA clock.

``SqlCaseRepository.next_reference`` resolves the tenant short key via
the real ``TenantRepository``; ``SqlWorkflowDefinitionRepository`` /
``SqlWorkflowVersionRepository`` back the workflow lookups.
"""

from __future__ import annotations

from .blueprint import (  # noqa: F401
    UUID,
    Any,
    WorkflowState,
    WorkflowTransition,
    _service_request_workflow_blueprint,
)
from .cli import (  # noqa: F401
    app,
    asyncio,
    cli_main,
    get_settings,
    seed,
    typer,
)
from .dataset import (  # noqa: F401
    TENANT_KEY,
    TENANT_NAME,
    TENANT_SHORT,
    CaseTypeField,
    CaseTypeSchema,
    SyntheticUser,
    _service_request_schema,
    _synthetic_users,
    _uuid,
    dataclass,
)
from .runner import (  # noqa: F401
    UTC,
    Case,
    CaseType,
    CaseTypeVersion,
    CaseTypeVersionStatus,
    SqlAuditEventStore,
    SqlCaseRepository,
    SqlCaseTypeRepository,
    SqlCaseTypeVersionRepository,
    SqlOutboxWriter,
    SqlQueueRepository,
    SqlSlaClockRepository,
    SqlTaskRepository,
    SqlWorkflowDefinitionRepository,
    SqlWorkflowInstanceRepository,
    SqlWorkflowVersionRepository,
    UserContext,
    WorkflowDefinition,
    WorkflowVersion,
    _seed,
    datetime,
    models,
    publish_case_type_uc,
    publish_workflow_uc,
    register_local_user,
    select,
    session_scope,
    start_workflow_uc,
    uuid4,
    validate_definition,
)

# why the annotation: the pre-split module had annotated module-level
# constants (``TENANT_KEY: str``), so its dict carried ``__annotations__``.
# Annotating ``__all__`` emits the same SETUP_ANNOTATIONS opcode here and
# keeps ``dir(app.cli.seed)`` byte-identical after the split.
__all__: list[str] = ["seed", "app", "_seed", "TENANT_KEY", "TENANT_SHORT"]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, but the pre-split single-file module exposed no such
# attributes (and file modules carry no ``__path__``). Popping them keeps
# ``dir(app.cli.seed)`` byte-identical to the old module's attribute surface —
# the R2 refactor must be invisible. The submodules stay importable
# (``...seed.runner`` resolves through ``sys.modules``, which is untouched);
# only the redundant package-level attributes go away.
for _submodule in (
    "blueprint",
    "cli",
    "dataset",
    "runner",
):
    globals().pop(_submodule, None)
del _submodule
globals().pop("__path__", None)


# why these hooks (PEP 562): runpy resolves ``python -m app.cli.seed`` by
# importing this package and *then* calling ``find_spec('app.cli.seed.__main__')``,
# which reads ``__path__`` on the already-initialized package above — a bare
# ``__path__`` pop (the usual R2 pattern) breaks direct execution with
# "``__path__`` attribute not found". Serving ``__path__`` from a module-level
# ``__getattr__`` keeps it out of ``__dict__`` while remaining resolvable, and
# ``__dir__`` hides the hooks themselves so ``dir(app.cli.seed)`` stays
# byte-identical to the pre-split file module.
def __getattr__(name: str) -> Any:
    """Resolve attributes absent from this package's dict (PEP 562).

    Only ``__path__`` is served — the package path survives the pop above via
    ``__spec__.submodule_search_locations`` (the same list object the import
    system installs as ``__path__``). Any other missing name raises the
    standard ``AttributeError``, matching the pre-split module's behavior.

    Args:
        name: The attribute name the import system or a caller looked up.

    Returns:
        The resolved attribute value.

    Failure mode:
        ``AttributeError`` for every name except ``__path__``.
    """
    if name == "__path__":
        return __spec__.submodule_search_locations
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Report the pre-split module's attribute surface from ``dir()``.

    The two PEP 562 hooks live in this package's ``__dict__`` but were never
    visible on the single-file module, so they are filtered out of the listing;
    every other dict key is reported as-is.

    Returns:
        The sorted attribute names ``dir(app.cli.seed)`` must yield.
    """
    return sorted(key for key in globals() if key not in ("__getattr__", "__dir__"))
