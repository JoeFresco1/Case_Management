"""Azure OpenAI placeholder adapter — not wired in this slice.

# INTEGRATION_SEAM: Azure OpenAI — this module is a placeholder for the
# production AI adapter. It deliberately does not import ``openai`` or
# any Azure SDK; the package is reserved here so the seam location is
# discoverable. The Azure adapter will:

- Use ``azure.identity.DefaultAzureCredential`` to fetch an
  ``azure-ai-inference`` or ``openai`` client for the configured
  deployment.
- Apply the redaction list from
  ``app.infrastructure.telemetry.structured_logging`` to ``request.context``
  *before* the model call, and refuse to send any field on the
  deny-list.
- Map the model response back to ``AIRecommendation`` with a stable
  ``model_id`` recorded for audit.
- Degrade to a zero-confidence ``AIRecommendation`` on any
  recoverable error (timeout, throttling, content-filter rejection).

The deterministic core never depends on this adapter — see the
no-op stub at ``app.infrastructure.ai.noop_stub`` for the local
default and FM-050 acceptance ("deterministic core passes without
LLM").

# Why a real class is provided instead of just a docstring

A pure docstring would let the seam drift: someone could import this
module and assume the AI was real. ``AzureOpenAIAdvisor`` raises
``NotImplementedError`` on ``recommend`` so the seam is *typed* and
*runtime-checkable* without lying about being implemented. The class
also exposes the future configuration surface (deployment name,
endpoint, content-filter policy) so the adapter's contract is
visible today.
"""

from __future__ import annotations

from app.application.ai.boundary import AIAdvisor, AIRecommendation, AIRequest
from app.domain.errors import DependencyUnavailable

# The model id reported in audit rows when (eventually) the Azure
# adapter is in use. Stable across runs so on-call can grep for it.
DEFAULT_AZURE_OPENAI_MODEL_ID: str = "azure-openai:unconfigured"


class AzureOpenAIAdvisor:
    """Placeholder for the Azure OpenAI-backed ``AIAdvisor``.

    Construction requires the deployment endpoint and the content-
    filter policy name. ``recommend`` raises ``DependencyUnavailable``
    because the slice ships no Azure adapter; the deterministic core
    continues to use ``NoOpAIAdvisor``.

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

    async def recommend(self, request: AIRequest) -> AIRecommendation:
        # Why raise, not return empty: an adapter that silently
        # returned empty would mask a misconfiguration. The exception
        # is caught by ``AIAdvisor`` callers that must degrade
        # gracefully — see ``NoOpAIAdvisor`` for the safe default.
        raise DependencyUnavailable(
            "AzureOpenAIAdvisor is a placeholder seam; the no-op stub is the "
            "active adapter in this environment. Configure settings.ai_advisor "
            "and implement the Azure adapter before enabling."
        )

    @property
    def model_id(self) -> str:
        """Stable identifier for audit rows.

        Includes the deployment name when configured so audit logs can
        distinguish deployments in the same environment.
        """
        if self._deployment:
            return f"azure-openai:{self._deployment}"
        return DEFAULT_AZURE_OPENAI_MODEL_ID


# Verify structural conformance to the Protocol. This is a
# startup-time assertion: if the AI port gains a new method, an
# implementer who updates one adapter without the other fails fast
# here rather than at first call.
assert isinstance(
    AzureOpenAIAdvisor(
        endpoint="",
        deployment="",
        content_filter_policy="",
        api_version="",
    ),
    AIAdvisor,
)  # noqa: S101 - intentional seam conformance check


__all__ = ["DEFAULT_AZURE_OPENAI_MODEL_ID", "AzureOpenAIAdvisor"]
