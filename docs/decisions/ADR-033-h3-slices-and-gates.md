# ADR-033: H3 implementation slices and gated positions (FM-034/038/056/057)

- Status: Accepted (2026-09-11)
- Scope: semantic search endpoint, form-session durability, outbound
  delivery loop, GraphQL mount; verification of the remaining H3 rows.
- Related: FM-034, FM-038, FM-057, FM-056, FM-050, ADR-012, ADR-013.

## Context

H3 assessment found four genuine wiring gaps (scaffolded domain/app
with no operable surface) and eighteen rows that were already
operable. Three parallel slices plus one direct implementation
closed the gaps.

## Decision

1. **FM-034** — `POST /search/semantic` on the search router, same
   envelope as structured search. `SearchHttpRepositories` gains
   `ranker: SearchRanker` wired to `NoOpSearchRanker` locally; the
   Azure ranker remains the untouched seam. FM-050's boundary holds
   by construction: the ranker only reorders the authorized
   candidate set, the recommendation is logged separately from the
   deterministic decision, and nothing in the transition/authorization
   path calls the ranker.
2. **FM-038** — `form_sessions` table (migration `0024`) +
   `SqlFormSessionRepository` (CAS on `version_token`) wired into the
   forms provider (the 501s are gone). Also fixed: `steps` arrays
   were dropped by the form-version mapping (silent single-step
   fallback) — they now round-trip; the start/save endpoints commit
   (previously returned 200 without persisting); `If-Match` is
   enforced in `save_step` (previously parsed and echoed only).
   Debt noted: `models_forms.py` is dead code (live form tables live
   in `models.py`); `form_definitions/versions/bindings` still lack
   migrations (tests run on `create_all`).
3. **FM-056** — `WebhookDeliveryDispatcher` (tick/drain mirroring
   `OutboxDispatcher`, per-tenant isolation, settings flags
   `webhook_dispatcher_enabled` / `webhook_dispatch_interval_seconds`)
   wired into the lifespan with shutdown cancellation. Also fixed:
   `DeliveryService.attempt` never checked subscription status
   despite promising to skip disabled subscriptions.
4. **FM-057** — `POST /api/v1/graphql` mounted: hand-rolled
   parse/execute over `ResolverContext` from the case bundle;
   mutations rejected at parse; root denials null the branch with an
   error entry (no leak); oversized `variables` rejected.

## Gated positions (unchanged, by design)

- **FM-029**: scan capture + quarantine mechanics operable; the
  physical scan device stays Gated.
- **FM-046**: signed batches + export operable; `audit.stream`
  ungranted until a SIEM target is selected (ADR-009).
- **FM-049**: idle/absolute/lockout checks operable;
  `SESSION_POLICY_PUBLISH` / `SESSION_REQUIRE_CERT` ungranted
  (ADR-011); session durability stays in-memory pending an
  IdP/session-store product decision.
- **FM-058**: synthetic connector lifecycle operable;
  `CONNECTOR_NAMED_IMPLEMENT` ungranted (Constitution V.10).
- **FM-059**: coexistence/migration state operable; real-time
  synchronization is explicitly not implied.

## Consequences

- OpenAPI grows to 123 paths; all dependency resolvers settle.
- 18 new tests (4 semantic router, 8 graphql router, 6 dispatcher
  — dispatcher slice, 4 form-session router + 4 SQL here).
