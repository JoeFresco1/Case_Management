# ADR-012: AI capability boundary — recommend-only, human-authoritative

- Status: Accepted (2026-09-02)
- Scope: `backend/app/application/ai/boundary.py`,
  `backend/app/application/ai/decision.py`,
  `backend/app/infrastructure/ai/noop_stub.py`,
  `backend/app/infrastructure/ai/azure_openai.py`,
  `backend/tests/ai/`, AGENTS.md §4.R11, AGENTS.md §12,
  `docs/integrations/seams.md`.
- Supersedes: nothing.
- Related: Constitution XIV (deterministic core before AI),
  Pillar P11 §4 P11-US12, FM-050, AGENTS.md §12 (seam inventory:
  "AI assistance → `application/ai/*` Protocols + `infrastructure/ai/`
  adapters"). See also ADR-002 (integration seams pattern).

## Context

The platform wants AI to *assist* — extraction, summarization, search
ranking, workflow-authoring suggestions (P05, P06, P10, P12). It does
not want AI to *authorize* consequential action. Constitution XIV and
Pillar P11 §4 US12 ("AI suggestions cannot authorize; the suggestion
is presented to a human who authorizes as themselves; the human's
identity, not the AI's, is on the audit event") are non-negotiable.

Without a deliberate boundary, three failure modes are likely:

1. A workflow transition or a configuration publish ends up with
   `actor_type="ai"` and an authorization-shaped verdict from a model.
   The audit row records the AI as author; downstream consumers
   treat the AI as accountable.
2. An adapter exposes both `recommend(...)` and `authorize(...)`
   on the same Protocol. A reviewer reads the type signature and
   assumes the second is safe to wire.
4. A test relies on an LLM (or its mock) to drive the
   deterministic path. When the AI is unavailable, the workflow
   engine cannot run.

FM-050 acceptance is concrete: AI recommendation logged with
confidence + source + human decision separate; workflow transition
never auto-authorized by AI; deterministic core passes without LLM.

## Options considered

1. **Inline AI helper in `app/application/cases/` next to the
   first use case**
   - Pros: shortest path; no new module.
   - Cons: the helper cannot be reused by P05, P06, P10, P12; each
     new capability reinvents the boundary; the human-decision
     shape drifts across use cases. Violates Constitution XIV by
     drift, not by intent.
2. **Application-layer Protocol + separate `HumanDecision`
   dataclass + no-op local adapter + Azure placeholder**
   - Pros: the Protocol is the only verb (`recommend`); the human
     decision is its own object; the no-op proves FM-050
     acceptance ("deterministic core passes without LLM"); the
     Azure placeholder reserves the seam location without lying
     about being implemented. Mirrors the seams pattern in
     ADR-002.
   - Cons: more files; the placeholder must assert Protocol
     conformance at import time so a refactor that adds a second
     verb fails fast.
3. **Domain-layer port (`domain/ports.py`) with `recommend` /
   `authorize`**
   - Pros: uniform with the other infrastructure seams.
   - Cons: AI recommendation is application-shaped (text,
     confidence, source refs); it does not belong in the domain.
     Domain events are things that *happened*, not things being
     suggested. The Protocol in the domain would also push a
     cross-cutting concern (AI assist) into the pure layer that
     the architecture explicitly forbids from FastAPI / SQLAlchemy
     imports but allows AI to drift in.

## Decision

Adopt **option 2** with these specific rules:

1. **One verb on the AI port**: `AIAdvisor.recommend(...)`. The
   Protocol exposes no `authorize` / `approve` / `execute`. A test
   (`tests/ai/test_workflow_authorization_unchanged.py`) greps the
   Protocol for forbidden verbs and fails the build if a future
   refactor adds one.
2. **Human decision is its own struct**: `HumanDecision` carries
   `authorizer_id`, `rationale`, and `accepted_recommendation`.
   `HumanDecision.__post_init__` rejects the zero UUID, blank
   display name, and empty rationale so a future caller cannot
   drop the human from the audit row.
3. **No-op is the local default**: `NoOpAIAdvisor` returns a
   zero-confidence, empty-summary, no-source-refs recommendation
   with `model_id="noop"` and the warning
   `ai_advisor_disabled`. Deterministic, no I/O, no environment
   reads. Tests pin exact equality across calls so any future
   randomness surfaces here.
4. **Azure OpenAI is a placeholder seam**: `AzureOpenAIAdvisor`
   raises `DependencyUnavailable` on `recommend`. Construction
   asserts structural conformance to `AIAdvisor`. The deployment
   name and content-filter policy are constructor parameters so
   the adapter's contract is visible today, not invented when
   Azure connectivity lands.
5. **Confidence, source, model id, warnings always present on
   `AIRecommendation`**: downstream audit rows render every
   field. There is no path that records a recommendation without
   these. The seam does not silently drop provenance.
6. **No seam depends on an LLM to run**: `tests/ai/` exercises
   the no-op adapter and the deterministic `Policy` to prove the
   workflow authorization path is unchanged by AI absence. The
   workflow engine imports neither AI module at runtime.

## Consequences

### Positive

- The deterministic core works without any LLM call (FM-050
  acceptance). Demos, tests, and CI do not require Azure OpenAI.
- The human identity on consequential actions is structurally
  enforced: `HumanDecision` validation prevents an AI model id or
  a system actor from being recorded as the authorizer.
- Every recommendation is auditable. Source refs are required
  fields; absence of refs in the no-op is the proof the answer
  came from nowhere. Auditors can grep audit rows by `model_id`
  to find "this came from the no-op" or "this came from
  azure-openai:<deployment>".
- The placeholder adapter reserves the seam location. A future
  Azure-targeted PR drops a real implementation behind the same
  Protocol without touching application code.

### Negative / risks

- More files than option 1. The cost is bounded: four small files
  plus one test directory, all well under the 1000-line limit.
- The no-op is structurally an `AIAdvisor`. A reviewer who skims
  the seam may assume AI is "in use." Mitigated by the
  `model_id="noop"` + `ai_advisor_disabled` warning and the ADR
  cross-reference.
- An adapter that bypasses the Protocol (defines its own
  `authorize(...)` and never satisfies `AIAdvisor`) would not be
  caught by the import-time `isinstance` check. Mitigated by code
  review (R3) and by the explicit forbidden-verbs grep test.

### Operational notes

- `app.config.Settings` will gain an `ai_advisor` field whose
  default is `"noop"`. Wiring that is out of scope for this
  slice; when it lands, follow the seams pattern in ADR-002 (DI
  in `app/deps.py`, health endpoint visibility in
  `app/interfaces/http/routers/health.py`).
- The audit row payload schema for AI recommendations lives in
  P09 (FM-045). When that lands, the `model_id`, `confidence`,
  `source_refs`, and `warnings` fields here map 1:1 to the audit
  payload keys.

## Azure seam note

`app/infrastructure/ai/azure_openai.py` is the reserved slot. The
Azure adapter will:

- Use `azure.identity.DefaultAzureCredential` to fetch an
  `openai` client for the configured deployment.
- Apply the redaction list from
  `app.infrastructure.telemetry.structured_logging` to
  `request.context` *before* any model call; refuse to send any
  field on the deny-list.
- Map the model response back to `AIRecommendation` with a
  stable `model_id="azure-openai:<deployment>"` for audit.
- Degrade to a zero-confidence recommendation on timeout,
  throttling, or content-filter rejection.

None of that is implemented in this slice. The placeholder raises
`DependencyUnavailable` so the no-op remains the safe default.