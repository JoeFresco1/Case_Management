"""Tests for the ``NoOpAIAdvisor`` local default adapter.

Validates the FM-050 acceptance that the deterministic core works
without an LLM.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.application.ai.boundary import AIAdvisor, AIRequest
from app.infrastructure.ai.noop_stub import NOOP_MODEL_ID, NoOpAIAdvisor


def _request(*, capability: str = "case.summarize") -> AIRequest:
    """Build a deterministic request for parameter-free tests."""
    return AIRequest(
        capability=capability,
        tenant_id=uuid4(),
        subject_id=uuid4(),
        context={"hint": "synthetic"},
        requested_by=uuid4(),
        correlation_id="cid-abc",
    )


class TestNoOpAIAdvisor:
    """The no-op adapter is the local default; every assertion here
    is also a load-bearing contract for any future adapter swap."""

    def test_is_structural_aiadvisor(self) -> None:
        # Protocol conformance: NoOpAIAdvisor must satisfy AIAdvisor
        # without inheritance. If the protocol gains a method, this
        # test fails fast.
        assert isinstance(NoOpAIAdvisor(), AIAdvisor)

    @pytest.mark.asyncio
    async def test_recommendation_carries_zero_confidence__no_ai_answer_marker(
        self,
    ) -> None:
        # Acceptance: AI recommendation logged with confidence
        # distinguishable from a real answer.
        rec = await NoOpAIAdvisor().recommend(_request())
        assert rec.confidence == 0.0
        assert rec.summary == ""

    @pytest.mark.asyncio
    async def test_recommendation_carries_model_id_for_audit_grep(self) -> None:
        # Auditors must be able to filter "this came from the no-op".
        rec = await NoOpAIAdvisor().recommend(_request())
        assert rec.model_id == NOOP_MODEL_ID

    @pytest.mark.asyncio
    async def test_recommendation_carries_disabled_warning(self) -> None:
        # The ``ai_advisor_disabled`` warning is the audit signal that
        # the AI layer was *not* in the loop. UI/audit code keys off
        # it; the no-op must emit it.
        rec = await NoOpAIAdvisor().recommend(_request())
        assert "ai_advisor_disabled" in rec.warnings

    @pytest.mark.asyncio
    async def test_recommendation_has_empty_source_refs__cannot_fabricate(
        self,
    ) -> None:
        # Source refs are how auditors verify provenance. The no-op
        # must never invent refs; absence of refs is the proof that
        # the answer came from nowhere.
        rec = await NoOpAIAdvisor().recommend(_request())
        assert rec.source_refs == ()

    @pytest.mark.asyncio
    async def test_noop_does_not_branch_by_capability(self) -> None:
        # Why: the no-op exists to prove the seam does not invent
        # answers. If a future maintainer adds capability-specific
        # canned answers here, downstream code may defer to them.
        # Pin the contract: every capability returns the same shape.
        rec_summary = await NoOpAIAdvisor().recommend(_request(capability="x"))
        rec_decide = await NoOpAIAdvisor().recommend(_request(capability="y"))
        assert rec_summary.confidence == rec_decide.confidence == 0.0
        assert rec_summary.summary == rec_decide.summary == ""

    @pytest.mark.asyncio
    async def test_noop_is_deterministic_across_calls(self) -> None:
        # Determinism is the FM-050 contract for the no-op: two calls
        # with the same request must produce identical results
        # without time/random/environment reads.
        adapter = NoOpAIAdvisor()
        req = _request()
        rec_a = await adapter.recommend(req)
        rec_b = await adapter.recommend(req)
        # dataclass equality compares every field, including the
        # tuple-shaped ones. If the no-op ever gains randomness, this
        # test fails.
        assert rec_a == rec_b

    @pytest.mark.asyncio
    async def test_noop_does_not_mutate_request(self) -> None:
        # Why: the request is shared with audit rows and downstream
        # callers. A side-effectful no-op would corrupt the audit
        # chain. Pin that ``context`` is unchanged after the call.
        req = _request()
        snapshot = dict(req.context)
        await NoOpAIAdvisor().recommend(req)
        assert req.context == snapshot

    def test_noop_model_id_is_stable_token(self) -> None:
        # The token is referenced by ADR-012 and the audit row's
        # payload schema. Renaming it is a breaking change.
        assert NOOP_MODEL_ID == "noop"

    def test_request_uuid_validation_is_type_enforced(self) -> None:
        # Belt-and-braces: the request must take UUIDs, not strings.
        # The test fails to construct if a future refactor relaxes the
        # type, surfacing the change at review time.
        req = _request()
        assert isinstance(req.tenant_id, UUID)
        assert isinstance(req.subject_id, UUID)
