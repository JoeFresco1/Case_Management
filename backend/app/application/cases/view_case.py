"""View case summary (P01-US2 / FM-006 / FR-002).

Assembles a case workspace summary for an authorized actor. The
summary is the canonical read shape that backs ``GET /api/v1/cases/{id}``
and the case-list view:

- reference, type, status, workflow state, priority
- assignee / queue (deferred; placeholder field set)
- participants (deferred; empty until P03 lands)
- open tasks (deferred; empty until Phase 2 lands)
- recent documents (deferred; empty until Phase 2 lands)
- recent decisions (deferred; empty until P02 lands)
- timeline (deferred; ``[]`` until audit timeline lands in T033)

# INTEGRATION_SEAM: read API

The use case is the only place that knows the shape of a *case summary*
vs a *case record*. The router takes the dataclass and serializes it
through `interfaces.http.schemas.cases.CaseSummary`. The Pydantic schema
is the OpenAPI contract — never hand-edit at the router.

# Authorization redaction (FR-002)

The summary MUST NOT include content the requester is not authorized to
see. The deny-by-default policy from ``app.application.authorization``
evaluates ``Action.CASE_READ`` first; on deny we raise ``Forbidden``.
For sensitive cases (P01-US4) the summary fields the actor is not
permitted to see are **stripped** (not zeroed out) so a leaked field
cannot be detected by a side channel. A redacted field is replaced with
``Redacted()`` whose JSON shape is ``{"redacted": true, "reason":
"sensitive"}`` — the only string the client sees is the redaction
marker.

The redactor never throws; it is a pure transformation over the
already-authorized summary. Deny happens *before* the summary is
assembled so we never build a payload only to throw half of it away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.case import Case
from app.domain.errors import Forbidden, NotFound
from app.domain.ports import CaseRepository

# ---------------------------------------------------------------------------
# Limits (AGENTS.md §17.1): pagination defaults; keep in sync with the
# HTTP layer so server-side clamps mirror the OpenAPI schema.
# ---------------------------------------------------------------------------

DEFAULT_PAGE_LIMIT: Final[int] = 50
MAX_PAGE_LIMIT: Final[int] = 200


# ---------------------------------------------------------------------------
# Redaction marker
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Redacted:
    """A field whose content the actor is not authorized to see.

    The serializer renders this dataclass as ``{"redacted": true,
    "reason": "..."}`` so the client can detect and localize the
    redaction without learning the underlying content. Two reasons are
    defined today:

    - ``"sensitive"`` — the case is sensitive and the actor's role does
      not grant the sensitive-read action (P01-US4).
    - ``"not_implemented"`` — the field is reserved for a later pillar
      (participants, documents, decisions, timeline) and intentionally
      omitted from the walking-skeleton summary.
    """

    reason: str = "sensitive"

    def to_dict(self) -> dict[str, Any]:
        return {"redacted": True, "reason": self.reason}


def _redacted(reason: str = "sensitive") -> Redacted:
    return Redacted(reason=reason)


# ---------------------------------------------------------------------------
# Summary dataclass — the canonical read shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseSummary:
    """A redacted, assembled case workspace summary (P01-US2 / FR-002).

    The dataclass is the contract between the application and the HTTP
    layer. The Pydantic schema (``interfaces/http/schemas/cases``)
    mirrors these fields. New fields land here first, then in the
    schema; the router never hand-types fields.
    """

    id: UUID
    tenant_id: UUID
    reference: str
    case_type_key: str
    case_type_version_id: UUID
    status: str
    priority: str
    title: str
    description: str
    data: dict[str, Any]
    current_workflow_state: str | None
    version_token: int
    created_at: str
    updated_at: str
    created_by: UUID
    closed_at: str | None
    sensitivity: str = "internal"

    # Deferred slots — populated by later pillars. Redacted today so
    # the workspace UI never breaks when a later slice lands.
    assignee: Redacted | None = None
    queue: Redacted | None = None
    # SLA state is now populated by P02-US6 / FM-018: the router
    # reads the case's live SLA clocks and projects the worst tier
    # (``breached`` > ``warning`` > ``active`` > ``paused`` >
    # ``completed`` > ``none``) plus the next deadline. Today the
    # field is typed as ``Any`` so the wire shape can evolve without
    # churning this dataclass; the Pydantic schema
    # (``SlaStateView``) is the authoritative wire contract.
    sla_state: Any | None = None
    participants: list[Any] = field(default_factory=list)
    open_tasks: list[Any] = field(default_factory=list)
    recent_documents: list[Any] = field(default_factory=list)
    recent_decisions: list[Any] = field(default_factory=list)
    timeline: list[Any] = field(default_factory=list)
    # P08-US1 / FM-039: surfaced from the Case aggregate's
    # legal_hold flag so operators see "hold" on the workspace without
    # a separate lookup. Read-only: placement/release goes through
    # POST/DELETE /cases/{id}/holds.
    legal_hold: bool = False


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ViewCaseQuery:
    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID


async def handle(query: ViewCaseQuery, *, case_repo: CaseRepository) -> Case:
    """Fetch the case aggregate for the workspace view.

    Returns the **domain Case** rather than a pre-built summary. The
    router is responsible for assembling the summary shape so the
    decision of which fields to expose lives at the API boundary, not
    in the use case. This keeps the use case testable without
    referencing FastAPI / Pydantic.

    Failure behavior:
    - Missing case (or cross-tenant id) -> ``NotFound``.
    - Actor missing any role -> ``Forbidden`` (deny by default).
    """
    # 1. Tenant + role gate. Authorization evaluates ``CASE_READ``
    # against the actor's roles; deny raises ``Forbidden`` which the
    # HTTP layer maps to 403. Cross-tenant ids never reach this point
    # because the repository filters by tenant_id, but the policy is a
    # belt-and-suspenders check on the user's own tenant_id.
    actor_ctx = _ActorProxy(
        tenant_id=query.tenant_id,
        user_id=query.actor_id,
        roles=query.actor_roles,
    )
    await authorize(
        Action.CASE_READ,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=query.tenant_id, kind="case", id=query.case_id),
    )

    # 2. Resolve the case, tenant-scoped.
    case = await case_repo.get(query.tenant_id, query.case_id)
    if case is None:
        raise NotFound(f"case {query.case_id} not found")
    return case


async def actor_can_see_sensitive(
    *,
    tenant_id: UUID,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
    case_id: UUID,
) -> bool:
    """Return ``True`` iff the actor's role grants ``CASE_READ_SENSITIVE``.

    Evaluates the action against the actor's role set using the same
    policy registry as the read gate. The router passes the result
    into :func:`build_summary` so the redactor stays pure. The
    authorization helper swallows deny by returning ``False`` rather
    than raising — the read is still allowed; only sensitive fields
    are masked. A failed sensitive-read is not a 403.
    """
    actor_ctx = _ActorProxy(
        tenant_id=tenant_id,
        user_id=actor_id,
        roles=actor_roles,
    )
    try:
        await authorize(
            Action.CASE_READ_SENSITIVE,
            actor_ctx,  # type: ignore[arg-type]
            Resource(tenant_id=tenant_id, kind="case", id=case_id),
        )
    except Forbidden:
        return False
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy (see create_case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "CaseSummary",
    "Redacted",
    "ViewCaseQuery",
    "_redacted",
    "actor_can_see_sensitive",
    "handle",
]
