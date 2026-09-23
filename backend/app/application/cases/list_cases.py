"""List case summaries for the case-list page (P01-US2 / FR-002).

The list endpoint (``GET /api/v1/cases``) returns the same per-row shape
as the workspace summary, minus the fields that don't make sense in a
list (timeline, recent documents, etc.). Pagination is enforced server
side: ``limit`` is clamped to ``[1, MAX_PAGE_LIMIT]`` and ``offset`` to
``[0, ...)``; the OpenAPI schema advertises the same bounds.

Authorization: the policy evaluates ``Action.CASE_READ`` once per
request; deny raises ``Forbidden``. Tenant scoping is delegated to the
repository (every method takes ``tenant_id`` first and includes
``WHERE tenant_id = :tenant_id``).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.application.cases.view_case import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
)
from app.domain.case import Case
from app.domain.errors import ValidationFailed
from app.domain.ports import CaseRepository


@dataclass(frozen=True, slots=True)
class ListCasesQuery:
    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0


def _clamp_limit(limit: int) -> int:
    if limit < 1:
        raise ValidationFailed("limit must be >= 1", field="limit")
    return min(limit, MAX_PAGE_LIMIT)


def _clamp_offset(offset: int) -> int:
    if offset < 0:
        raise ValidationFailed("offset must be >= 0", field="offset")
    return offset


async def handle(query: ListCasesQuery, *, case_repo: CaseRepository) -> list[Case]:
    """Return a page of case aggregates, newest first.

    The list is intentionally aggregate-shaped: the HTTP layer decides
    which fields to expose in the list view. Returning ``Case`` keeps
    the use case testable without Pydantic.
    """
    actor_ctx = _ActorProxy(
        tenant_id=query.tenant_id,
        user_id=query.actor_id,
        roles=query.actor_roles,
    )
    await authorize(
        Action.CASE_READ,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=query.tenant_id, kind="case", id=None),
    )

    limit = _clamp_limit(query.limit)
    offset = _clamp_offset(query.offset)
    return list(await case_repo.list(query.tenant_id, limit=limit, offset=offset))


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy (see create_case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


__all__ = ["ListCasesQuery", "handle"]
