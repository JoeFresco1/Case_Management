"""No-op search ranker — the local default (P06-US7 / FM-034).

# Why this exists

FM-050 acceptance: the deterministic core passes without LLM. The
walking skeleton must run, tests must pass, and demos must work without
any external AI dependency. ``NoOpSearchRanker`` is the local adapter
that implements ``SearchRanker`` without any I/O.

# Why the no-op preserves the candidate order

A naive no-op that returns an empty ranking would force every caller
to special-case "AI was disabled" and silently drop otherwise-valid
results. The deterministic alternative is to return the candidates in
their *original* order with ``confidence=0.0``. Callers can:

- Detect "no AI answer" by ``confidence == 0.0``.
- Still serve a fully-authorized result page when the AI is down
  (the use case falls back to the structured order).

# Why the no-op is deterministic

The same ``RankingHints`` + ``candidate_ids`` tuple always produces
the same ``RankingResult``. There is no time, no random, no
environment read; tests can pin exact equality without ``freezegun``.
"""

from __future__ import annotations

from uuid import UUID

from app.application.ai.search_ranking import (
    RankedCandidate,
    RankingHints,
    RankingResult,
)

# The model id reported in audit rows when the no-op ranker is in
# place. Auditors and on-call engineers need a stable token to grep
# for; "noop" is the conventional choice across the codebase and
# matches ``NOOP_MODEL_ID`` in ``app.infrastructure.ai.noop_stub``.
NOOP_RANKER_MODEL_ID: str = "noop"


class NoOpSearchRanker:
    """Local default ``SearchRanker`` that preserves the input order.

    The adapter satisfies the ``SearchRanker`` Protocol structurally.
    It performs no I/O, has no environment dependencies, and is safe
    to instantiate at import time. Use it in tests, in CI, and as
    the default in ``app.config`` when ``Settings.search_ranker`` is
    unset.

    Failure behavior:

    - An empty ``candidate_ids`` returns an empty ``RankingResult``
      with the standard ``ai_ranker_disabled`` warning so callers
      can detect "nothing to rank" deterministically.
    - A malformed hint (e.g. negative ``max_results``) is ignored —
      the no-op does not branch on hint shape; the structured
      provider has already validated the actor-visible inputs.
    """

    async def rank(
        self,
        *,
        candidate_ids: tuple[UUID, ...],
        hints: RankingHints,
    ) -> RankingResult:
        # Preserve the input order verbatim. The use case treats
        # this as "AI has no preference" and falls back to the
        # structured search order. The candidate's ``confidence``
        # is zero so audit / UI can detect "no AI answer".
        candidates = tuple(
            RankedCandidate(case_id=cid, confidence=0.0, rank=idx)
            for idx, cid in enumerate(candidate_ids)
        )
        return RankingResult(
            candidates=candidates,
            model_id=NOOP_RANKER_MODEL_ID,
            confidence=0.0,
            warnings=("ai_ranker_disabled",),
            rationale="",
        )


__all__ = ["NOOP_RANKER_MODEL_ID", "NoOpSearchRanker"]
