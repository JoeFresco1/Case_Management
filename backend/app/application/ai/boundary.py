"""AI capability boundary port.

# Why this Protocol lives in the application layer

AGENTS.md §4.R11 and Constitution XIV are explicit: AI is **bounded
assistance only**. The deterministic workflow engine and authorization
policy must work without any LLM call. Where AI would add value — for
example FM-050 (P11-US12) "AI may recommend but never authorize
consequential work" — the AI capability is exposed as a **port** here
and implemented in ``app.infrastructure.ai``.

The port is the seam that:

- Lets the application ask for a recommendation *before* a human
  decision, capture it with its confidence and provenance, and surface
  it in the audit timeline.
- Refuses to expose any ``authorize(...)`` / ``execute(...)`` shape: an
  AI adapter cannot be the actor that flips a case from one workflow
  state to another. Authorization is the human's, by id.
- Lets tests and local development run with the no-op stub
  (``NoOpAIAdvisor``) — the domain is unaffected by the absence of an
  LLM (FM-050 acceptance).

# Why this is not a domain port

The protocol returns recommendation-shaped data (text, confidence,
source refs) which is application-layer concern. The domain only knows
about events that already happened; the AI boundary is the *moment of
seeking guidance before a domain change*. Anything that touches domain
invariants belongs in ``app.domain.rules`` (P02 rule evaluator),
not here.

# Why ``recommend`` is the only verb

A second verb would be a smuggling mechanism for "AI auto-approved
this." Naming enforces the boundary: ``recommend`` returns evidence;
``HumanDecision`` (see ``app.application.ai.decision``) carries the
authoritative answer. A reviewer who reads this file later should be
able to grep for any callable on the AI port and find ``recommend`` and
nothing else.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AIRequest:
    """A bounded, evidence-shaped query to the AI layer.

    Why frozen + slots: the request is the boundary object that flows
    through the AI adapter and into the audit log. We must not allow
    callers to mutate it after the adapter has read its fields.

    Attributes:
        capability: The advisory capability name (e.g.
            ``"case.summarize"``, ``"workflow.suggest_transition"``,
            ``"extraction.invoice_fields"``). Each capability is
            registered with a published schema; unknown capabilities
            cause ``NoOpAIAdvisor`` (and any well-behaved adapter) to
            return an empty recommendation rather than guessing.
        tenant_id: The tenant scope of the request. Required so an AI
            adapter cannot be tricked into cross-tenant retrieval and
            so every audit row carries the right scope.
        subject_id: The id of the subject being analyzed (case id,
            document id, etc.). Adapter implementations must include
            this in the recommendation's ``source_refs`` so audit can
            prove what the AI saw.
        context: A bounded dict of evidence the AI is allowed to
            consider. PHI is forbidden in this dict by the redaction
            list in ``app.infrastructure.telemetry``; the AI adapter
            must assume callers may have already redacted.
        requested_by: The human user who asked for the recommendation.
            The audit row records *this* identity as the actor on the
            recommendation event — not the AI's.
        correlation_id: The request's correlation id. Carried into the
            audit event so the recommendation can be located next to
            the human decision in the timeline.
    """

    capability: str
    tenant_id: UUID
    subject_id: UUID
    context: dict[str, Any] = field(default_factory=dict)
    requested_by: UUID | None = None
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class AIRecommendation:
    """The AI's advisory output.

    Why this carries confidence and source, not just text: P11-US12
    ("AI suggestions cannot authorize") and FM-050 acceptance ("AI
    recommendation logged with confidence+source+human decision
    separate") require downstream code to be able to *distinguish*
    AI-suggested answers from human decisions, and to surface the AI's
    provenance in the audit timeline.

    Attributes:
        capability: Echoes the request capability for downstream
            routing / logging.
        summary: Short human-readable suggestion. Never authoritative;
            it is a *recommendation* displayed to a human reviewer.
        confidence: A float in ``[0.0, 1.0]``. Adapters that cannot
            score their confidence must return ``0.0`` rather than
            invent a number; downstream UI may choose to suppress low-
            confidence suggestions entirely (out of scope here).
        source_refs: References (ids + brief labels) that the answer was
            drawn from. Every entry must be an id the human reviewer
            can open (case id, document id, audit event id). Free-text
            "explanations" without a reference are not allowed —
            auditors must be able to verify provenance.
        rationale: A short structured rationale. Free text is
            permitted, but it must not contain content the AI
            fabricated; well-behaved adapters cite ``source_refs``
            inline.
        model_id: Identifier of the underlying model (deployment name,
            ``"noop"`` for the no-op adapter). Always present so the
            audit row records *which* AI produced the answer; this is
            required for incident review and for the eventual model
            retirement workflow (P11).
        warnings: Adapter-emitted warnings ("context truncated",
            "schema version unknown", etc.). Surfaced in the audit
            row's payload so reviewers can judge the answer.
    """

    capability: str
    summary: str
    confidence: float
    source_refs: tuple[tuple[str, str], ...]
    rationale: str = ""
    model_id: str = "unknown"
    warnings: tuple[str, ...] = ()


@runtime_checkable
class AIAdvisor(Protocol):
    """The AI boundary port.

    The protocol exposes a single verb (``recommend``). An adapter that
    defines any additional verb is suspect at code-review time and
    should be challenged against FM-050 / Constitution XIV.

    The protocol is ``@runtime_checkable`` so tests can assert
    ``isinstance(adapter, AIAdvisor)`` against in-memory fakes without
    subclassing.
    """

    @abstractmethod
    async def recommend(self, request: AIRequest) -> AIRecommendation:
        """Return a recommendation for ``request``.

        Implementations must:

        - Never raise on missing context — return an empty
          ``AIRecommendation`` with ``confidence=0.0`` and a warning
          instead. The call site is on the human-decision hot path and
          must not fail because the AI layer is degraded.
        - Never block domain writes. The recommendation is *displayed*
          to a human reviewer; the human's decision is what the audit
          log records as the authoritative action.
        - Redact PHI before any external call. The local no-op adapter
          performs no I/O; the Azure OpenAI placeholder documents the
          redaction contract but does not implement it.

        Args:
            request: A bounded ``AIRequest`` carrying the capability,
                tenant scope, subject id, and allowed context.

        Returns:
            An ``AIRecommendation``. Never ``None``. Callers that need
            to detect "no AI answer" compare ``confidence == 0.0``
            and ``summary == ""``.

        Raises:
            Never. Implementations swallow recoverable errors and
            return a zero-confidence recommendation with a warning.
        """


__all__ = ["AIAdvisor", "AIRequest", "AIRecommendation"]
