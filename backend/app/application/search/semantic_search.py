"""Authorized, AI-ranked (semantic) case search (P06-US7 / FM-034).

# Why this is a separate use case from ``case_search``

P06-US7 ("semantic search with AI-ranked results and confidence
labels; result set still filtered to what I can see") and FM-034
("authorization-filtered AI-ranked results with confidence and
provenance cues; deterministic authorization remains authoritative")
require a *two-stage* pipeline that the structured search use case
does not model:

1. **Authorize and filter first.** The actor's tenant, role, and
   sensitive-read grant gate which cases the AI is allowed to see.
   The AI never widens the candidate set; it only reorders what the
   deterministic core already approved. This is the inverse of the
   failure mode the pillar explicitly forbids: "a search index that
   contains data the actor is not authorized to see, even
   temporarily" (P06 §2).
2. **AI ranks the survivors.** A separate ``SearchRanker`` port
   (P06-US7 / FM-034 / AGENTS.md §4.R11) reorders the survivors
   by AI preference and attaches confidence + provenance so
   auditors can verify what the AI saw.

# Why ``semantic_search`` does not call ``case_search.handle`` directly

The structured search use case does its own authorization and audit
writing; this use case needs the *raw* authorized candidate set
*before* any audit write. Calling it would force the audit row to
be written twice (once for the structured fallback, once for the AI
ranked result). Instead, ``semantic_search.handle`` performs the same
authorization checks the structured search does, calls the same
``SearchProvider`` to get a candidate set, and writes the audit row
exactly once with both the AI recommendation and the final ordering.

# Why the recommendation is logged separately from the decision

Constitution XIV and ADR-012 are explicit: AI recommendation and
human / deterministic authorization decision live in distinct audit
events. This use case writes the *recommendation* (model id,
confidence, source refs, candidate count) to the structured log and
returns a deterministic, authorization-gated result page. No audit
event records the AI as the author of the search.

# INTEGRATION_SEAM: search engine + AI search ranking

``SearchProvider`` is a domain port (``app/domain/ports.py``); the
local adapter is ``app.infrastructure.persistence.search`` (portable
LIKE). ``SearchRanker`` is an application-layer port
(``app/application/ai/search_ranking.py``); the local adapter is
``app.infrastructure.ai.noop_search_ranker`` (zero-confidence,
order-preserving) and the production seam is
``app.infrastructure.ai.azure_search_ranker`` (placeholder). An Azure
Cognitive Search adapter replacing ``SearchProvider`` does not change
this use case.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import structlog

from app.application.ai.search_ranking import (
    RankingHints,
    RankingResult,
    SearchRanker,
)
from app.application.authorization import Action, Resource, authorize
from app.application.cases.view_case import actor_can_see_sensitive
from app.application.search.case_search import (
    CORE_FIELDS,
    SUPPORTED_OPS,
    _ActorProxy,
    _reject_non_searchable_filters,
    _validate_filters,
)
from app.domain.errors import ValidationFailed
from app.domain.ports import (
    SearchFilter,
    SearchHit,
    SearchProvider,
    SearchResultPage,
    SearchSpec,
)

_LOGGER = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SemanticSearchQuery:
    """The application-layer shape for a semantic (AI-ranked) search.

    The fields mirror :class:`app.application.search.case_search.CaseSearchQuery`
    plus the new ``ranking`` boolean the router uses to opt into AI
    ranking. ``searchable_field_keys`` and ``sensitive_field_keys``
    are pre-resolved by the router from the actor's published
    CaseTypeVersions (P05 / P06); the use case consumes them as
    sealed inputs.

    Attributes:
        tenant_id: Tenant scope of the request.
        actor_id: Human actor who initiated the search.
        actor_roles: Actor's role set used to authorize the search.
        correlation_id: Carried into the audit row and the ranking
            hint so the AI recommendation can be located next to the
            result page in the timeline.
        text_query: Free-text portion matched across ``title`` and
            declared searchable ``data`` fields.
        filters: Tuple of ``(field, op, value)`` triples; the same
            controlled vocabulary as the structured search use case.
        limit: Page size; clamped to ``[1, 200]``.
        offset: Pagination offset; non-negative.
        searchable_field_keys: Union of ``searchable=true`` keys
            across the tenant's published CaseTypeVersions.
        sensitive_field_keys: Union of restricted-or-above keys; used
            to project redacted snippets via ``can_see_sensitive``.
        ranking: When ``True``, the use case asks the
            ``SearchRanker`` to reorder the authorized candidate
            set. When ``False``, the use case is a structured
            search that simply attaches a zero-confidence
            recommendation to the audit row.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    correlation_id: str
    text_query: str | None = None
    filters: tuple[tuple[str, str, object], ...] = ()
    limit: int = 50
    offset: int = 0
    searchable_field_keys: tuple[str, ...] = ()
    sensitive_field_keys: tuple[str, ...] = ()
    ranking: bool = True


async def handle(
    query: SemanticSearchQuery,
    *,
    search_provider: SearchProvider,
    ranker: SearchRanker,
) -> SearchResultPage:
    """Run an authorized, optionally AI-ranked case search.

    The pipeline is fixed:

    1. Validate filters and pagination (mirrors
       :func:`app.application.search.case_search.handle`).
    2. Authorize ``CASE_READ`` against the actor. Cross-tenant
       actors are denied unconditionally.
    3. Compute the actor's ``can_see_sensitive`` grant and hand it
       to the structured ``SearchProvider`` so redacted snippets
       never leak restricted values.
    4. Receive the authorized candidate set.
    5. If ``query.ranking`` is ``True``, ask ``ranker`` to reorder
       the candidates. The ranker cannot widen the candidate set;
       any id the ranker surfaces that was not in the authorized
       set is discarded.
    6. Emit the AI recommendation to the structured log so the
       audit timeline carries the model id, confidence, source
       refs, and candidate count — separate from any
       authorization decision.
    7. Return the (possibly reordered) result page.

    Failure behavior:

    - Authorization failure raises ``Forbidden`` (mapped to 403).
    - Unknown operator / non-searchable field raises
      ``ValidationFailed`` (mapped to 422 / 400).
    - ``limit`` / ``offset`` are clamped to the contract
      (``[1, 200]`` / ``[0, ...)``).
    - The ranker is never allowed to raise. Adapters that raise are
      a contract violation and tests pin this with a separate
      assertion; production callers must wrap the ranker in a
      try/except that downgrades to the structured order (out of
      scope here).
    """
    _validate_filters(query.filters)
    _reject_non_searchable_filters(query.filters, query.searchable_field_keys)
    _clamp_pagination_semantic(query)

    actor_ctx = _ActorProxy(
        tenant_id=query.tenant_id,
        user_id=query.actor_id,
        roles=query.actor_roles,
    )
    # 1. Tenant + role gate. Deny-by-default; cross-tenant actors
    # are denied here even if the structured provider were
    # misconfigured to leak rows.
    await authorize(
        Action.CASE_READ,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=query.tenant_id, kind="case", id=None),
    )

    # 2. Compute the sensitive-read grant once. The policy is
    # tenant-scoped; ``case_id`` is not consulted, so a fresh
    # placeholder UUID satisfies the typed signature.
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

    # 4. The structured provider is the authorization gate. Its
    # SQL adapter includes ``WHERE tenant_id = :tenant_id`` and
    # projects redacted snippets — that contract is the line in
    # the sand: nothing past this point may add case ids.
    structured: SearchResultPage = await search_provider.search(spec)
    authorized_ids: tuple[UUID, ...] = tuple(h.case_id for h in structured.items)

    # 5. AI ranking is bounded by the authorized candidate set.
    # When ``query.ranking`` is False the use case still logs a
    # zero-confidence recommendation so the audit row is uniform
    # across both modes.
    ranking_result: RankingResult = await ranker.rank(
        candidate_ids=authorized_ids,
        hints=RankingHints(
            tenant_id=query.tenant_id,
            query_text=query.text_query or "",
            candidate_count=len(authorized_ids),
            max_results=query.limit,
            actor_id=query.actor_id,
            correlation_id=query.correlation_id,
        ),
    )

    # 6. Reorder the structured hits by AI preference, dropping
    # any id the ranker tried to add (defense in depth — see
    # ADR-008 / FM-034 / Pillar P06 §2 "Explicitly not building").
    final_hits: list[SearchHit] = _reorder(structured.items, ranking_result)

    # 7. Emit the AI recommendation to the structured log
    # **separately** from any authorization decision. The audit
    # row carries the model id, confidence, source refs, and
    # candidate count so reviewers can verify provenance. The
    # human-context keys come from the query, not from structlog
    # contextvars, so the application layer stays free of
    # infrastructure imports (AGENTS.md §4.R10).
    _log_recommendation(
        ranking_result=ranking_result,
        authorized_count=len(authorized_ids),
        result_count=len(final_hits),
        tenant_id=query.tenant_id,
        actor_id=query.actor_id,
        correlation_id=query.correlation_id,
    )

    # The cursor reflects the *final* result page, not the
    # pre-rank page, because pagination is over the AI-ordered
    # set.
    return SearchResultPage(
        items=final_hits,
        total_considered=structured.total_considered,
        limit=structured.limit,
        offset=structured.offset,
        next_cursor=structured.next_cursor,
    )


def _reorder(
    structured_hits: list[SearchHit],
    ranking: RankingResult,
) -> list[SearchHit]:
    """Reorder ``structured_hits`` by the ranker's preference.

    The ranker is forbidden from widening the candidate set; any
    id it surfaces that was not in the structured provider's
    output is dropped (it would have been unauthorized anyway,
    but defense in depth — the use case does not trust the
    ranker to never accidentally surface an out-of-scope id).

    Ids the ranker did not see preserve their structured order
    after the AI-ordered prefix. This is the documented fallback:
    a degraded ranker that returned an empty ranking still serves
    a deterministic, authorization-gated result page.
    """
    by_id: dict[UUID, SearchHit] = {h.case_id: h for h in structured_hits}
    seen: set[UUID] = set()
    ordered: list[SearchHit] = []
    for cand in ranking.candidates:
        hit = by_id.get(cand.case_id)
        if hit is None or cand.case_id in seen:
            # Out-of-scope or duplicated: drop. The use case does
            # not trust the ranker to widen the candidate set.
            continue
        ordered.append(hit)
        seen.add(cand.case_id)
    # Tail: any structured hit the ranker did not surface
    # (empty ranking, etc.) keeps its original position so the
    # fallback is deterministic.
    for hit in structured_hits:
        if hit.case_id not in seen:
            ordered.append(hit)
            seen.add(hit.case_id)
    return ordered


def _log_recommendation(
    *,
    ranking_result: RankingResult,
    authorized_count: int,
    result_count: int,
    tenant_id: UUID,
    actor_id: UUID,
    correlation_id: str,
) -> None:
    """Emit the AI recommendation to the structured log.

    The log line is the audit signal that an AI was consulted for
    the search. It deliberately carries:

    - ``ranking.model_id`` — which adapter ran (auditors grep for
      this).
    - ``ranking.confidence`` — the ranker's aggregate confidence.
      When ``0.0``, the audit row proves the AI was disabled and
      the result page fell back to the structured order.
    - ``ranking.warnings`` — adapter-emitted warnings (context
      truncated, schema version unknown, ``ai_ranker_disabled``).
    - ``authorized_count`` — the pre-rank pool size.
    - ``result_count`` — the post-rank result size; divergence
      with ``authorized_count`` proves the AI dropped some hits.
    - ``tenant_id`` / ``actor_id`` / ``correlation_id`` — the
      human context the recommendation was generated under. These
      are taken from the ``SemanticSearchQuery``; the application
      layer does not read structlog contextvars so the layer rule
      (AGENTS.md §4.R10) stays intact. The HTTP middleware binds
      these keys on the request logger via the standard seam.

    Why a separate log line, not an audit event: the audit
    timeline for case creation / transition is the
    :class:`app.domain.audit` chain; search activity is logged
    through the structured logger so reviewers can join log and
    timeline by ``correlation_id``. Adding an audit event here
    would conflate AI recommendation with deterministic
    authorization decision (ADR-012).
    """
    _LOGGER.info(
        "semantic_search.recommendation",
        ranking_model_id=ranking_result.model_id,
        ranking_confidence=ranking_result.confidence,
        ranking_warnings=list(ranking_result.warnings),
        ranking_candidate_count=len(ranking_result.candidates),
        authorized_count=authorized_count,
        result_count=result_count,
        tenant_id=str(tenant_id),
        actor_id=str(actor_id),
        correlation_id=correlation_id,
    )


def _clamp_pagination_semantic(query: SemanticSearchQuery) -> None:
    """Clamp ``limit`` / ``offset`` to the contract.

    Mirrors :func:`app.application.search.case_search._clamp_pagination`
    so the semantic and structured use cases share the same
    pagination contract. Anything outside ``[1, 200]`` / ``[0, ...)``
    is a client bug, surfaced as ``ValidationFailed``.
    """
    if query.limit < 1 or query.limit > 200:
        raise ValidationFailed("limit must be in [1, 200]", field="limit")
    if query.offset < 0:
        raise ValidationFailed("offset must be >= 0", field="offset")


# Re-export the controlled vocabulary so callers that import only
# from ``semantic_search`` see the same constants as ``case_search``.
__all__ = [
    "CORE_FIELDS",
    "SUPPORTED_OPS",
    "SemanticSearchQuery",
    "handle",
]
