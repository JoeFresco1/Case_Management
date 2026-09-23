"""Bounded governed query-surface tests (P13-US11 / FM-057 hardening).

Covers the task acceptance for the bounded extension that reuses the
tenant, field, document, and sensitivity policy without exposing
persistence:

- unauthorized fields/docs return ``forbidden`` with no partial data;
- pagination and complexity bounds fail closed with
  ``validation_failed`` and an empty data graph;
- every execution records exactly one PHI-free query-shape audit
  entry on ``ctx.audit_trail``;
- cross-tenant reads stay invisible (null + error, empty list).

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5. In-memory fakes keep the suite off Docker (AGENTS.md §4.R1).
"""

from __future__ import annotations

from uuid import UUID

import pytest

from app.domain.document import DocumentSensitivity
from app.domain.errors import ValidationFailed
from app.interfaces.graphql import ResolverContext
from app.interfaces.graphql import execute as gql_execute
from app.interfaces.graphql.resolvers import (
    MAX_QUERY_LENGTH,
    MAX_SELECTIONS,
    describe_query_shape,
    parse_query,
)
from tests.application.cases._fakes import (
    InMemoryCaseRepository,
    InMemoryCaseTypeVersionRepository,
)

from ._fakes import (
    InMemoryDocumentRepository,
    make_case_with_restricted_schema,
    make_document,
)

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ACTOR_INTAKE = UUID("33333333-3333-3333-3333-333333333333")

SECRET_SSN = "123-45-6789"
PUBLIC_TITLE = "Open request"


def _build_ctx(
    *,
    actor_id: UUID = ACTOR_INTAKE,
    tenant_id: UUID = TENANT_A,
    roles: tuple[str, ...] = ("intake",),
    case_repo: InMemoryCaseRepository | None = None,
    document_repo: InMemoryDocumentRepository | None = None,
    version_repo: InMemoryCaseTypeVersionRepository | None = None,
) -> ResolverContext:
    """Build a resolver context over in-memory fakes (test helper)."""
    return ResolverContext(
        actor_id=actor_id,
        tenant_id=tenant_id,
        actor_roles=roles,
        case_repo=case_repo or InMemoryCaseRepository(),
        case_type_version_repo=version_repo or InMemoryCaseTypeVersionRepository(),
        document_repo=document_repo or InMemoryDocumentRepository(),
        correlation_id="cid-bounds-test",
    )


def _seed_case_with_schema(*, ctx: ResolverContext) -> tuple[UUID, UUID]:
    """Seed a Case + CaseTypeVersion into the in-memory repos."""
    case, version = make_case_with_restricted_schema(
        tenant_id=ctx.tenant_id,
        actor_id=ctx.actor_id,
        title=PUBLIC_TITLE,
        ssn=SECRET_SSN,
    )
    ctx.case_repo._cases.append(case)  # type: ignore[attr-defined]
    ctx.case_type_version_repo._by_id[version.id] = version  # type: ignore[attr-defined]
    return case.id, version.id


# ---------------------------------------------------------------------------
# Forbidden with no partial data
# ---------------------------------------------------------------------------


async def test_governed_query__unauthorized_documents__forbidden_with_null_branch() -> None:
    """A denied ``documents`` sub-selection is null; no doc value leaks."""
    # // given a case with one document and an actor lacking DOCUMENT_READ
    ctx = _build_ctx(roles=("intake",))
    case_id, _ = _seed_case_with_schema(ctx=ctx)
    document = make_document(tenant_id=ctx.tenant_id, case_id=case_id, classification="Contract")
    ctx.document_repo.add(document)

    # // when the actor selects documents alongside authorized scalars
    result = await gql_execute(
        f'query {{ case(id: "{case_id}") {{ id documents {{ id }} }} }}',
        ctx,
    )

    # // then the documents branch is null + forbidden, and the doc id never leaks
    assert len(result.errors) == 1
    assert result.errors[0]["code"] == "forbidden"
    assert result.errors[0]["path"] == ["case", "documents"]
    assert result.data["case"]["documents"] is None
    assert str(document.id) not in repr(result.data)
    assert str(document.id) not in repr([e.get("path") for e in result.errors])


async def test_governed_query__root_forbidden__null_branch_not_partial() -> None:
    """A root-level denial yields an explicit null branch (no partial object)."""
    # // given a case and an actor with no CASE_READ grant at all
    ctx = _build_ctx(roles=("no-such-role",))
    case_id, _ = _seed_case_with_schema(ctx=ctx)

    # // when the unauthorized actor queries the case root
    result = await gql_execute(
        f'query {{ case(id: "{case_id}") {{ id reference }} }}',
        ctx,
    )

    # // then the branch is explicit null (key present, value None), not a partial object
    assert len(result.errors) == 1
    assert result.errors[0]["code"] == "forbidden"
    assert "case" in result.data
    assert result.data["case"] is None


# ---------------------------------------------------------------------------
# Pagination bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [0, -1, 201, 1000])
async def test_governed_query__out_of_range_limit__rejected(limit: int) -> None:
    """``limit`` outside [1, 200] fails closed with no partial data."""
    # // given any actor context (parse/execution validation precedes reads)
    ctx = _build_ctx()

    # // when the caller pages outside the governed window
    result = await gql_execute(
        f"query {{ cases(limit: {limit}, offset: 0) {{ id }} }}",
        ctx,
    )

    # // then the response is validation_failed with an empty data graph
    assert result.data == {}
    assert len(result.errors) == 1
    assert result.errors[0]["code"] == "validation_failed"


async def test_governed_query__negative_offset__rejected() -> None:
    """A negative ``offset`` fails closed with no partial data."""
    # // given any actor context
    ctx = _build_ctx()

    # // when the caller passes a negative offset
    result = await gql_execute(
        "query { cases(limit: 10, offset: -1) { id } }",
        ctx,
    )

    # // then the response is validation_failed with an empty data graph
    assert result.data == {}
    assert result.errors[0]["code"] == "validation_failed"


async def test_governed_query__unknown_root_arg__rejected() -> None:
    """Undeclared root arguments fail closed instead of being ignored."""
    # // given any actor context
    ctx = _build_ctx()

    # // when the caller passes an undeclared argument
    result = await gql_execute(
        "query { cases(limit: 10, offset: 0, foo: 1) { id } }",
        ctx,
    )

    # // then the response is validation_failed with an empty data graph
    assert result.data == {}
    assert result.errors[0]["code"] == "validation_failed"


# ---------------------------------------------------------------------------
# Complexity bounds
# ---------------------------------------------------------------------------


async def test_governed_query__mutation__rejected_at_parse() -> None:
    """``mutation`` operations never reach a resolver; the audit trail still records."""
    # // given any actor context
    ctx = _build_ctx()

    # // when the caller posts a mutation
    with pytest.raises(ValidationFailed):
        await gql_execute('mutation { case(id: "x") { id } }', ctx)

    # // then exactly one unknown-root audit entry exists with no ids
    assert len(ctx.audit_trail) == 1
    assert ctx.audit_trail[0]["root"] == "unknown"


async def test_governed_query__variables__rejected_at_parse() -> None:
    """Variable references (``$``) are rejected before lexing."""
    # // given any actor context
    ctx = _build_ctx()

    # // when the caller references a variable
    with pytest.raises(ValidationFailed):
        await gql_execute(
            "query { case(id: $id) { id } }",
            ctx,
        )

    # // then the audit trail records the rejection without values
    assert len(ctx.audit_trail) == 1


async def test_governed_query__oversize_body__rejected_at_parse() -> None:
    """Bodies over MAX_QUERY_LENGTH are rejected before parsing."""
    # // given any actor context
    ctx = _build_ctx()

    # // when the caller posts an oversize body
    with pytest.raises(ValidationFailed):
        await gql_execute(
            "query { cases(limit: 10, offset: 0) { id } }" + (" " * MAX_QUERY_LENGTH), ctx
        )

    # // then the audit trail records the rejection
    assert len(ctx.audit_trail) == 1


async def test_governed_query__too_many_selections__rejected() -> None:
    """Selection sets over MAX_SELECTIONS are rejected with no resolver work."""
    # // given any actor context
    ctx = _build_ctx()
    case_id, _ = _seed_case_with_schema(ctx=ctx)

    # // when the caller selects more fields than the budget allows
    fields = " ".join(["id"] * (MAX_SELECTIONS + 1))
    with pytest.raises(ValidationFailed):
        await gql_execute(f'query {{ case(id: "{case_id}") {{ {fields} }} }}', ctx)


def test_describe_query_shape__never_carries_values_or_ids() -> None:
    """The safe shape carries names + window only (no ids, no secrets)."""
    # // given a parsed query with an id argument
    parsed = parse_query('query { case(id: "some-uuid-value") { id dataFields { key } } }')

    # // when projected to the audit shape
    shape = describe_query_shape(parsed)

    # // then names and counts survive but the id value does not
    assert shape["root"] == "case"
    assert "id" in shape["fields"]
    assert "some-uuid-value" not in repr(shape)
    assert shape["limit"] is None


# ---------------------------------------------------------------------------
# Safe query-shape audit
# ---------------------------------------------------------------------------


async def test_governed_query__success__records_single_safe_audit_entry() -> None:
    """Every execution appends exactly one PHI-free audit entry."""
    # // given an authorized case query
    ctx = _build_ctx()
    case_id, _ = _seed_case_with_schema(ctx=ctx)

    # // when the query succeeds
    result = await gql_execute(
        f'query {{ case(id: "{case_id}") {{ id reference dataFields {{ key value }} }} }}',
        ctx,
    )

    # // then one audit entry exists with shape-only payload
    assert result.errors == ()
    assert len(ctx.audit_trail) == 1
    entry = ctx.audit_trail[0]
    assert entry["event_type"] == "GraphQLQueryExecuted"
    assert entry["root"] == "case"
    assert entry["outcome"] == "ok"
    assert entry["result_count"] == 1
    # // and no secret, id value, or raw text leaked into the entry
    assert SECRET_SSN not in repr(entry)
    assert str(case_id) not in repr(entry)


async def test_governed_query__denied__audit_marks_denied_without_values() -> None:
    """A denied sub-selection is audited as denied with codes only."""
    # // given an actor lacking DOCUMENT_READ
    ctx = _build_ctx(roles=("intake",))
    case_id, _ = _seed_case_with_schema(ctx=ctx)
    document = make_document(tenant_id=ctx.tenant_id, case_id=case_id, classification="Contract")
    ctx.document_repo.add(document)

    # // when documents are denied at sub-selection depth
    result = await gql_execute(
        f'query {{ case(id: "{case_id}") {{ id documents {{ id }} }} }}',
        ctx,
    )

    # // then the audit entry marks denied with the code, but no doc id
    assert result.errors[0]["code"] == "forbidden"
    assert len(ctx.audit_trail) == 1
    entry = ctx.audit_trail[0]
    assert entry["outcome"] == "denied"
    assert "forbidden" in entry["error_codes"]
    assert str(document.id) not in repr(entry)


# ---------------------------------------------------------------------------
# Cross-tenant invisibility
# ---------------------------------------------------------------------------


async def test_governed_query__cross_tenant_case__null_error_and_audit() -> None:
    """A cross-tenant id is null + error; the audit carries no cross-tenant data."""
    # // given a case in tenant A
    ctx_a = _build_ctx(tenant_id=TENANT_A, roles=("intake",))
    case_id_a, _ = _seed_case_with_schema(ctx=ctx_a)

    # // when tenant B queries it through the shared repos
    ctx_b = ResolverContext(
        actor_id=ACTOR_INTAKE,
        tenant_id=TENANT_B,
        actor_roles=("intake",),
        case_repo=ctx_a.case_repo,
        case_type_version_repo=ctx_a.case_type_version_repo,
        document_repo=ctx_a.document_repo,
        correlation_id="cid-cross-tenant",
    )
    result = await gql_execute(f'query {{ case(id: "{case_id_a}") {{ id reference }} }}', ctx_b)

    # // then the branch is null with one error and the audit leaks nothing
    assert result.data["case"] is None
    assert len(result.errors) == 1
    assert result.errors[0]["code"] in {"forbidden", "not_found"}
    assert len(ctx_b.audit_trail) == 1
    assert PUBLIC_TITLE not in repr(ctx_b.audit_trail[0])
    assert SECRET_SSN not in repr(ctx_b.audit_trail[0])


async def test_governed_query__cross_tenant_list__empty_with_audit() -> None:
    """Tenant B lists zero rows for tenant A's data; the audit still lands."""
    # // given a case seeded in tenant A
    ctx_a = _build_ctx(tenant_id=TENANT_A)
    _seed_case_with_schema(ctx=ctx_a)

    # // when tenant B lists through the shared repos
    ctx_b = ResolverContext(
        actor_id=ACTOR_INTAKE,
        tenant_id=TENANT_B,
        actor_roles=("intake",),
        case_repo=ctx_a.case_repo,
        case_type_version_repo=ctx_a.case_type_version_repo,
        document_repo=ctx_a.document_repo,
        correlation_id="cid-cross-list",
    )
    result = await gql_execute("query { cases(limit: 50, offset: 0) { id } }", ctx_b)

    # // then the list is empty with no errors and one ok audit entry
    assert result.errors == ()
    assert result.data["cases"] == []
    assert len(ctx_b.audit_trail) == 1
    assert ctx_b.audit_trail[0]["outcome"] == "ok"
    assert ctx_b.audit_trail[0]["result_count"] == 0


async def test_governed_query__cross_tenant_documents__not_visible() -> None:
    """Documents seeded in tenant A never surface through a tenant-B query."""
    # // given a case + document in tenant A readable by a reviewer
    ctx_a = _build_ctx(tenant_id=TENANT_A, roles=("reviewer",))
    case_id_a, _ = _seed_case_with_schema(ctx=ctx_a)
    document = make_document(
        tenant_id=TENANT_A,
        case_id=case_id_a,
        classification="Contract",
        sensitivity=DocumentSensitivity.INTERNAL,
    )
    ctx_a.document_repo.add(document)

    # // when tenant B (reviewer) queries the same case id
    ctx_b = ResolverContext(
        actor_id=ACTOR_INTAKE,
        tenant_id=TENANT_B,
        actor_roles=("reviewer",),
        case_repo=ctx_a.case_repo,
        case_type_version_repo=ctx_a.case_type_version_repo,
        document_repo=ctx_a.document_repo,
        correlation_id="cid-cross-docs",
    )
    result = await gql_execute(
        f'query {{ case(id: "{case_id_a}") {{ id documents {{ id }} }} }}',
        ctx_b,
    )

    # // then the case branch is null (tenant boundary first) with no doc leak
    assert result.data["case"] is None
    assert len(result.errors) == 1
    assert str(document.id) not in repr(result.data)
