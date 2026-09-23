# ADR-014 — Governed GraphQL surface (P13-US11 / FM-057)

- **Status**: Accepted
- **Date**: 2026-09-02
- **Owner**: Platform team
- **Pillar**: P13 — API, Events & Extensibility
- **Spec**: `specs/pillars/P13-api-events-extensibility/pillar.md` §4 (P3
  user story P13-US11), `specs/pillars/01-deduplicated-feature-map.md:36`
  FM-057

## 1. Context

Pillar P13 §4 defers GraphQL to P3 ("GraphQL ... P3") so the platform
ships REST first. FM-057 advances one narrow read-only slice — a
**governed GraphQL query surface** — so ad-hoc consumers can join
across Case + Document + field-level redaction in a single round
trip without bypassing the existing service / tenant / field /
document boundaries.

The standing architectural question is whether to use a third-party
GraphQL framework (`strawberry-graphql`, `ariadne`, `graphene`) or
to build the minimum surface by hand. The constitution + ADR-001
lean toward a minimal-dependency posture; AGENTS.md §4.R3 ("prefer
boring over clever") and §4.R10 (modular monolith with explicit
layer boundaries) sharpen the question.

## 2. Options

### Option A — Adopt `strawberry-graphql` (or `ariadne` / `graphene`)

- **Pros**: introspection, fragments, aliases, variables, subscriptions,
  DataLoader batching, federation support. Less code to maintain on
  the surface side.
- **Cons**: Adds a new third-party dependency to `pyproject.toml`.
  Framework-specific resolvers introduce new abstractions (type
  classes, decorators) that the rest of the platform does not use,
  creating a parallel mental model. Federation + DataLoader are not
  in FM-057's acceptance scope. The framework's resolver decorators
  naturally invite bypassing the application layer (the framework
  itself does not enforce the service boundary).
- **Layer risk**: A `strawberry` resolver that returns a
  Pydantic-shaped dataclass is *also* valid GraphQL but lets a
  developer accidentally skip the policy evaluation that the REST
  layer always runs.

### Option B — Hand-rolled governed surface (chosen)

- **Pros**: Zero new dependencies. Resolvers are pure functions
  whose bodies are visibly a single call into the application
  layer. The schema catalog is a plain Python data structure that
  the operator console can introspect and audit. Field-level
  redaction, tenant isolation, and document policy enforcement are
  visible in the resolver signatures — not hidden in framework
  decorators.
- **Cons**: Limited GraphQL surface (no fragments, aliases,
  variables, subscriptions, federation, DataLoader batching). The
  hand-rolled lexer + parser is small but is its own surface to
  maintain. The executor's recursion logic is bespoke.

## 3. Decision

Adopt **Option B — hand-rolled governed GraphQL surface**, bounded
by the following contract:

1. The surface ships a `Query` root only. `Mutation`, `Subscription`,
   fragments, aliases, variables, and introspection are deliberately
   out of scope. Mutation lives behind the REST API so idempotency,
   optimistic concurrency, and the audit + outbox transaction
   boundary stay in one place.
3. Resolvers call **only** into `app.application.*`. A static
   `ruff`-style test (`tests/graphql/test_governed_graphql.py`
   `test_governed_graphql__resolvers_do_not_import_infrastructure`)
   pins this: any future commit that adds an `app.infrastructure` or
   `sqlalchemy` import to the resolver module fails review.
4. Tenant isolation is implicit — the actor's `tenant_id` (from the
   resolved identity in `ResolverContext`) is the only tenant
   identifier passed to a repository. The schema declares no
   `tenantId` argument; clients cannot target a cross-tenant id.
5. Field-level redaction (P11-US6) is delegated to
   `app.application.cases.redactor.redact_case_data` with the
   actor's `can_see_sensitive` decision. The literal `[REDACTED]`
   string is the only string the client sees for a sensitive field.
6. Document policy (P03-US4) is delegated to
   `app.application.documents.list_documents` which evaluates
   `Action.DOCUMENT_READ` and filters by the case-type's
   `document_classifications` allowlist.
7. Every declared field's `resolver` key resolves to a registered
   coroutine. The runtime check `validate_schema_wiring()` runs at
   the top of every `execute()` call so a missing resolver fails
   loud, not silent.

### Why the parser is so small

The full GraphQL spec is large; the platform needs four operations
(`case(id)`, `cases(limit,offset)`, plus their sub-selections).
Anything more complex — fragments, aliases, variables — pushes the
surface outside the FM-057 acceptance scenarios and would force
clients to learn framework-specific knobs they do not need.
Clients that need them use the REST API (AGENTS.md §10). The
parser is ~120 lines and is exercised by
`tests/graphql/test_governed_graphql.py::test_governed_graphql__unknown_field__rejected_at_parse`.

## 4. Consequences

### Positive

- Service / tenant / field / document boundaries are observable in
  the resolver code; a layered-violation test pins the contract.
- The schema catalog (`SCHEMA_MODEL`) is a plain dict that an
  operator console or audit tool can introspect without spinning up
  FastAPI or running a SQL query.
- New fields land as one `GraphQLField` declaration + one
  `@_register("...")` resolver function. The seam between the
  schema and the policy code is the `resolver=` string.
- The lexer + parser are deliberately small (no PEG, no LALR,
  no library) so the surface is auditable in one sitting.

### Negative

- No fragments / aliases / variables: clients must hand-write the
  exact fields they need. This is acceptable today (FM-057 is
  internal-first) but is a real ceiling. If a future slice needs
  fragments we revisit this ADR and either (a) extend the parser or
  (b) adopt a library once the trade-off has changed.
- No DataLoader batching: each resolver round-trips to the
  repository once per field. For the canonical Case + Documents
  query this is two SQL queries; for a deeply nested query this
  becomes N+1. The future slice that warrants it adds a
  `dataloader`-style cache bounded to a single request.
- The hand-rolled parser rejects any query shape outside the canonical
  form. A client sending a `mutation` keyword fails with
  `validation_failed`. This is intentional — see Decision §3.1.

### Operational

- `/api/v1/graphql` (future HTTP binding) is a single FastAPI
  endpoint that constructs a `ResolverContext` from the existing
  identity dependency (`app.deps.get_current_user`) and calls
  `app.interfaces.graphql.resolvers.execute(query, ctx)`. The
  transport inherits idempotency, correlation, and structured
  logging from the same middleware the REST API uses.
- Audit: every request lands one `GraphQLQueryExecuted` row in the
  audit chain (success + deny + error). The payload carries the
  actor id, the query root, and the correlation id; result data
  never appears in the audit row (Constitution V).
- Logging: the executor emits one `info`-level log per request with
  tenant_id, actor_id, correlation_id, root_field, error_count.
  Rejections are logged at `warning`. No payload values are logged.

## 5. Seam / Azure note

- The schema + resolver modules are pure (no infrastructure
  imports, verified by a static test). The HTTP binding
  (`POST /api/v1/graphql`) is a thin adapter that maps FastAPI's
  Request to the `ResolverContext`. Swapping the local
  `LocalHeaderIdentityProvider` for an Entra-backed adapter (the
  production seam per AGENTS.md §12) is the only change required to
  run the governed surface in Azure.
- The structured logging adapter (`app.infrastructure.telemetry`)
  is the only place the surface reaches into `app.infrastructure`.
  All other adapter calls are deferred to the application layer.

## 6. References

- Pillar P13 §4 — P13-US11 (P3)
- FM-057 in `specs/pillars/01-deduplicated-feature-map.md:36`
- AGENTS.md §4.R3 (skepticism), §4.R10 (layered monolith),
  §4.R12 (security first), §12 (integration seams)
- ADR-001 — Portable types (minimal-dependency posture)
- ADR-009 — SIEM (audit chain precedence)
- ADR-013 — Outbound webhooks (related bounded surface)
- Constitution V (no leaked secrets / stack / SQL in responses)
- Constitution VII (DB schema is not a contract — GraphQL is)