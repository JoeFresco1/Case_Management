"""Tests for the ``HumanDecision`` separation from AI recommendations.

P11-US12 and FM-050 acceptance: human authorization is authoritative;
the AI's answer is distinguishable from the human's.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from uuid import UUID, uuid4

import pytest

from app.application.ai.boundary import AIRecommendation
from app.application.ai.decision import HumanDecision
from app.domain.errors import ValidationFailed


def _decision(**overrides: object) -> HumanDecision:
    """Build a valid HumanDecision with sensible defaults."""
    base: dict[str, object] = {
        "decision_id": uuid4(),
        "authorizer_id": uuid4(),
        "authorizer_display_name": "Pat Operator",
        "accepted_recommendation": False,
        "rationale": "Reviewed the case; documents sufficient.",
        "correlation_id": "cid-abc",
        "recommendation_id": "rec-xyz",
    }
    base.update(overrides)
    return HumanDecision(**base)  # type: ignore[arg-type]


class TestHumanDecisionSeparatesHumanFromAI:
    """The decision is a separate struct from the recommendation.
    The two must never silently collapse."""

    def test_human_decision_is_distinct_from_ai_recommendation(self) -> None:
        # Acceptance: human decision and AI recommendation are
        # distinguishable. They are not the same type and not the
        # same field set.
        ai = AIRecommendation(
            capability="case.summarize",
            summary="AI suggests approve",
            confidence=0.7,
            source_refs=(("case", "case-1"),),
            model_id="noop",
        )
        decision = _decision()
        assert type(ai) is not type(decision)
        decision_fields = {f.name for f in decision.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        assert {"authorizer_id", "rationale", "accepted_recommendation"} <= decision_fields
        # The AI recommendation does NOT carry an authorizer_id.
        assert "authorizer_id" not in ai.__dataclass_fields__

    def test_decision_records_human_actor_id(self) -> None:
        # Acceptance: the human's identity is on the decision.
        decision = _decision()
        assert isinstance(decision.authorizer_id, UUID)
        assert decision.authorizer_id.int != 0

    def test_decision_carries_correlation_id_for_timeline_join(self) -> None:
        # Audit timeline joins recommendation + decision by
        # correlation_id. Without this, the link is lossy.
        decision = _decision(correlation_id="cid-42")
        assert decision.correlation_id == "cid-42"

    def test_decision_records_recommendation_id_when_ai_was_consulted(
        self,
    ) -> None:
        # If the AI was consulted, the decision must cite the
        # recommendation; absence means "no AI assistance."
        decision = _decision(recommendation_id="rec-1")
        assert decision.recommendation_id == "rec-1"

    def test_decision_records_empty_recommendation_id_when_ai_absent(
        self,
    ) -> None:
        decision = _decision(recommendation_id="")
        assert decision.recommendation_id == ""

    def test_decision_is_immutable(self) -> None:
        decision = _decision()
        with pytest.raises(FrozenInstanceError):
            decision.authorizer_id = uuid4()  # type: ignore[misc]


class TestHumanDecisionValidatesHumanIdentity:
    """Validation rules preventing an AI or system actor from being
    recorded as the authorizer."""

    def test_authorizer_id_zero_rejected(self) -> None:
        # The zero UUID is the sentinel "no user"; using it would
        # produce an audit row with a missing actor. Reject.
        with pytest.raises(ValidationFailed) as exc:
            HumanDecision(
                decision_id=uuid4(),
                authorizer_id=UUID(int=0),
                authorizer_display_name="x",
                accepted_recommendation=False,
                rationale="y",
                correlation_id="cid",
            )
        assert exc.value.field == "authorizer_id"

    def test_blank_display_name_rejected(self) -> None:
        with pytest.raises(ValidationFailed) as exc:
            HumanDecision(
                decision_id=uuid4(),
                authorizer_id=uuid4(),
                authorizer_display_name="",
                accepted_recommendation=False,
                rationale="y",
                correlation_id="cid",
            )
        assert exc.value.field == "authorizer_display_name"

    def test_empty_rationale_rejected(self) -> None:
        # Constitution IV requires consequential actions to be
        # explainable. An empty rationale is rejected at the boundary
        # so the audit row can never be blank.
        with pytest.raises(ValidationFailed) as exc:
            HumanDecision(
                decision_id=uuid4(),
                authorizer_id=uuid4(),
                authorizer_display_name="x",
                accepted_recommendation=False,
                rationale="",
                correlation_id="cid",
            )
        assert exc.value.field == "rationale"
