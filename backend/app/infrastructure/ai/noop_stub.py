"""No-op AI advisor adapter — the local default.

# Why this exists

FM-050 (P11-US12) acceptance: "deterministic core passes without LLM."
The walking skeleton must run, tests must pass, and demos must work
without any external AI dependency. ``NoOpAIAdvisor`` is the local
adapter that implements ``AIAdvisor`` without any I/O.

# Why the no-op returns a zero-confidence empty recommendation
# instead of a fabricated one

A recommendation with non-zero confidence is *evidence-shaped*: a
reviewer who sees ``confidence=0.42`` on a no-op answer will treat it
as a real suggestion and may defer to it. The no-op returns
``confidence=0.0`` and an empty ``summary`` so any code that consults
the recommendation (UI, audit, downstream rules) can detect "no AI
answer" deterministically and skip the AI branch.

# Determinism

The same ``AIRequest`` always produces the same ``AIRecommendation``.
There is no time, no random, no environment read; tests can pin
exact equality without ``freezegun``.
"""

from __future__ import annotations

from app.application.ai.boundary import AIRecommendation, AIRequest

# The model id reported in audit rows when the no-op adapter is in
# place. Auditors and on-call engineers need a stable token to grep
# for; "noop" is the conventional choice across the codebase.
NOOP_MODEL_ID: str = "noop"


class NoOpAIAdvisor:
    """Local AI advisor that returns zero-confidence empty recommendations.

    The adapter satisfies the ``AIAdvisor`` Protocol structurally. It
    performs no I/O, has no environment dependencies, and is safe to
    instantiate at import time. Use it in tests, in CI, and as the
    default in ``app.config`` when ``Settings.ai_advisor`` is unset.
    """

    async def recommend(self, request: AIRequest) -> AIRecommendation:
        # We deliberately do NOT branch on ``request.capability``: the
        # no-op never invents a recommendation. A real adapter would
        # route by capability; the no-op exists to prove the
        # deterministic core works regardless of capability.
        return AIRecommendation(
            capability=request.capability,
            summary="",
            confidence=0.0,
            source_refs=(),
            rationale="",
            model_id=NOOP_MODEL_ID,
            warnings=("ai_advisor_disabled",),
        )


__all__ = ["NOOP_MODEL_ID", "NoOpAIAdvisor"]
