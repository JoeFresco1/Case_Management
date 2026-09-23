"""Azure OpenAI search-ranker placeholder — not wired in this slice.

# INTEGRATION_SEAM: Azure OpenAI — this module is a placeholder for the
# production AI search-ranking adapter. It deliberately does not import
# ``openai`` or any Azure SDK; the package is reserved here so the seam
# location is discoverable. The Azure adapter will:

- Use ``azure.identity.DefaultAzureCredential`` to fetch an
  ``azure-ai-inference`` or ``openai`` client for the configured
  deployment.
- Apply the redaction list from
  ``app.infrastructure.telemetry.structured_logging`` to ``hints``
  *before* the model call, and refuse to send any field on the
  deny-list. The ranker MUST NOT widen the candidate set beyond
  ``candidate_ids``.
- Map the model response back to ``RankingResult`` with a stable
  ``model_id`` recorded for audit.
- Degrade to a zero-confidence ``RankingResult`` on any recoverable
  error (timeout, throttling, content-filter rejection), preserving
  the input order so the structured fallback can still serve.

The deterministic core never depends on this adapter — see the
no-op ranker at ``app.infrastructure.ai.noop_search_ranker`` for the
local default and FM-050 acceptance ("deterministic core passes
without LLM").

# Why a real class is provided instead of just a docstring

A pure docstring would let the seam drift: someone could import this
module and assume the ranker was real. ``AzureOpenAISearchRanker``
raises ``NotImplementedError`` on ``rank`` so the seam is *typed*
and *runtime-checkable* without lying about being implemented. The
class also exposes the future configuration surface (deployment name,
endpoint, content-filter policy) so the adapter's contract is
visible today.
"""

from __future__ import annotations

from uuid import UUID

from app.application.ai.search_ranking import (
    RankingHints,
    RankingResult,
    SearchRanker,
)
from app.domain.errors import DependencyUnavailable

# The model id reported in audit rows when (eventually) the Azure
# adapter is in use. Stable across runs so on-call can grep for it.
DEFAULT_AZURE_OPENAI_RANKER_MODEL_ID: str = "azure-openai:unconfigured"


class AzureOpenAISearchRanker:
    """Placeholder for the Azure OpenAI-backed ``SearchRanker``.

    Construction requires the deployment endpoint and the content-
    filter policy name. ``rank`` raises ``DependencyUnavailable``
    because the slice ships no Azure adapter; the deterministic core
    continues to use ``NoOpSearchRanker``.

    Args:
        endpoint: The Azure OpenAI resource endpoint (e.g.
            ``https://<resource>.openai.azure.com``).
        deployment: The deployment name (e.g. ``gpt-4o-mini``).
        content_filter_policy: The Azure content-filter policy name.
        api_version: The Azure OpenAI API version string.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str,
        content_filter_policy: str,
        api_version: str,
    ) -> None:
        self._endpoint = endpoint
        self._deployment = deployment
        self._content_filter_policy = content_filter_policy
        self._api_version = api_version

    async def rank(
        self,
        *,
        candidate_ids: tuple[UUID, ...],
        hints: RankingHints,
    ) -> RankingResult:
        # Why raise, not return zero-confidence: an adapter that
        # silently returned zero would mask a misconfiguration. The
        # exception is caught by ``SearchRanker`` callers that must
        # degrade gracefully — see ``NoOpSearchRanker`` for the safe
        # default. The candidate order is intentionally NOT echoed
        # here because a "looks like it ran" return would let an
        # operator believe the AI produced real rankings.
        _ = (candidate_ids, hints)
        raise DependencyUnavailable(
            "AzureOpenAISearchRanker is a placeholder seam; the no-op "
            "ranker is the active adapter in this environment. Configure "
            "settings.search_ranker and implement the Azure adapter "
            "before enabling."
        )

    @property
    def model_id(self) -> str:
        """Stable identifier for audit rows.

        Includes the deployment name when configured so audit logs can
        distinguish deployments in the same environment.
        """
        if self._deployment:
            return f"azure-openai:{self._deployment}"
        return DEFAULT_AZURE_OPENAI_RANKER_MODEL_ID


# Verify structural conformance to the Protocol. This is a
# startup-time assertion: if the AI port gains a new method, an
# implementer who updates one adapter without the other fails fast
# here rather than at first call.
assert isinstance(
    AzureOpenAISearchRanker(
        endpoint="",
        deployment="",
        content_filter_policy="",
        api_version="",
    ),
    SearchRanker,
)  # noqa: S101 - intentional seam conformance check


__all__ = ["DEFAULT_AZURE_OPENAI_RANKER_MODEL_ID", "AzureOpenAISearchRanker"]
