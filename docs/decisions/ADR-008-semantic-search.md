# ADR-008: Semantic search — authorization-filtered, AI-ranked

- Status: Accepted (2026-09-02)
- Scope: `backend/app/application/ai/search_ranking.py`,
  `backend/app/application/search/semantic_search.py`,
  `backend/app/infrastructure/ai/noop_search_ranker.py`,
  `backend/app/infrastructure/ai/azure_search_ranker.py`,
  `backend/tests/search/test_semantic_search.py`, AGENTS.md §4.R11
  (AI bounded), §12 (seam inventory), Pillar P06 §4 P06-US7,
  FM-034.
- Supersedes: nothing.
- Related: Constitution XIV (deterministic core before AI), ADR-012
  (AI boundary), ADR-002 (integration seams pattern), Pillar P06 §2
  ("Explicitly not building — a search index that contains data the
  actor is not authorized to see, even temporarily").

## Context

P06-US7 ("As a case worker, I can run a semantic search and see
AI-ranked results with confidence labels; the result set is still
filtered to what I can see") and FM-034 ("Workers receive
authorization-filtered AI-ranked results with confidence and
provenance cues; deterministic authorization remains authoritative")
require a *bounded* AI capability on top of the deterministic
structured search. The pillar also forbids, explicitly, "a search
index that contains data the actor is not authorized to see, even
temporarily" (P06 §2). Without a deliberate ordering and a single
seam for the ranker, four failure modes are likely:

1. The ranker widens the candidate set. The AI returns ids the
   actor cannot see — either because the model misread the schema
   or because the caller skipped the structured filter. The
   audit row records the actor as having seen the case.
2. The ranker becomes the authorization decision. A reviewer reads
   "AI ranked first" as "AI authorized this", and the audit row
   carries the model id as the actor.
3. The ranker's failure takes the search down. A timeout, a
   throttled model, or a content-filter rejection on the AI side
   blocks the deterministic search that would otherwise have
   served the actor.
4. The recommendation and the human / deterministic authorization
   decision are conflated. An audit row that records both as the
   same event lets a careless reviewer treat them as one.

## Options

1. **Reuse `case_search.handle` and post-process its result.** The
   structured use case already authorizes and returns
   `SearchResultPage`. Wrapping it would write the audit row
   twice (once for the structured fallback path, once for the AI
   rank path) and would conflate two distinct events.
2. **Add a new `SearchProvider` that knows how to rank.** Mix the
   AI capability into the search-engine port. The provider
   surface grows; the boundary between "engine" and "ranker"
   disappears, and the AI capability becomes load-bearing for
   any search.
3. **A separate `SearchRanker` port called *after* a structured
   `SearchProvider` returns the authorized candidate set.** The
   structured provider is the authorization gate; the ranker
   reorders, never widens. The use case writes one audit row
   for the deterministic authorization and one log line for the
   AI recommendation. The deterministic core is unaffected when
   the ranker is degraded.

## Decision

Adopt option 3.

- `SearchRanker` (Protocol, application-layer port) lives in
  `app/application/ai/search_ranking.py`. The protocol exposes a
  single verb, `rank`, that takes a tuple of authorized
  candidate ids and a bounded `RankingHints` (tenant id, query
  text, candidate count, max results, actor id, correlation id)
  and returns a `RankingResult` (ordered candidates + model id +
  aggregate confidence + warnings).
- The local default `NoOpSearchRanker` lives in
  `app/infrastructure/ai/noop_search_ranker.py`. It preserves
  the input order with `confidence=0.0` and the
  `ai_ranker_disabled` warning, so the deterministic core works
  without an LLM (FM-050 acceptance).
- The production seam `AzureOpenAISearchRanker` lives in
  `app/infrastructure/ai/azure_search_ranker.py`. It is a
  `DependencyUnavailable`-raising placeholder today and documents
  the future redaction / cross-tenant refusal contract.
- `semantic_search.handle` performs the same authorization as
  `case_search.handle`, calls the structured `SearchProvider`,
  hands the surviving ids to the ranker, and reorders the
  structured hits by the ranker's preference. The ranker cannot
  widen the candidate set: any id it surfaces that was not in
  the structured provider's output is dropped (defense in depth).
- The recommendation is logged on its own structured-log line
  (`semantic_search.recommendation`) carrying the model id,
  confidence, warnings, authorized count, and result count. No
  audit event records the AI as the author of the search.
- The use case derives the human-context keys (tenant id, actor
  id, correlation id) from `SemanticSearchQuery` rather than
  reading structlog contextvars, so the application layer stays
  free of infrastructure imports (AGENTS.md §4.R10).

## Consequences

- The deterministic core is unaffected when the ranker is
  disabled. Tests inject `NoOpSearchRanker` and assert that
  result pages match the structured order; production with the
  Azure adapter down returns the same (audit rows carry the
  `ai_ranker_disabled` warning).
- A separate audit row is unnecessary: the structured log line
  carries the AI recommendation separately from the
  deterministic authorization decision. Reviewers join log and
  timeline by `correlation_id`.
- A misbehaving ranker cannot widen the candidate set. The use
  case discards any id the ranker tries to surface that was not
  authorized; this is the inverse of P06 §2's forbidden pattern.
- The seam is the same shape as the existing AI capability
  boundary (ADR-012): single verb (`rank`), bounded context
  (`RankingHints`), structured return (`RankingResult`), local
  no-op default, Azure placeholder.
- `SearchHit` is imported only by the application module (not
  the protocol module), so the protocol stays narrow.

## Seams

| Capability | Port | Local adapter | Production seam |
|---|---|---|---|
| AI search ranking | `app.application.ai.search_ranking.SearchRanker` | `app.infrastructure.ai.noop_search_ranker.NoOpSearchRanker` (preserves order, `confidence=0.0`, `ai_ranker_disabled` warning) | `app.infrastructure.ai.azure_search_ranker.AzureOpenAISearchRanker` (placeholder, raises `DependencyUnavailable`) |
| Structured search engine | `app.domain.ports.SearchProvider` (unchanged) | `app.infrastructure.persistence.search.SqlSearchProvider` (portable LIKE) | Azure Cognitive Search / OpenSearch adapter replacing the same Protocol |

Documented in `docs/integrations/seams.md` (per AGENTS.md §12).

## Acceptance

- `tests/search/test_semantic_search.py` covers the ranker
  receiving only the structured provider's authorized ids, the
  result page reflecting the ranker's ordering, the
  recommendation log line carrying model id + confidence +
  warnings + actor/tenant ids, the ranker not being consulted
  when authorization fails, and the no-op / Azure placeholder
  satisfying the Protocol structurally.
- Deterministic core still passes without the ranker:
  `NoOpSearchRanker` returns the input order with zero
  confidence; the structured fallback remains in effect.
- The AI does not appear as the actor in the audit / log
  pipeline: the recommendation is logged on its own line and
  the AI's model id is recorded as `ranking_model_id`, never as
  `actor_id`.

## Open

- Q1: When the Azure adapter ships, must `RankingHints.extra`
  surface to the model verbatim, or is the field restricted to
  the application layer's redaction list? Today the no-op ignores
  `extra`; the Azure placeholder will apply the existing
  redaction list from `app.infrastructure.telemetry.structured_logging`.
- Q2: When a ranker returns duplicate ids (same id twice in the
  ordered list), is that a contract violation worth pinning in
  tests? Today `_reorder` discards duplicates silently. A future
  test could assert the ranker's deduplication contract.