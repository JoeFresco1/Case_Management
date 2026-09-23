"""Governed GraphQL interface (P13-US11 / FM-057).

Exposes a thin, policy-enforced GraphQL surface for ad-hoc queries.
Every field is resolved through the application layer; no field
bypasses the service / tenant / field / document boundaries. See
:mod:`app.interfaces.graphql.schema` for the schema catalog and
:mod:`app.interfaces.graphql.resolvers` for the field dispatch.

# Boundaries (Constitution V / AGENTS.md §4.R12)

1. Service boundary — resolvers call into ``app.application.*``.
2. Tenant boundary — the actor's ``tenant_id`` is the only tenant
   identifier passed to a repository.
3. Field boundary — ``Case.dataFields`` is gated by the
   published ``CaseTypeVersion.schema`` and the actor's
   ``can_see_sensitive`` decision.
4. Document boundary — ``Case.documents`` is gated by
   ``Action.DOCUMENT_READ`` and the case-type's classification
   allowlist.
"""

from __future__ import annotations

from app.interfaces.graphql.resolvers import (
    GraphQLResult,
    ResolverContext,
    execute,
    parse_query,
    validate_schema_wiring,
)
from app.interfaces.graphql.schema import (
    CASE_STATUS_ENUM,
    CASE_TYPE,
    DATA_FIELD_TYPE,
    DOCUMENT_SENSITIVITY_ENUM,
    DOCUMENT_TYPE,
    QUERY_TYPE,
    SCHEMA_MODEL,
)

__all__ = [
    "CASE_STATUS_ENUM",
    "DOCUMENT_SENSITIVITY_ENUM",
    "CASE_TYPE",
    "DATA_FIELD_TYPE",
    "DOCUMENT_TYPE",
    "GraphQLResult",
    "QUERY_TYPE",
    "ResolverContext",
    "SCHEMA_MODEL",
    "execute",
    "parse_query",
    "validate_schema_wiring",
]
