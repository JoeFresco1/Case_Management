"""Authorized structured case search (P06-US1 / FM-031).

The use case is the canonical entry point for case search:

1. **Authorize** the actor against ``Action.CASE_READ`` (P11 / FM-002).
   Cross-tenant actors are denied unconditionally.
2. **Validate the query shape**. Filters must reference fields that are
   declared ``searchable`` in the published CaseTypeVersion; non-
   searchable fields cannot appear in a filter (P05-US4, P06-US1).
   Operators are restricted to a controlled vocabulary (``eq`` /
   ``like`` / ``in``).
3. **Compute** the ``can_see_sensitive`` grant for the actor so the SQL
   adapter can project redacted snippets without leaking field values
   through a side channel (P11-US6 / FM-029).
4. **Delegate** to ``SearchProvider.search`` which returns paginated
   ``SearchHit`` rows. The use case does not touch SQL.

The query shape is declarative (P06 pillar statement) and the
adapter is portable ``LIKE`` (AGENTS.md §4.R1). The application layer
is pure: no FastAPI / SQLAlchemy imports.

The ``searchable_field_keys`` parameter is the actor's pre-resolved set
of declared-searchable keys for the tenant. The router computes it from
the published ``CaseTypeVersion`` schemas (which is the only place the
contract lives — P05 / P06) and hands the resulting tuple down here.

# INTEGRATION_SEAM: search engine

``SearchProvider`` is a domain port (``app/domain/ports.py``). The local
adapter is ``app.infrastructure.persistence.search.SqlSearchProvider``
(portable LIKE). An Azure Cognitive Search / OpenSearch adapter replaces
the same Protocol without touching this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.application.cases.view_case import actor_can_see_sensitive
from app.domain.errors import ValidationFailed
from app.domain.ports import (
    SearchFilter,
    SearchProvider,
    SearchResultPage,
    SearchSpec,
)

# Controlled operator vocabulary. Any expansion of this set requires a
# corresponding entry in the SQL adapter's operator dispatch and an
# update to the OpenAPI schema.
SUPPORTED_OPS: frozenset[str] = frozenset({"eq", "like", "in"})

# Core case columns that may be filtered on without going through the
# case-type schema. Each entry maps to a SQL column on the ``cases``
# table; ``priority`` and ``status`` are matched case-insensitively
# because the schema stores them as lower-case strings (Portable LIKE
# + collation on SQLite/PostgreSQL).
CORE_FIELDS: frozenset[str] = frozenset(
    {"reference", "status", "priority", "title", "current_workflow_state"}
)


@dataclass(frozen=True, slots=True)
class CaseSearchQuery:
    """The application-layer shape for a case search request.

    The HTTP router translates its request model into this dataclass;
    the use case does not see FastAPI / Pydantic types. ``filters`` is
    a tuple of ``(field, op, value)`` triples; ``text_query`` is the
    free-text portion matched across ``title`` and the declared
    searchable ``data`` fields.

    ``searchable_field_keys`` is the union of ``searchable=true`` keys
    across the tenant's published CaseTypeVersions. The router
    resolves it from the schema store; the use case consumes it as a
    sealed input.

    ``sensitive_field_keys`` is the union of field keys declared at or
    above the restricted sensitivity threshold across the tenant's
    published CaseTypeVersions. The router resolves it from the
    schema store and the use case forwards it to the SQL adapter so
    snippets are redacted per-field (P11-US6 / P05-US3).
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    text_query: str | None = None
    filters: tuple[tuple[str, str, object], ...] = ()
    limit: int = 50
    offset: int = 0
    searchable_field_keys: tuple[str, ...] = ()
    sensitive_field_keys: tuple[str, ...] = ()


async def handle(
    query: CaseSearchQuery,
    *,
    search_provider: SearchProvider,
) -> SearchResultPage:
    """Run an authorized, portable case search.

    Failure behavior:
    - Authorization failure raises ``Forbidden`` (mapped to 403).
    - Unknown operator raises ``ValidationFailed`` (mapped to 422).
    - Non-searchable field in a filter raises ``ValidationFailed`` (400
      per acceptance criteria — the field key is rejected as a bad
      request).
    - ``limit`` / ``offset`` are clamped to the contract (``[1, 200]``
      / ``[0, ...)``).
    """
    _clamp_pagination(query)
    _validate_filters(query.filters)
    _reject_non_searchable_filters(query.filters, query.searchable_field_keys)

    actor_ctx = _ActorProxy(
        tenant_id=query.tenant_id,
        user_id=query.actor_id,
        roles=query.actor_roles,
    )
    # 1. Tenant + role gate. The policy is deny-by-default; missing
    # roles raise ``Forbidden`` here.
    await authorize(
        Action.CASE_READ,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=query.tenant_id, kind="case", id=None),
    )

    # 2. Compute the sensitive-read grant once so the SQL adapter can
    # project restricted values without re-evaluating the policy. The
    # grant is tenant-scoped; ``case_id`` is not consulted by the
    # policy, so we pass a fresh placeholder UUID to satisfy the typed
    # signature.
    can_see_sensitive = await actor_can_see_sensitive(
        tenant_id=query.tenant_id,
        actor_id=query.actor_id,
        actor_roles=query.actor_roles,
        case_id=uuid4(),
    )

    # 3. Translate the application filters into port filters.
    port_filters = tuple(SearchFilter(field=f[0], op=f[1], value=f[2]) for f in query.filters)

    spec = SearchSpec(
        tenant_id=query.tenant_id,
        text_query=query.text_query,
        filters=port_filters,
        limit=query.limit,
        offset=query.offset,
        can_see_sensitive=can_see_sensitive,
        searchable_field_keys=query.searchable_field_keys,
        sensitive_field_keys=query.sensitive_field_keys,
    )
    return await search_provider.search(spec)


def _validate_filters(filters: tuple[tuple[str, str, object], ...]) -> None:
    """Reject malformed filters (unknown operator, empty field, empty ``in``).

    The vocabulary is intentionally narrow: ``eq``, ``like``, ``in``.
    Expansion requires updating ``SUPPORTED_OPS`` AND the SQL adapter's
    operator dispatch AND the OpenAPI schema. Anything else is a
    client bug, surfaced as ``ValidationFailed``.
    """
    if not filters:
        return
    for field, op, value in filters:
        if not field or not isinstance(field, str):
            raise ValidationFailed("filter field must be a non-empty string", field="filters")
        if op not in SUPPORTED_OPS:
            raise ValidationFailed(
                f"unsupported filter operator {op!r} for field {field!r}; "
                f"supported: {sorted(SUPPORTED_OPS)}",
                field="filters",
            )
        if op == "in":
            seq = value if isinstance(value, (list, tuple)) else None
            if seq is None or len(seq) == 0:
                raise ValidationFailed(
                    f"operator 'in' on field {field!r} requires a non-empty list",
                    field="filters",
                )


def _reject_non_searchable_filters(
    filters: tuple[tuple[str, str, object], ...],
    searchable_field_keys: tuple[str, ...],
) -> None:
    """Reject filters on fields that are not declared ``searchable``.

    Core case columns (``status``, ``priority``, etc.) are always
    searchable. ``data.<key>`` filters must point at a key declared
    ``searchable`` in the tenant's published CaseTypeVersion(s). A
    non-searchable field is a client bug; the use case surfaces it as
    ``ValidationFailed`` so the router translates to 400 per the
    acceptance criteria.
    """
    if not filters:
        return
    searchable = set(searchable_field_keys) | set(CORE_FIELDS)
    for field, _op, _value in filters:
        # ``data.<key>`` filters strip the prefix before lookup.
        if field.startswith("data."):
            key = field[len("data.") :]
            if key not in searchable:
                raise ValidationFailed(
                    f"field {field!r} is not declared searchable",
                    field="filters",
                )
            continue
        if field not in searchable:
            raise ValidationFailed(
                f"field {field!r} is not a searchable field",
                field="filters",
            )


def _clamp_pagination(query: CaseSearchQuery) -> None:
    """Clamp ``limit`` / ``offset`` to the contract.

    Mirrors ``app.application.cases.list_cases``: ``limit`` is in
    ``[1, 200]``; ``offset`` is non-negative. Anything else is rejected
    as ``ValidationFailed`` so the SQL adapter never has to reason
    about out-of-range inputs.
    """
    if query.limit < 1 or query.limit > 200:
        raise ValidationFailed("limit must be in [1, 200]", field="limit")
    if query.offset < 0:
        raise ValidationFailed("offset must be >= 0", field="offset")


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy (see create_case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


__all__ = [
    "CORE_FIELDS",
    "CaseSearchQuery",
    "SUPPORTED_OPS",
    "handle",
]
