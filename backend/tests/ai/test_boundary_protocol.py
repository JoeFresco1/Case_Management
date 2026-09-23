"""Tests for the ``AIAdvisor`` Protocol and its dataclasses.

Verifies the *shape* of the boundary — what the application layer
sees — without touching any adapter implementation.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from uuid import uuid4

import pytest

from app.application.ai.boundary import AIAdvisor, AIRecommendation, AIRequest
from app.infrastructure.ai.azure_openai import AzureOpenAIAdvisor
from app.infrastructure.ai.noop_stub import NoOpAIAdvisor


class TestAIRecommendationShape:
    """AIRecommendation is the audit-shaped answer. Its fields are
    load-bearing for FM-050 acceptance."""

    def test_required_fields_present(self) -> None:
        names = {f.name for f in fields(AIRecommendation)}
        # Acceptance: confidence + source + capability are required;
        # the recommendation must carry them so audit can render
        # them.
        assert {"capability", "summary", "confidence", "source_refs"} <= names

    def test_confidence_is_bounded_float_in_dataclass(self) -> None:
        # The dataclass does not enforce ``0.0 <= confidence <= 1.0``
        # at construction (Pydantic-style validators live in the
        # inbound boundary, not here), but the field type pins the
        # expected shape so an int-valued confidence is a type
        # error at review time.
        rec = AIRecommendation(capability="x", summary="", confidence=0.0, source_refs=())
        assert isinstance(rec.confidence, float)

    def test_recommendation_is_immutable(self) -> None:
        # The recommendation is the audit object — mutating it after
        # a downstream caller has logged it would invalidate the
        # audit chain. Frozen + slots make accidental mutation fail.
        rec = AIRecommendation(capability="x", summary="", confidence=0.0, source_refs=())
        with pytest.raises(FrozenInstanceError):
            rec.summary = "tampered"  # type: ignore[misc]


class TestAIRequestShape:
    """AIRequest carries the bounded context to the AI layer."""

    def test_required_fields_present(self) -> None:
        names = {f.name for f in fields(AIRequest)}
        # Acceptance: every request is tenant-scoped and correlation-
        # tagged so audit can group recommendation + decision.
        assert {
            "capability",
            "tenant_id",
            "subject_id",
            "correlation_id",
            "requested_by",
        } <= names

    def test_request_is_immutable(self) -> None:
        req = AIRequest(
            capability="x",
            tenant_id=uuid4(),
            subject_id=uuid4(),
            correlation_id="cid",
        )
        with pytest.raises(FrozenInstanceError):
            req.capability = "y"  # type: ignore[misc]


class TestAIAdvisorProtocolConformance:
    """Every adapter must structurally satisfy ``AIAdvisor``."""

    def test_noop_adapter_satisfies_protocol(self) -> None:
        assert isinstance(NoOpAIAdvisor(), AIAdvisor)

    def test_azure_placeholder_adapter_satisfies_protocol(self) -> None:
        # The placeholder raises on ``recommend`` but is still a
        # structural AIAdvisor. This guards against a future refactor
        # of the Protocol that silently breaks the placeholder.
        advisor = AzureOpenAIAdvisor(
            endpoint="https://example.test",
            deployment="gpt-4o-mini",
            content_filter_policy="default",
            api_version="2024-08-01",
        )
        assert isinstance(advisor, AIAdvisor)
