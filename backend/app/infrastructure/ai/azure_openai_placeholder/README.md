# INTEGRATION_SEAM: Azure OpenAI — placeholder for the production AI-advisor adapter.

This directory reserves the location for the Azure OpenAI implementation of
the `AIAdvisor` Protocol declared in `app/application/ai/boundary.py`.

## Status

- **Local adapter implemented**: `NoOpAIAdvisor`
  (`app/infrastructure/ai/noop_stub.py`) — zero-confidence empty
  recommendation, `model_id="noop"`, deterministic, no I/O. The
  deterministic core passes without LLM (FM-050 acceptance).
- **Azure adapter pending (placeholder present)**: an
  `AzureOpenAIAdvisor` stub exists at
  `app/infrastructure/ai/azure_openai.py` and raises `DependencyUnavailable`
  on every call. This directory reserves the location for the
  **production-ready** Azure OpenAI adapter when the production
  environment is available.

## Env vars (read by the future Azure OpenAI adapter)

| Env var | Required when wired | Example | Purpose |
|---|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | yes (prod posture) | `https://<resource>.openai.azure.com` | Azure OpenAI resource endpoint. |
| `AZURE_OPENAI_DEPLOYMENT` | yes (prod posture) | `gpt-4o` | Model deployment name. |
| `AZURE_OPENAI_API_VERSION` | no (default `2024-08-01-preview`) | `2024-08-01-preview` | Azure OpenAI API version. |
| `AZURE_OPENAI_AAD_APP_ID` | optional | `<uuid>` | Managed-identity app id (uses `DefaultAzureCredential`). |
| `AI_MAX_PROMPT_TOKENS` | no (default `4000`) | `4000` | Maximum prompt size; longer prompts are truncated with a warning. |

The local placeholder never reads these variables — they exist only so the
seam location is discoverable and the env-var contract is documented in one
place.

## Protocol contract

```python
class AzureOpenAIAdvisor:
    """Azure OpenAI adapter for AIAdvisor.

    Satisfies the Protocol declared in app.application.ai.boundary.AIAdvisor.
    Implementation strategy:
      - Use azure.identity.DefaultAzureCredential + the openai SDK
        (>=1.0) configured for Azure.
      - Apply the redaction list from
        ``app.infrastructure.telemetry.structured_logging`` to
        ``request.context`` *before* the model call; refuse to send any
        field on the deny-list.
      - Map the model response back to ``AIRecommendation`` with a stable
        ``model_id`` recorded for audit.
      - Degrade to a zero-confidence ``AIRecommendation`` on any
        recoverable error (timeout, throttling, content-filter
        rejection); the deterministic core must never depend on this
        adapter succeeding.
    """

    async def recommend(self, *, context: Mapping[str, object], prompt: str) -> AIRecommendation:
        raise DependencyUnavailable("Azure OpenAI adapter is not wired in this slice")
```

## Do NOT add Azure SDK imports here yet

The placeholder deliberately does **not** import `openai`,
`azure-identity`, or any other Azure SDK. The synthetic environment must
not drag Azure dependencies into its dependency closure.

The deterministic core never depends on this adapter — see the no-op
stub at `app/infrastructure/ai/noop_stub.py` for the local default and
FM-050 acceptance ("deterministic core passes without LLM").