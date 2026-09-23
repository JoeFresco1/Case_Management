"""AI advisory namespace — application-layer use cases and types.

# INTEGRATION_SEAM: Azure OpenAI — local adapters (no-op stub) live in
# ``app.infrastructure.ai``; production adapters that call Azure
# OpenAI are not implemented in this slice. The local ``no-op`` adapter
# proves the deterministic core works without any LLM (FM-050
# acceptance).
"""

from __future__ import annotations

from app.application.ai.boundary import AIAdvisor, AIRecommendation, AIRequest
from app.application.ai.decision import HumanDecision
from app.application.ai.search_ranking import (
    RankedCandidate,
    RankingHints,
    RankingResult,
    SearchRanker,
)

__all__ = [
    "AIAdvisor",
    "AIRecommendation",
    "AIRequest",
    "HumanDecision",
    "RankedCandidate",
    "RankingHints",
    "RankingResult",
    "SearchRanker",
]
