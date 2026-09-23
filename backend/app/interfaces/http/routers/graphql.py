"""Governed GraphQL HTTP boundary (P13-US11 / FM-057).

Exposes the hand-rolled governed GraphQL surface from
:mod:`app.interfaces.graphql` over HTTP as ``POST /graphql``. The
endpoint is read-only by construction: the parser in
:mod:`app.interfaces.graphql.resolvers` rejects ``mutation``
operations, fragments, aliases, and variables, so writes keep flowing
through the REST API where idempotency, optimistic concurrency, and
the audit + outbox transaction guarantees live.

# Boundaries (Constitution V / AGENTS.md section 4 rule R12)

1. **Service boundary** — the handler delegates to
   :func:`app.interfaces.graphql.resolvers.execute`, which calls into
   ``app.application.*`` use cases. The router never touches a
   repository directly.
2. **Tenant boundary** — the actor's ``tenant_id`` from
   :func:`app.deps.get_current_user` is the only tenant identifier in
   the :class:`ResolverContext`. The schema exposes no ``tenantId``
   argument.
3. **Infrastructure boundary** — repositories reach the handler only
   through the :class:`CaseHttpDependencyProvider` bundle resolved via
   :func:`app.interfaces.http.dependencies.get_case_dependencies`
   (AGENTS.md section 4 rule R10). This module imports no
   ``app.infrastructure`` symbol.

# Status-code contract

- ``200`` — the query parsed and executed. ``data``/``errors`` follow
  the GraphQL shape: authorization denials and missing ids surface as
  entries in ``errors`` with the field set to ``null``, never as a
  leaked value.
- ``400`` — the query failed to parse (malformed syntax, ``mutation``
  operation, unknown root field) or the caller supplied ``variables``,
  which the executor does not support.
- ``401``/``403`` — raised by the standard auth handlers before the
  executor runs (missing identity, denied transport-level auth).

Mount (owned by the ``main.py`` worker — do not mount here)::

    from app.interfaces.http.routers.graphql import router as graphql_router

    app.include_router(graphql_router, prefix="/api/v1")
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cases.http_dependencies import CaseHttpDependencyProvider
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.domain.audit import GRAPHQL_QUERY_EXECUTED
from app.domain.errors import ValidationFailed
from app.interfaces.graphql.resolvers import ResolverContext, execute
from app.interfaces.http.dependencies import get_case_dependencies

router = APIRouter(tags=["graphql"])

# Upper bound for the raw query text. The executor is a hand-rolled
# recursive parser (no streaming), so an unbounded body would let a
# caller pin the event loop. 8000 characters is generous for the
# governed read shape (a Case + Document selection) while keeping the
# parse budget trivial. Pydantic rejects longer bodies with 422 before
# the executor runs.
MAX_QUERY_LENGTH = 8000


class GraphQLRequest(BaseModel):
    """The HTTP body for ``POST /graphql``.

    ``query`` is the raw GraphQL query text (queries only; ``mutation``
    operations are rejected by the parser). ``variables`` is accepted
    in the schema so clients get a clear 400 instead of a silent drop,
    but any non-empty value is rejected because the executor inlines
    all arguments and does not resolve variable references.
    """

    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    variables: dict[str, Any] | None = None


class GraphQLErrorItem(BaseModel):
    """One entry of the GraphQL ``errors`` array.

    ``code`` is the platform error code (``forbidden``, ``not_found``,
    ``validation_failed``); ``message`` is safe to display (never SQL,
    secrets, or stack traces); ``path`` is the JSON path of the failing
    field (for example ``["case", "documents"]``).
    """

    code: str
    message: str
    path: list[str] = Field(default_factory=list)


class GraphQLResponse(BaseModel):
    """The HTTP wire shape for ``POST /graphql``.

    ``data`` is the resolved field tree (a denied or missing branch is
    ``null``); ``errors`` carries per-field denials and root-level
    failures. A well-formed execution always returns HTTP 200, even
    when ``errors`` is non-empty — the status code describes the
    transport, the ``errors`` array describes the policy outcome.
    """

    data: dict[str, Any] = Field(default_factory=dict)
    errors: list[GraphQLErrorItem] = Field(default_factory=list)


@router.post(
    "/graphql",
    response_model=GraphQLResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute a governed read-only GraphQL query",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Query failed to parse or used variables"},
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or unknown caller identity"},
    },
)
async def execute_graphql_query(
    body: GraphQLRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    correlation: CorrelationContext = Depends(get_correlation_id),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
) -> GraphQLResponse:
    """Run a governed GraphQL query for the calling actor.

    Args:
        body: The query text plus optional (rejected) variables.
        user: The authenticated actor; ``tenant_id`` scopes every read.
        db: The request-scoped persistence session shared by the repo bundle.
        correlation: The request correlation id, propagated to resolvers.
        dependencies: The case HTTP port bundle bound to ``db``.

    Returns:
        The ``{data, errors}`` GraphQL envelope with HTTP 200.

    Raises:
        HTTPException: 400 when the query fails to parse (malformed
            syntax, ``mutation`` operation, unknown root field) or when
            ``variables`` are supplied, which the executor does not
            support. Authentication failures (401/403) surface via the
            standard handlers before this handler runs.
    """
    # why: the executor inlines every argument at parse time and has no
    # variable environment. Accepting-and-ignoring variables would let a
    # caller believe a parameterized query ran when it did not, so a
    # non-empty value fails closed with 400 instead of being dropped.
    if body.variables:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="variables are not supported by the governed surface; inline all arguments",
        )
    repositories = dependencies.for_session(db)
    ctx = ResolverContext(
        actor_id=user.user_id,
        tenant_id=user.tenant_id,
        actor_roles=tuple(user.roles),
        case_repo=repositories.case,
        case_type_version_repo=repositories.case_type_version,
        document_repo=repositories.document,
        correlation_id=correlation.correlation_id,
    )
    try:
        result = await execute(body.query, ctx)
    except ValidationFailed as exc:
        # why: parse-time failures raise out of ``execute`` before the
        # executor can build a ``{data, errors}`` envelope (unknown root
        # field, malformed syntax, ``mutation`` operation). They are a
        # client error, not a policy outcome, so they surface as 400
        # rather than flowing to the DomainError handler (which maps
        # ValidationFailed to 422 for REST field validation). The
        # executor already recorded one PHI-free shape entry on
        # ``ctx.audit_trail`` before raising, so persist it first.
        await _persist_query_audit(ctx, user=user, db=db, clock=clock, repositories=repositories)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    await _persist_query_audit(ctx, user=user, db=db, clock=clock, repositories=repositories)
    return GraphQLResponse(
        data=dict(result.data),
        errors=[
            GraphQLErrorItem(
                code=str(error.get("code", "internal_error")),
                message=str(error.get("message", "")),
                path=[str(part) for part in error.get("path", [])],
            )
            for error in result.errors
        ],
    )


async def _persist_query_audit(
    ctx: ResolverContext,
    *,
    user: UserContext,
    db: AsyncSession,
    clock: Clock,
    repositories: Any,
) -> None:
    """Persist the executor's PHI-free query-shape entry (ADR-014).

    The payload comes from ``ctx.audit_trail`` (root + field names +
    pagination window + outcome codes only — never argument values,
    ids, or result data, per Constitution V). The write lands in the
    request transaction next to the reads; a failing audit write
    rolls back only the audit row and the query result is still
    returned, because an observability outage must not become a
    retrieval outage on a read endpoint (ADR-039 pattern).

    Tenant isolation is implicit: the row is written under the
    actor's ``tenant_id`` from identity, never from a client
    argument (the schema exposes no ``tenantId``).
    """
    if not ctx.audit_trail:
        return
    entry = dict(ctx.audit_trail[-1])
    # why: strip transport-only keys before persisting so the audit
    # row carries exactly the governed shape contract.
    entry.pop("tenant_id", None)
    entry.pop("actor_id", None)
    entry.pop("correlation_id", None)
    try:
        await repositories.audit.append(
            tenant_id=user.tenant_id,
            event_type=GRAPHQL_QUERY_EXECUTED,
            actor_id=user.user_id,
            actor_type="user",
            subject_type="graphql_query",
            subject_id=user.user_id,
            case_id=None,
            correlation_id=ctx.correlation_id,
            causation_id=None,
            payload=entry,
            occurred_at=clock.now(),
        )
        await db.commit()
    except Exception:
        # why swallow: audit is observability, not the retrieval
        # contract; a full rollback here would turn an audit outage
        # into a query outage for every worker.
        await db.rollback()


__all__ = [
    "GraphQLErrorItem",
    "GraphQLRequest",
    "GraphQLResponse",
    "execute_graphql_query",
    "router",
]
