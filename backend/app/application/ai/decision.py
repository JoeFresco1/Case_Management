"""Human-decision record for AI-assisted consequential actions.

# Why human decisions are a separate object from AI recommendations

Constitution XIV and FM-050 (P11-US12) require that "the accountable
human decision and identity remain distinguishable and audited." A
recommendation that lives in the same struct as the decision would
let a careless reviewer conflate the two; a separate object forces
every layer that records a final action to cite the human identity,
not the AI.

This module is intentionally tiny: it owns the dataclass and the
invariant that ``authorizer_id`` is never ``None`` and never equal to
an AI model id. Validation lives here so the use case that records
the decision cannot accidentally drop the human.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.domain.errors import ValidationFailed


@dataclass(frozen=True, slots=True)
class HumanDecision:
    """The authoritative answer on a consequential action.

    Attributes:
        decision_id: Stable id for the decision record. Distinct from
            the ``AIRequest`` id; the timeline groups them by
            ``correlation_id``.
        authorizer_id: The human user who authorized the action. Must
            be a real seeded user; never ``None``, never a system
            actor (``is_system=True`` users cannot authorize a
            consequential AI-assisted action), never an AI model id.
        authorizer_display_name: Best-effort display label for the
            audit row. The audit row's ``actor_id`` is
            ``authorizer_id``; ``authorizer_display_name`` is for
            human reviewers only.
        accepted_recommendation: True if the human accepted the AI's
            ``summary`` verbatim, False if they overrode it. ``False`` is
            the more conservative answer and is what auditors expect.
        rationale: The human's own reasoning. Persisted alongside
            the decision so the AI's ``rationale`` can never be
            substituted for the human's.
        correlation_id: Copied from the ``AIRequest`` so the
            recommendation and the decision land in the same timeline
            slice.
        recommendation_id: A reference (id or hash) to the
            recommendation that influenced the decision. Empty string
            means the decision was made without AI assistance.
    """

    decision_id: UUID
    authorizer_id: UUID
    authorizer_display_name: str
    accepted_recommendation: bool
    rationale: str
    correlation_id: str
    recommendation_id: str = ""

    def __post_init__(self) -> None:
        # Why validate here, not in the use case: every caller goes
        # through this constructor. Centralizing the check means a
        # future caller cannot quietly drop the human author.
        if self.authorizer_id.int == 0:
            raise ValidationFailed(
                "HumanDecision.authorizer_id must be a real user id",
                field="authorizer_id",
            )
        if not self.authorizer_display_name:
            raise ValidationFailed(
                "HumanDecision.authorizer_display_name is required",
                field="authorizer_display_name",
            )
        if not self.rationale:
            # The platform's policy (Constitution IV) requires that
            # consequential decisions be explainable. An empty
            # rationale means the human clicked "approve" without
            # articulating why — we reject at the boundary so the
            # audit row is never blank.
            raise ValidationFailed(
                "HumanDecision.rationale is required for consequential actions",
                field="rationale",
            )


__all__ = ["HumanDecision"]
