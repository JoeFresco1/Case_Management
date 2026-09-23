"""Shared cases router and summary/SLA helper functions.

# Why this module (R2 structural split)

The former single-file ``cases.py`` module (682 lines) passed the R2
600-line soft limit. This module holds the pieces every route group in
the package shares:

- :data:`router` — the one ``APIRouter(prefix="/cases", tags=["cases"])``
  that :mod:`create`, :mod:`read`, :mod:`edit`, and :mod:`transitions`
  register onto. The package ``__init__`` imports those modules in the
  pre-split route order (create → view/list → edit → transitions), so
  route precedence, first-match semantics, and OpenAPI operation
  registration order are unchanged.
- :func:`_resolve_case_type_key` / :func:`_resolve_case_type_schema` —
  case-type lookups that drive P01-US4 / P11-US6 redaction (coarse
  sensitivity flag plus field-level schema).
- :func:`_load_sla_state` — projects the case's SLA clocks into the
  single workspace badge state (P02-US6 / FM-018).
- :func:`_summary_response` / :func:`_list_item_response` — translate
  the application-layer ``CaseSummary`` into the wire models.

# INTEGRATION_SEAM: HTTP entry

Unchanged from the pre-split module: handlers delegate to
``app.application.cases`` / ``app.application.workflow``; this package
only translates between Pydantic and the application layer (see the
package docstring of :mod:`app.interfaces.http.routers.cases`).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cases.http_dependencies import CaseHttpDependencyProvider
from app.application.cases.sla_state import summarize_sla_state
from app.application.cases.summary import (
    summary_to_dict,
    summary_to_list_dict,
)
from app.deps import Clock
from app.domain.case_type import CaseTypeSchema
from app.interfaces.http.schemas.cases import (
    CaseListItem,
    CaseSummary,
)

router = APIRouter(prefix="/cases", tags=["cases"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_case_type_key(
    *,
    tenant_id: UUID,
    case_type_version_id: UUID,
    dependencies: CaseHttpDependencyProvider,
    db: AsyncSession,
) -> tuple[str, bool]:
    """Resolve the human-readable ``case_type_key`` for a case row.

    Returns ``(case_type_key, is_sensitive)``. ``is_sensitive`` is True
    iff the case type's ``sensitivity_default`` is in ``{"restricted",
    "phi"}``. The router uses this to drive P01-US4 redaction: a
    sensitive case's structured ``data`` is stripped from the response
    even when the actor has ``case.read`` permission.

    Why surface sensitivity at the router layer: the policy registry
    does not yet carry a per-resource sensitive-read action (P01-US4
    lands with FM-007 / FM-029). Today the redactor is keyed off the
    case type's declared sensitivity; when FM-007 adds the
    ``case.read_sensitive`` action, the same flag drives the policy
    lookup and the router's redaction stays correct.
    """
    repositories = dependencies.for_session(db)
    version = await repositories.case_type_version.get(tenant_id, case_type_version_id)
    if version is None:
        return "", False
    case_type = await repositories.case_type.get(tenant_id, version.case_type_id)
    if case_type is None:
        return "", False
    return case_type.key, case_type.sensitivity_default in {"restricted", "phi"}


async def _resolve_case_type_schema(
    *,
    tenant_id: UUID,
    case_type_version_id: UUID,
    dependencies: CaseHttpDependencyProvider,
    db: AsyncSession,
) -> CaseTypeSchema | None:
    """Resolve the published schema for field-level sensitivity checks.

    P11-US6 / P05-US3: when the case's case type version declares any
    field with sensitivity at or above ``restricted``, the router hands
    the schema to :func:`build_summary` so the redactor can replace
    the values with the redaction marker for actors without the
    sensitive-read grant. Returns ``None`` when the version is missing
    or not published — the caller falls back to the coarse
    ``is_sensitive`` flag.
    """
    repositories = dependencies.for_session(db)
    version = await repositories.case_type_version.get(tenant_id, case_type_version_id)
    if version is None:
        return None
    return version.schema


async def _load_sla_state(
    *,
    tenant_id: UUID,
    case_id: UUID,
    clock: Clock,
    dependencies: CaseHttpDependencyProvider,
    db: AsyncSession,
):
    """Load the case's SLA clocks and project the workspace summary.

    P02-US6 / FM-018: the workspace view needs a single state
    field (``breached`` / ``warning`` / ``active`` / ``paused`` /
    ``completed`` / ``none``) plus the next deadline so the UI can
    render ``<SlaBadge />``. The projection is a pure function;
    the router just supplies the clock for the timestamp and the
    repository for the read. Returns ``None`` if no clocks are
    attached so the Pydantic schema can stay optional.
    """
    repositories = dependencies.for_session(db)
    clocks = await repositories.sla_clock.list_for_case(tenant_id, case_id)
    if not clocks:
        return None
    return summarize_sla_state(clocks, now=clock.now())


def _summary_response(summary) -> CaseSummary:
    """Translate the application-layer ``CaseSummary`` into the wire model."""
    wire = summary_to_dict(summary)
    return CaseSummary.model_validate(wire)


def _list_item_response(summary) -> CaseListItem:
    """Translate the application-layer ``CaseSummary`` into the list row."""
    wire = summary_to_list_dict(summary)
    return CaseListItem.model_validate(wire)
