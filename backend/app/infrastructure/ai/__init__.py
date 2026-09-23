"""Infrastructure AI namespace.

# INTEGRATION_SEAM: Azure OpenAI — local default is ``NoOpAIAdvisor``;
# production adapter is ``AzureOpenAIAdvisor`` (placeholder until
# Azure connectivity is available; see ``azure_openai.py``).
#
# The AI search-ranking port follows the same seam pattern: local
# default ``NoOpSearchRanker``, production placeholder
# ``AzureOpenAISearchRanker`` (see ``noop_search_ranker.py`` and
# ``azure_search_ranker.py``).
"""

from __future__ import annotations

from app.infrastructure.ai.azure_openai import (
    DEFAULT_AZURE_OPENAI_MODEL_ID,
    AzureOpenAIAdvisor,
)
from app.infrastructure.ai.azure_search_ranker import (
    DEFAULT_AZURE_OPENAI_RANKER_MODEL_ID,
    AzureOpenAISearchRanker,
)
from app.infrastructure.ai.noop_search_ranker import (
    NOOP_RANKER_MODEL_ID,
    NoOpSearchRanker,
)
from app.infrastructure.ai.noop_stub import NOOP_MODEL_ID, NoOpAIAdvisor

__all__ = [
    "DEFAULT_AZURE_OPENAI_MODEL_ID",
    "DEFAULT_AZURE_OPENAI_RANKER_MODEL_ID",
    "NOOP_MODEL_ID",
    "NOOP_RANKER_MODEL_ID",
    "AzureOpenAIAdvisor",
    "AzureOpenAISearchRanker",
    "NoOpAIAdvisor",
    "NoOpSearchRanker",
]
