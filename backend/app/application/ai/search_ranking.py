"""AI search-ranking capability port (P06-US7 / FM-034).

# Why this Protocol lives in the application layer

P06-US7 ("semantic search with AI-ranked results and confidence labels;
result set still filtered to what I can see") and FM-034 ("Workers
receive authorization-filtered AI-ranked results with confidence and
provenance cues; deterministic authorization remains authoritative")
require a *bounded* AI capability that:

- **Cannot authorize**. The Protocol exposes ``rank`` only. An adapter
  that exposes ``authorize_rank`` or ``approve_results`` is suspect at
  review time and should be challenged against FM-050 / Constitution XIV.
- **Cannot widen the result set**. The use case (``semantic_search``)
  filters the candidate set to authorized cases *before* handing it to
  the ranker; the ranker reorders, it never adds ids.
- **Carries confidence + provenance**. Every recommendation includes
  ``confidence`` (bounded ``[0.0, 1.0]``) and ``source_refs`` so
  auditors can verify what the AI saw.

# Why this is a Protocol, not an ABC

The application layer depends only on the shape, never on the
implementation. The local default (``NoOpSearchRanker``) and the
production adapter (``AzureOpenAISearchRanker`` placeholder) live in
``app.infrastructure.ai`` and are wired via ``app.config`` /
``app.deps``. Tests inject fakes without subclassing.

# Why the Protocol lives here, not in ``app.domain.ports``

Search ranking is an application-layer concern — the domain is
unaware of the AI capability and works without it. ``CaseSearchQuery``
is the domain-shaped request; ``RankingHints`` is the AI-shaped
context that the use case builds from the actor / query before calling
``rank``.

# INTEGRATION_SEAM: Azure OpenAI

The Azure adapter placeholder lives in
``app.infrastructure.ai.azure_search_ranker``. The seam is documented
in ``docs/integrations/seams.md``; the deterministic core never depends
on it (FM-050 acceptance).
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RankingHints:
    """Bounded, evidence-shaped context passed to the AI ranker.

    Why the use case constructs this from ``CaseSearchQuery`` and never
    lets the caller dictate it: the seam has to enforce the redaction
    contract (no PHI in the AI request) and the tenant scope (the AI
    can only see the actor's tenant). A caller who needs additional
    context can extend this dataclass; an adapter that reads fields
    outside this dataclass is suspect at review time.

    Attributes:
        tenant_id: The tenant scope of the ranking. The AI adapter MUST
            not retrieve across tenants; this id is recorded in the
            audit row's ``tenant_id``.
        query_text: The actor's free-text query, already validated by
            the application layer (P06-US1). Carried as the only signal
            the AI sees; the candidate set is the rest of the context.
        candidate_count: How many candidates the use case is handing
            the AI. Recorded in the audit row so a reviewer can see the
            pre-rank pool size.
        max_results: The cap on returned results (mirror of the actor's
            ``limit``). The ranker MUST NOT return more than this.
        actor_id: The human actor who initiated the search. Recorded
            in the audit row's ``actor_id``; the ranker never acts on
            their behalf.
        correlation_id: The request's correlation id so the ranking
            recommendation can be located next to the human's
            authorization decision in the timeline.
    """

    tenant_id: UUID
    query_text: str
    candidate_count: int
    max_results: int
    actor_id: UUID
    correlation_id: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """A single AI-ranked candidate.

    Why the ranker returns id + confidence + provenance + rationale:
    P11-US12 ("AI suggestions cannot authorize") and FM-034
    ("confidence and provenance cues") require downstream code to be
    able to *distinguish* AI-suggested orderings from human decisions
    and to surface the AI's provenance in the audit timeline.

    Attributes:
        case_id: The case id being ranked. MUST be one of the ids the
            use case handed to the ranker; an id outside that set is
            a contract violation and the use case discards it.
        confidence: A float in ``[0.0, 1.0]``. Adapters that cannot
            score their confidence MUST return ``0.0`` rather than
            invent a number; downstream UI may choose to suppress low-
            confidence suggestions entirely (out of scope here).
        source_refs: References (ids + brief labels) the AI used when
            ranking this candidate. Empty tuple is acceptable; the
            ``NoOpSearchRanker`` always returns empty. Free-text
            "explanations" without a reference are not allowed —
            auditors must be able to verify provenance.
        rationale: A short structured rationale. Free text is
            permitted but MUST NOT contain content the AI fabricated;
            well-behaved adapters cite ``source_refs`` inline.
        rank: The AI's 0-based ordering. Lower is better. The use
            case preserves this order when assembling the result page
            but is free to interleave with non-AI sources (none today).
    """

    case_id: UUID
    confidence: float
    source_refs: tuple[tuple[str, str], ...] = ()
    rationale: str = ""
    rank: int = 0


@dataclass(frozen=True, slots=True)
class RankingResult:
    """The AI ranker's ordered output.

    Why this carries the model id and warnings on the result, not on
    each candidate: audit rows are written once per ranking call, not
    once per candidate. ``model_id`` is the stable identifier of the
    adapter that produced the order; ``warnings`` are adapter-emitted
    warnings ("context truncated", "schema version unknown", etc.).

    Attributes:
        candidates: AI-ranked candidate ids in AI-preferred order.
            The use case reorders its pre-filtered set by matching
            against this list; ids the AI did not see are dropped
            (they would have been unauthorized anyway).
        model_id: Stable identifier of the adapter (``"noop"`` for the
            local default, ``"azure-openai:<deployment>"`` for the
            production seam). Always present so the audit row records
            *which* AI produced the answer.
        confidence: Aggregate confidence in ``[0.0, 1.0]``. The use
            case emits this on the audit log row; downstream code can
            choose to suppress results when ``confidence == 0.0``.
        warnings: Adapter-emitted warnings carried into the audit
            row's payload so reviewers can judge the answer.
        rationale: Short free-text rationale for the ordering.
            Optional; the no-op leaves it empty.
    """

    candidates: tuple[RankedCandidate, ...]
    model_id: str
    confidence: float
    warnings: tuple[str, ...] = ()
    rationale: str = ""


@runtime_checkable
class SearchRanker(Protocol):
    """The AI search-ranking port.

    The protocol exposes a single verb (``rank``). An adapter that
    defines any additional verb — especially anything shaped like an
    authorization — is suspect at code-review time and should be
    challenged against FM-050 / Constitution XIV. Naming enforces the
    boundary; reviewers can grep for any callable on the AI port and
    find ``rank`` and nothing else.

    The protocol is ``@runtime_checkable`` so tests can assert
    ``isinstance(ranker, SearchRanker)`` against in-memory fakes
    without subclassing.
    """

    @abstractmethod
    async def rank(
        self,
        *,
        candidate_ids: tuple[UUID, ...],
        hints: RankingHints,
    ) -> RankingResult:
        """Reorder ``candidate_ids`` by AI-preferred order.

        Implementations MUST:

        - Never add ids that were not in ``candidate_ids``. The use
          case pre-filters the candidate set by authorization; the
          ranker exists to reorder, not to widen.
        - Never raise on missing context. Return a
          ``RankingResult`` with ``confidence=0.0`` and a warning
          instead. The use case is on the human search hot path and
          must not fail because the AI layer is degraded.
        - Never block domain writes. The ranking is *displayed* to a
          human reviewer; the human's query is what the audit log
          records as the authoritative action.
        - Redact PHI before any external call. The local no-op
          adapter performs no I/O; the Azure OpenAI placeholder
          documents the redaction contract but does not implement it.

        Args:
            candidate_ids: The authorized candidate case ids, in the
                order the structured search returned them. The ranker
                MAY consume context not in this tuple by id only.
            hints: Bounded context (tenant id, query text, cap on
                results). The ranker MUST NOT widen the candidate set
                beyond ``candidate_ids``.

        Returns:
            A ``RankingResult``. Never ``None``. Callers that need
            to detect "no AI answer" compare ``confidence == 0.0``
            and ``len(candidates) == 0``.

        Raises:
            Never. Implementations swallow recoverable errors and
            return a zero-confidence ranking with a warning.
        """


__all__ = [
    "RankedCandidate",
    "RankingHints",
    "RankingResult",
    "SearchRanker",
]
