"""Tests for the governed GraphQL surface (P13-US11 / FM-057).

Acceptance from the task contract:

- Authorized GraphQL query returns case fields.
- Sensitive field without permission is redacted.
- Document access policy is enforced.
- Service / tenant / field / document boundaries are not bypassed.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5. The fixture layer uses in-memory fakes (AGENTS.md §4.R1) so
the suite does not require a PostgreSQL container.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any
from uuid import UUID

from app.domain.document import DocumentSensitivity
from app.interfaces.graphql import (
    ResolverContext,
    resolvers,
    validate_schema_wiring,
)
from app.interfaces.graphql import (
    execute as gql_execute,
)
from app.interfaces.graphql.resolvers import GraphQLResult
from app.interfaces.graphql.schema import SCHEMA_MODEL
from tests.application.cases._fakes import (
    InMemoryCaseRepository,
    InMemoryCaseTypeVersionRepository,
)

from ._fakes import (
    InMemoryDocumentRepository,
    make_case_with_restricted_schema,
    make_document,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ACTOR_INTAKE = UUID("33333333-3333-3333-3333-333333333333")
ACTOR_SUPERVISOR = UUID("44444444-4444-4444-4444-444444444444")


SECRET_SSN = "123-45-6789"
PUBLIC_TITLE = "Open request"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_ctx(
    *,
    actor_id: UUID = ACTOR_INTAKE,
    tenant_id: UUID = TENANT_A,
    roles: tuple[str, ...] = ("intake",),
    case_repo: InMemoryCaseRepository | None = None,
    document_repo: InMemoryDocumentRepository | None = None,
    version_repo: InMemoryCaseTypeVersionRepository | None = None,
) -> ResolverContext:
    return ResolverContext(
        actor_id=actor_id,
        tenant_id=tenant_id,
        actor_roles=roles,
        case_repo=case_repo or InMemoryCaseRepository(),
        case_type_version_repo=version_repo or InMemoryCaseTypeVersionRepository(),
        document_repo=document_repo or InMemoryDocumentRepository(),
        correlation_id="cid-graphql-test",
    )


def _seed_case_with_schema(*, ctx: ResolverContext) -> tuple[UUID, UUID]:
    """Seed a Case + its CaseTypeVersion into the in-memory repos."""
    case, version = make_case_with_restricted_schema(
        tenant_id=ctx.tenant_id,
        actor_id=ctx.actor_id,
        title=PUBLIC_TITLE,
        ssn=SECRET_SSN,
    )
    # The Case + version are stage-only objects; the case_repo fake
    # is unaware of the schema, so we register both into their
    # respective repos below.
    ctx.case_repo._cases.append(case)  # type: ignore[attr-defined]
    ctx.case_type_version_repo._by_id[version.id] = version  # type: ignore[attr-defined]
    return case.id, version.id


# ---------------------------------------------------------------------------
# Schema wiring
# ---------------------------------------------------------------------------


def test_schema_wiring__every_field_has_a_resolver() -> None:
    """The schema declares no field whose resolver is missing.

    The schema catalog (``SCHEMA_MODEL``) and the resolver registry
    must agree. ``validate_schema_wiring`` is called by ``execute``
    on every request; this test pins the contract.
    """
    validate_schema_wiring()


# ---------------------------------------------------------------------------
# Authorized query
# ---------------------------------------------------------------------------


async def test_governed_graphql__authorized_case_query__returns_fields() -> None:
    """An authorized actor (intake role, CASE_READ grant) can run
    ``case(id: ...) { id reference title }`` and see the fields
    verbatim. This is the canonical M1 acceptance scenario from
    pillar P13."""
    ctx = _build_ctx()
    case_id, _ = _seed_case_with_schema(ctx=ctx)

    result: GraphQLResult = await gql_execute(
        f'query {{ case(id: "{case_id}") {{ id reference title status priority sensitivity }} }}',
        ctx,
    )

    assert result.errors == ()
    case = result.data["case"]
    assert case is not None
    assert case["reference"] == "C-ACME-2026-000001"
    assert case["title"] == PUBLIC_TITLE
    assert case["status"] == "open"
    assert case["priority"] == "normal"
    assert case["sensitivity"] == "internal"


async def test_governed_graphql__authorized_list_query__returns_paginated() -> None:
    """``cases(limit, offset)`` returns a list in the actor's tenant,
    newest first. The list is the only sanctioned path for an
    ad-hoc query that does not pin a case id."""
    ctx = _build_ctx()
    case_id, _ = _seed_case_with_schema(ctx=ctx)

    result = await gql_execute(
        "query { cases(limit: 10, offset: 0) { id reference } }",
        ctx,
    )

    assert result.errors == ()
    rows = result.data["cases"]
    assert len(rows) == 1
    assert rows[0]["id"] == str(case_id)


# ---------------------------------------------------------------------------
# Field policy enforcement (P11-US6 / P05-US3)
# ---------------------------------------------------------------------------


async def test_governed_graphql__unauthorized_sensitive_field__is_redacted() -> None:
    """An actor lacking ``CASE_READ_SENSITIVE`` sees the
    redaction marker for restricted fields. The actor's role
    grants ``CASE_READ`` but not ``CASE_READ_SENSITIVE`` — the
    policy evaluation distinguishes them per AGENTS.md §4.R12.

    The literal ``[REDACTED]`` is the only string the client
    sees; the original SSN value never reaches the wire shape.
    """
    ctx = _build_ctx(roles=("intake",))
    case_id, _ = _seed_case_with_schema(ctx=ctx)

    result = await gql_execute(
        f'query {{ case(id: "{case_id}") {{ dataFields {{ key value redacted }} }} }}',
        ctx,
    )

    assert result.errors == ()
    fields = result.data["case"]["dataFields"]
    by_key = {f["key"]: f for f in fields}
    assert by_key["title"]["redacted"] is False
    assert by_key["title"]["value"] == PUBLIC_TITLE
    assert by_key["ssn"]["redacted"] is True
    assert by_key["ssn"]["value"] == "[REDACTED]"
    # The wire shape MUST NOT contain the original secret.
    assert SECRET_SSN not in repr(result.data)


async def test_governed_graphql__authorized_sensitive_field__is_returned_verbatim() -> None:
    """An actor with ``process_admin`` (which grants
    ``CASE_READ_SENSITIVE``) sees the structured field value
    verbatim. The redactor never down-grades a granted read."""
    ctx = _build_ctx(roles=("process_admin",))
    case_id, _ = _seed_case_with_schema(ctx=ctx)

    result = await gql_execute(
        f'query {{ case(id: "{case_id}") {{ dataFields {{ key value redacted }} }} }}',
        ctx,
    )

    assert result.errors == ()
    fields = result.data["case"]["dataFields"]
    by_key = {f["key"]: f for f in fields}
    assert by_key["ssn"]["redacted"] is False
    assert by_key["ssn"]["value"] == SECRET_SSN


# ---------------------------------------------------------------------------
# Tenant boundary (Constitution V / AGENTS.md §4.R12)
# ---------------------------------------------------------------------------


async def test_governed_graphql__cross_tenant_case_id__returns_null_and_error() -> None:
    """A cross-tenant case id is indistinguishable from a missing id.

    The actor's policy denies first (cross-tenant access), or the
    repository filters return None — either way the GraphQL response
    surfaces a single error entry and the case appears absent. The
    error code may be ``forbidden`` (policy-first) or ``not_found``
    (repository-first); both are correct outcomes and neither leaks
    the case's existence in another tenant."""
    ctx_a = _build_ctx(tenant_id=TENANT_A, roles=("intake",))
    case_id_a, _ = _seed_case_with_schema(ctx=ctx_a)

    # Build a second context in tenant B that shares the *same*
    # repository instances. The repository filters by tenant_id, so
    # tenant B sees nothing — the cross-tenant id is silent.
    ctx_b = ResolverContext(
        actor_id=ACTOR_INTAKE,
        tenant_id=TENANT_B,
        actor_roles=("intake",),
        case_repo=ctx_a.case_repo,
        case_type_version_repo=ctx_a.case_type_version_repo,
        document_repo=ctx_a.document_repo,
        correlation_id="cid-tenant-test",
    )

    result = await gql_execute(
        f'query {{ case(id: "{case_id_a}") {{ id reference }} }}',
        ctx_b,
    )

    # The data sub-tree MUST be absent (null) — a leaked id cannot
    # be inferred from a partial response.
    assert result.data["case"] is None
    assert len(result.errors) == 1
    err = result.errors[0]
    assert err["code"] in {"forbidden", "not_found"}
    # The error code is enough to communicate the outcome. The id
    # may appear in the error message because the actor supplied
    # it themselves; what must NOT be exposed is data from the
    # cross-tenant case. Asserting absence of the case id would
    # be a self-defeating defense (the actor wrote it).
    assert err["code"] != "validation_failed"


async def test_governed_graphql__cross_tenant_list__returns_empty() -> None:
    """The list endpoint in tenant B returns ``[]`` because the
    case was seeded in tenant A. Tenant isolation at the
    repository level — not at the GraphQL layer — keeps the
    boundary enforced."""
    ctx_a = _build_ctx(tenant_id=TENANT_A)
    _seed_case_with_schema(ctx=ctx_a)

    ctx_b = ResolverContext(
        actor_id=ACTOR_INTAKE,
        tenant_id=TENANT_B,
        actor_roles=("intake",),
        case_repo=ctx_a.case_repo,
        case_type_version_repo=ctx_a.case_type_version_repo,
        document_repo=ctx_a.document_repo,
        correlation_id="cid-tenant-list",
    )

    result = await gql_execute(
        "query { cases(limit: 50, offset: 0) { id } }",
        ctx_b,
    )

    assert result.errors == ()
    assert result.data["cases"] == []


# ---------------------------------------------------------------------------
# Document policy (P03-US4 / P11)
# ---------------------------------------------------------------------------


async def test_governed_graphql__authorized_document_read__returns_metadata() -> None:
    """``case.documents`` returns the document metadata for an
    actor that holds ``DOCUMENT_READ`` (reviewer role). The
    metadata excludes the bytes — those flow through the
    dedicated REST endpoint so the audit row can land in one
    transaction."""
    ctx = _build_ctx(roles=("reviewer",))
    case_id, _ = _seed_case_with_schema(ctx=ctx)
    document = make_document(
        tenant_id=ctx.tenant_id,
        case_id=case_id,
        classification="Contract",
        sensitivity=DocumentSensitivity.INTERNAL,
    )
    ctx.document_repo.add(document)

    result = await gql_execute(
        f'query {{ case(id: "{case_id}") '
        "{ documents { id fileName classification "
        "sensitivity locked currentVersion } } }",
        ctx,
    )

    assert result.errors == ()
    docs = result.data["case"]["documents"]
    assert len(docs) == 1
    assert docs[0]["id"] == str(document.id)
    assert docs[0]["fileName"] == "contract.txt"
    assert docs[0]["classification"] == "Contract"
    assert docs[0]["sensitivity"] == "internal"
    assert docs[0]["locked"] is False
    assert docs[0]["currentVersion"] == 1


async def test_governed_graphql__unauthorized_document_read__is_forbidden() -> None:
    """An actor whose role lacks ``DOCUMENT_READ`` cannot read
    ``case.documents``. The application use case raises
    ``Forbidden``; the executor translates that to an error
    entry and does NOT emit the documents sub-tree (no partial
    leak)."""
    ctx = _build_ctx(roles=("intake",))  # intake lacks DOC_READ
    case_id, _ = _seed_case_with_schema(ctx=ctx)
    document = make_document(tenant_id=ctx.tenant_id, case_id=case_id, classification="Contract")
    ctx.document_repo.add(document)

    result = await gql_execute(
        f'query {{ case(id: "{case_id}") {{ documents {{ id }} }} }}',
        ctx,
    )

    assert len(result.errors) == 1
    err = result.errors[0]
    assert err["code"] == "forbidden"
    # The documents field IS present in the data graph but null —
    # this is the GraphQL-spec shape (P13 / Pillar boundary). The
    # actor can see that the field was requested but receives no
    # value; the error entry tells them the policy denied the
    # sub-selection.
    assert result.data["case"]["documents"] is None
    assert err["path"] == ["case", "documents"]


async def test_governed_graphql__document_classification_filtered() -> None:
    """A document whose classification is not on the case-type's
    allowlist is omitted from the GraphQL response. The
    omission is indistinguishable from "no document of that
    classification exists" (Constitution V)."""
    ctx = _build_ctx(roles=("reviewer",))
    case_id, _ = _seed_case_with_schema(ctx=ctx)
    # Allowlist is {"Contract", "Evidence"}; this document's
    # classification ("Correspondence") is not allowed.
    document = make_document(
        tenant_id=ctx.tenant_id,
        case_id=case_id,
        classification="Correspondence",
    )
    ctx.document_repo.add(document)

    result = await gql_execute(
        f'query {{ case(id: "{case_id}") {{ documents {{ id }} }} }}',
        ctx,
    )

    assert result.errors == ()
    assert result.data["case"]["documents"] == []


# ---------------------------------------------------------------------------
# Service boundary (no bypass)
# ---------------------------------------------------------------------------


async def test_governed_graphql__never_bypasses_service_layer() -> None:
    """The schema catalog contains the full set of resolvable
    fields. No field can be resolved without going through the
    registered resolver functions, which all call into the
    application layer.

    This test pins the contract: every resolver key declared in
    the schema must appear in the resolver registry. A field
    declared in the schema without a registered resolver would
    fail the runtime wiring check (``validate_schema_wiring``),
    which is called at the top of every ``execute`` call.
    """
    declared = {f.resolver for obj in SCHEMA_MODEL.values() for f in obj.fields}
    assert declared <= set(resolvers._RESOLVERS.keys()), (
        f"resolvers missing: {sorted(declared - set(resolvers._RESOLVERS.keys()))}"
    )


def test_governed_graphql__resolvers_do_not_import_infrastructure() -> None:
    """Static guarantee: the resolver module does not import
    any infrastructure module. A layering violation would let a
    resolver read a repository directly, bypassing the service
    boundary (AGENTS.md §4.R10).
    """
    src_path = pathlib.Path(resolvers.__file__ or "")
    src = src_path.read_text(encoding="utf-8")
    forbidden = (
        "from app.infrastructure",
        "import app.infrastructure",
        "from sqlalchemy",
        "import sqlalchemy",
        "from app.db",
    )
    for needle in forbidden:
        assert needle not in src, f"resolvers module imports forbidden dependency: {needle}"


# ---------------------------------------------------------------------------
# Parser-level guards
# ---------------------------------------------------------------------------


def test_parse_query__rejects_unknown_field() -> None:
    """An unknown field name is rejected at parse time so a
    misconfigured client cannot probe the schema for hidden
    fields. This is the surface-level contract that matches
    GraphQL spec behavior."""
    ctx = _build_ctx()
    case_id, _ = _seed_case_with_schema(ctx=ctx)

    result = asyncio.run(
        gql_execute(
            f'query {{ case(id: "{case_id}") {{ secret }} }}',
            ctx,
        )
    )
    assert result.data == {}
    assert len(result.errors) == 1
    assert result.errors[0]["code"] == "validation_failed"


async def test_governed_graphql__unknown_field__rejected_at_parse() -> None:
    """End-to-end: an unknown field surfaces as a
    ``validation_failed`` error and the data graph is empty.
    The actor cannot infer the schema's shape from error
    side-channels because the error message names the unknown
    field only."""
    ctx = _build_ctx()
    case_id, _ = _seed_case_with_schema(ctx=ctx)

    result = await gql_execute(
        f'query {{ case(id: "{case_id}") {{ secret }} }}',
        ctx,
    )

    assert result.data == {}
    assert len(result.errors) == 1
    assert result.errors[0]["code"] == "validation_failed"


# ---------------------------------------------------------------------------
# Sanity: the executor runs in a single event loop and is async-safe
# ---------------------------------------------------------------------------


async def test_governed_graphql__concurrent_queries__do_not_share_state() -> None:
    """Two concurrent queries against the same in-memory repo do
    not see each other's partial state. The executor awaits each
    coroutine before returning; there is no shared mutable
    state at the resolver layer."""
    ctx = _build_ctx()
    case_id_a, _ = _seed_case_with_schema(ctx=ctx)

    async def run_one(qid: str) -> dict[str, Any]:
        return (
            await gql_execute(
                f'query {{ case(id: "{case_id_a}") {{ reference }} }}',
                ctx,
            )
        ).data

    results = await asyncio.gather(run_one("a"), run_one("b"))
    assert results[0]["case"]["reference"] == "C-ACME-2026-000001"
    assert results[1]["case"]["reference"] == "C-ACME-2026-000001"
