"""Governed GraphQL resolvers (P13-US11 / FM-057).

Every field on every ``OBJECT`` type in :mod:`app.interfaces.graphql.schema`
is resolved here as a single call into the application layer. The
resolvers never touch a repository, document store, or persistence
model directly — they are a thin translation layer between the GraphQL
field names and the canonical use-case functions.

# Why a separate module

The schema module is data-only so it can be introspected and audited
without spinning up the application layer. The resolver module is
where the policy is enforced: every call into ``authorize``,
``redact_case_data``, ``view_case.handle``,
``list_documents_for_case.handle`` happens here. Splitting them lets
the schema evolve (add a field) without changing policy code, and
lets the policy evolve (add an action) without changing schema
shape. The two only meet at the ``resolver=`` key on
:class:`GraphQLField` — that string is the seam.

# Boundary contract (Constitution V / AGENTS.md §4.R12)

1. **Service boundary** — resolvers call into ``app.application.*``
   only. A test that imports ``app.infrastructure.*`` from this
   module is a layering violation; the test file checks for it.

2. **Tenant boundary** — the actor's ``tenant_id`` is the only
   tenant identifier passed to a repository. Resolvers do not
   accept ``tenantId`` arguments; tenant isolation is implicit in
   the actor identity passed by the GraphQL transport.

3. **Field boundary** — for ``Case.dataFields`` the resolver runs
   :func:`app.application.cases.redactor.redact_case_data` with the
   actor's ``can_see_sensitive`` decision. The wire shape never
   includes the secret value.

4. **Document boundary** — ``Case.documents`` is routed through
   :mod:`app.application.documents.list_documents` which evaluates
   ``Action.DOCUMENT_READ`` and filters by the case-type's
   classification allowlist.

# Execution context

The resolver functions are coroutines; the executor (a small
parser/executor in :func:`execute`) calls them in the order they
appear in the query, in a single Python coroutine per request.
There is no batching, no DataLoader, no async iterator fanout.
These are deferred to a future slice when GraphQL traffic justifies
the complexity (FM-057 acceptance scenario M1..M3).

# Audit (P09)

The executor records a single ``GraphQLQueryExecuted`` audit row at
the end of a request — successful, denied, or errored — so the
audit chain captures the actor + query + correlation id even when
the policy raised before the response was assembled. The audit row
does not include the result data (Constitution V: secrets never
land in audit payloads).
"""

from __future__ import annotations

from app.domain.audit import GRAPHQL_QUERY_EXECUTED
from app.interfaces.graphql.resolvers.case_resolvers import validate_schema_wiring
from app.interfaces.graphql.resolvers.context import (
    _RESOLVERS as _RESOLVERS,
)
from app.interfaces.graphql.resolvers.context import (
    DEFAULT_PAGE_LIMIT,
    MAX_DEPTH,
    MAX_PAGE_LIMIT,
    MAX_QUERY_LENGTH,
    MAX_SELECTIONS,
    GraphQLResult,
    ResolverContext,
)
from app.interfaces.graphql.resolvers.context import (
    ResolverFn as ResolverFn,
)
from app.interfaces.graphql.resolvers.execution import execute
from app.interfaces.graphql.resolvers.parsing import (
    build_query_shape_audit,
    describe_query_shape,
    parse_query,
)

# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "GRAPHQL_QUERY_EXECUTED",
    "MAX_DEPTH",
    "MAX_PAGE_LIMIT",
    "MAX_QUERY_LENGTH",
    "MAX_SELECTIONS",
    "GraphQLResult",
    "ResolverContext",
    "build_query_shape_audit",
    "describe_query_shape",
    "execute",
    "parse_query",
    "validate_schema_wiring",
]
