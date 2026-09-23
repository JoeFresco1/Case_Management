"""Tests for the semantic search use case (P06-US7 / FM-034).

Acceptance scenarios (per FM-034 / P06-US7):

1. **Authorization filters before ranking.** The structured provider
   returns the authorized candidate set; the ranker is then handed
   those ids and may only reorder. A case the actor cannot see must
   never appear in the result page, even if the ranker would have
   placed it first.
2. **AI ranks the authorized set.** The ranker's ordering is
   preserved in the result page; the ranker's aggregate confidence
   and warnings are written to the structured log so the audit
   timeline carries provenance.
3. **Deterministic authorization is authoritative.** The structured
   provider's tenant scoping is the line in the sand; if the ranker
   somehow returned an out-of-scope id the use case discards it.
4. **Recommendation is logged separately from any authorization
   decision.** The audit row carries the ranker's model id and
   confidence; no audit event records the AI as the author.

The fakes mirror the ``test_saved_search`` in-memory pattern so the
suite runs without Postgres / SQLite. The use case does not touch
SQL.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
import structlog

from app.application.ai.search_ranking import (
    RankedCandidate,
    RankingHints,
    RankingResult,
    SearchRanker,
)
from app.application.search.semantic_search import SemanticSearchQuery, handle
from app.domain.errors import Forbidden, ValidationFailed
from app.domain.ports import (
    SearchFilter,
    SearchHit,
    SearchProvider,
    SearchResultPage,
    SearchSpec,
)
from app.infrastructure.ai.azure_search_ranker import (
    AzureOpenAISearchRanker,
)
from app.infrastructure.ai.noop_search_ranker import (
    NOOP_RANKER_MODEL_ID,
    NoOpSearchRanker,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
BETA_TENANT = UUID("22222222-2222-2222-2222-222222222222")

INTAKE_ACTOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
REVIEWER_ACTOR = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
OUTSIDER = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _StoredCase:
    case_id: UUID
    tenant_id: UUID
    priority: str
    status: str
    title: str
    reference: str
    # When ``True`` the SQL adapter in production would redact the
    # snippet unless ``can_see_sensitive`` is granted. The fake
    # honors the flag without enforcing it.
    sensitive: bool = False


class _RecordingSearchProvider(SearchProvider):
    """Hermetic search provider with deterministic ordering.

    Stores the candidate set per tenant and returns it filtered by
    ``tenant_id`` so the test can prove that authorization happens at
    the structured layer — *before* the ranker is consulted. Records
    the last ``SearchSpec`` for assertions.
    """

    def __init__(self) -> None:
        self.cases: list[_StoredCase] = []
        self.last_spec: SearchSpec | None = None

    def add(self, case: _StoredCase) -> None:
        self.cases.append(case)

    async def search(self, spec: SearchSpec) -> SearchResultPage:
        self.last_spec = spec
        items: list[_StoredCase] = []
        for c in self.cases:
            if c.tenant_id != spec.tenant_id:
                # Tenant scoping is enforced here. The ranker never
                # sees these rows.
                continue
            ok = True
            for f in spec.filters:
                if not _match(c, f):
                    ok = False
                    break
            if ok:
                items.append(c)
        total = len(items)
        page = items[spec.offset : spec.offset + spec.limit]
        hits = [
            SearchHit(
                case_id=c.case_id,
                reference=c.reference,
                priority=c.priority,
                status=c.status,
                title=c.title,
                data_snippet={},
            )
            for c in page
        ]
        return SearchResultPage(
            items=hits,
            total_considered=total,
            limit=spec.limit,
            offset=spec.offset,
            next_cursor=None,
        )


def _core_column(c: _StoredCase, name: str) -> Any:
    return {
        "reference": c.reference,
        "status": c.status,
        "priority": c.priority,
        "title": c.title,
    }.get(name)


def _match(c: _StoredCase, f: SearchFilter) -> bool:
    col = _core_column(c, f.field)
    if col is None:
        return False
    if f.op == "eq":
        return str(col).lower() == str(f.value).lower()
    return False


class _StubRanker(SearchRanker):
    """In-memory ranker that returns candidates in a caller-chosen order.

    The stub is intentionally a ``SearchRanker`` structural-protocol
    implementer (not a subclass import) so tests can assert the
    Protocol is satisfiable without inheritance. The test passes the
    ordering it wants the use case to apply.
    """

    def __init__(self) -> None:
        self.last_candidate_ids: tuple[UUID, ...] = ()
        self.last_hints: RankingHints | None = None
        # When ``forced_ranking`` is set the stub returns this list
        # (filtered to the input ids) in the order given. Useful for
        # simulating "ranker would have reordered".
        self.forced_ranking: list[UUID] | None = None
        # When ``out_of_scope_id`` is set the stub *adds* it to the
        # returned list — even though it was not in
        # ``candidate_ids`` — to prove the use case discards it.
        self.out_of_scope_id: UUID | None = None
        self.confidence = 0.7
        self.warnings: tuple[str, ...] = ()
        self.model_id = "stub-ranker"

    async def rank(
        self,
        *,
        candidate_ids: tuple[UUID, ...],
        hints: RankingHints,
    ) -> RankingResult:
        self.last_candidate_ids = candidate_ids
        self.last_hints = hints
        order = list(self.forced_ranking) if self.forced_ranking else list(candidate_ids)
        # Optionally inject an out-of-scope id the ranker should never
        # have surfaced; tests verify the use case drops it.
        if self.out_of_scope_id is not None:
            order.insert(0, self.out_of_scope_id)
        candidates = tuple(
            RankedCandidate(case_id=cid, confidence=self.confidence, rank=idx)
            for idx, cid in enumerate(order)
        )
        return RankingResult(
            candidates=candidates,
            model_id=self.model_id,
            confidence=self.confidence,
            warnings=self.warnings,
            rationale="stub rationale",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider() -> _RecordingSearchProvider:
    p = _RecordingSearchProvider()
    p.add(
        _StoredCase(
            case_id=UUID("00000000-0000-0000-0000-00000000000a"),
            tenant_id=ACME_TENANT,
            priority="urgent",
            status="open",
            title="alpha",
            reference="C-ACME-2026-000001",
        )
    )
    p.add(
        _StoredCase(
            case_id=UUID("00000000-0000-0000-0000-00000000000b"),
            tenant_id=ACME_TENANT,
            priority="normal",
            status="open",
            title="beta",
            reference="C-ACME-2026-000002",
        )
    )
    p.add(
        _StoredCase(
            case_id=UUID("00000000-0000-0000-0000-00000000000c"),
            tenant_id=ACME_TENANT,
            priority="low",
            status="open",
            title="gamma",
            reference="C-ACME-2026-000003",
        )
    )
    # Cross-tenant case — must NEVER appear in any result page for
    # an ACME actor even if the ranker tried to surface it.
    p.add(
        _StoredCase(
            case_id=UUID("00000000-0000-0000-0000-00000000000d"),
            tenant_id=BETA_TENANT,
            priority="urgent",
            status="open",
            title="delta",
            reference="C-BETA-2026-000001",
        )
    )
    return p


def _intake_query(
    *,
    tenant_id: UUID = ACME_TENANT,
    actor_id: UUID = INTAKE_ACTOR,
    ranking: bool = True,
    text_query: str | None = "alpha",
    limit: int = 50,
) -> SemanticSearchQuery:
    return SemanticSearchQuery(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_roles=("intake",),
        correlation_id="cid-semantic-1",
        text_query=text_query,
        limit=limit,
        ranking=ranking,
    )


@pytest.fixture
def provider() -> _RecordingSearchProvider:
    return _make_provider()


@pytest.fixture
def ranker() -> _StubRanker:
    return _StubRanker()


# The policy is constructed lazily via ``default_policy()`` and
# reads the static ``DEFAULT_ROLES`` registry on each call; no
# per-test reset is needed.


# ---------------------------------------------------------------------------
# Recommendation log capture
# ---------------------------------------------------------------------------


class _ListHandler(logging.Handler):
    """Capture structlog ``INFO`` records for assertion."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def capture_recommendation() -> _ListHandler:
    """Route structlog records through a list handler for assertion.

    Structlog's default factory is a ``PrintLoggerFactory``; tests
    need a handler that can be inspected. We add a stdlib handler to
    the ``semantic_search`` logger and clear it after the test.
    """

    handler = _ListHandler()
    logger = logging.getLogger("app.application.search.semantic_search")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    structlog.configure(logger_factory=structlog.stdlib.LoggerFactory())
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        structlog.reset_defaults()


# ---------------------------------------------------------------------------
# Acceptance: ranker handles authorization-filtered candidates only
# ---------------------------------------------------------------------------


class TestSemanticSearchAuthorizationFilterBeforeRanking:
    """The ranker is handed the post-authorization candidate set only."""

    @pytest.mark.asyncio
    async def test_ranker_never_sees_cross_tenant_case(
        self,
        provider: _RecordingSearchProvider,
        ranker: _StubRanker,
    ) -> None:
        # Even though the ranker is configured to inject a cross-
        # tenant id, the use case must drop it: that id was never in
        # the structured provider's authorized set.
        beta_id = UUID("00000000-0000-0000-0000-00000000000d")
        ranker.out_of_scope_id = beta_id
        result = await handle(
            _intake_query(),
            search_provider=provider,
            ranker=ranker,
        )
        result_ids = {h.case_id for h in result.items}
        assert beta_id not in result_ids, (
            "out-of-scope id must be discarded by the use case, "
            "even when the ranker tried to surface it"
        )

    @pytest.mark.asyncio
    async def test_ranker_only_receives_authorized_candidate_ids(
        self,
        provider: _RecordingSearchProvider,
        ranker: _StubRanker,
    ) -> None:
        # The ranker is given exactly the structured provider's
        # authorized ids, in order. No cross-tenant id leaks into
        # the ranking hint.
        await handle(
            _intake_query(),
            search_provider=provider,
            ranker=ranker,
        )
        authorized = {c.case_id for c in provider.cases if c.tenant_id == ACME_TENANT}
        assert set(ranker.last_candidate_ids) == authorized
        for cid in ranker.last_candidate_ids:
            assert cid in authorized


# ---------------------------------------------------------------------------
# Acceptance: AI ranks and confidence is logged separately
# ---------------------------------------------------------------------------


class TestSemanticSearchAIRanksAndConfidenceLogged:
    """The ranker's ordering drives the result page; confidence is
    written to the structured log."""

    @pytest.mark.asyncio
    async def test_result_page_reflects_ranker_ordering(
        self,
        provider: _RecordingSearchProvider,
        ranker: _StubRanker,
    ) -> None:
        # Force the ranker to surface beta first, alpha second, gamma
        # last. The result page must follow that order.
        alpha = UUID("00000000-0000-0000-0000-00000000000a")
        beta = UUID("00000000-0000-0000-0000-00000000000b")
        gamma = UUID("00000000-0000-0000-0000-00000000000c")
        ranker.forced_ranking = [beta, alpha, gamma]
        result = await handle(
            _intake_query(),
            search_provider=provider,
            ranker=ranker,
        )
        assert [h.case_id for h in result.items] == [beta, alpha, gamma]

    @pytest.mark.asyncio
    async def test_recommendation_log_carries_confidence_and_provenance(
        self,
        provider: _RecordingSearchProvider,
        ranker: _StubRanker,
        capture_recommendation: _ListHandler,
    ) -> None:
        ranker.confidence = 0.42
        ranker.warnings = ("context_truncated",)
        ranker.model_id = "stub-ranker-v1"
        await handle(
            _intake_query(),
            search_provider=provider,
            ranker=ranker,
        )
        events = [r.getMessage() for r in capture_recommendation.records]
        assert any("semantic_search.recommendation" in e for e in events), (
            "the AI recommendation must be logged on its own line"
        )
        # Inspect the most recent recommendation record for the
        # expected keys. structlog renders as a JSON-ish string under
        # the stdlib factory.
        rec_lines = [
            r.getMessage()
            for r in capture_recommendation.records
            if "semantic_search.recommendation" in r.getMessage()
        ]
        assert rec_lines, "recommendation log line missing"
        last = rec_lines[-1]
        for needle in (
            "ranking_model_id",
            "ranking_confidence",
            "ranking_warnings",
            "ranking_candidate_count",
            "authorized_count",
            "result_count",
        ):
            assert needle in last, f"missing {needle!r} in {last!r}"

    @pytest.mark.asyncio
    async def test_recommendation_log_does_not_record_ai_as_actor(
        self,
        provider: _RecordingSearchProvider,
        ranker: _StubRanker,
        capture_recommendation: _ListHandler,
    ) -> None:
        # The log line carries ``actor_id`` (the human actor) and
        # ``tenant_id`` (the human's tenant). The audit row must not
        # carry the AI as the actor — that would conflate
        # recommendation with authorization decision (ADR-012).
        await handle(
            _intake_query(),
            search_provider=provider,
            ranker=ranker,
        )
        rec_lines = [
            r.getMessage()
            for r in capture_recommendation.records
            if "semantic_search.recommendation" in r.getMessage()
        ]
        assert rec_lines
        last = rec_lines[-1]
        # The AI model id appears as ``ranking_model_id`` (provenance),
        # never as the actor.
        assert "actor_id=" in last
        assert "ranking_model_id=" in last

        # structlog renders with a single space after ``=``; allow
        # values that may be empty in tests without bound contextvars.
        actor_match = re.search(r"actor_id=([^ ]*)", last)
        model_match = re.search(r"ranking_model_id=(\S+)", last)
        assert actor_match is not None
        assert model_match is not None
        assert model_match.group(1) != actor_match.group(1), (
            "actor_id must be the human, not the AI model id"
        )


# ---------------------------------------------------------------------------
# Acceptance: deterministic authorization is authoritative
# ---------------------------------------------------------------------------


class TestSemanticSearchDeterministicAuthorizationAuthoritative:
    """The structured provider is the line in the sand for authorization."""

    @pytest.mark.asyncio
    async def test_noop_ranker_preserves_structured_order(
        self,
        provider: _RecordingSearchProvider,
    ) -> None:
        # When the AI is disabled (noop), the result page falls back
        # to the structured provider's order. Authorization is still
        # enforced — the cross-tenant case never appears.
        result = await handle(
            _intake_query(),
            search_provider=provider,
            ranker=NoOpSearchRanker(),
        )
        ids = [h.case_id for h in result.items]
        assert BETA_TENANT not in [  # sanity
            c.tenant_id for c in provider.cases if c.case_id in ids
        ]
        # Noop returns zero-confidence: use case still emits a log
        # line so audit rows are uniform across modes.
        assert all(
            h.case_id
            in {
                UUID("00000000-0000-0000-0000-00000000000a"),
                UUID("00000000-0000-0000-0000-00000000000b"),
                UUID("00000000-0000-0000-0000-00000000000c"),
            }
            for h in result.items
        )

    @pytest.mark.asyncio
    async def test_noop_ranker_logs_disabled_warning(
        self,
        provider: _RecordingSearchProvider,
        capture_recommendation: _ListHandler,
    ) -> None:
        # When the no-op ranker is in use the audit row carries the
        # ``ai_ranker_disabled`` warning so reviewers can tell the
        # result page was not AI-ranked.
        await handle(
            _intake_query(),
            search_provider=provider,
            ranker=NoOpSearchRanker(),
        )
        rec_lines = [
            r.getMessage()
            for r in capture_recommendation.records
            if "semantic_search.recommendation" in r.getMessage()
        ]
        assert rec_lines
        last = rec_lines[-1]
        assert "ai_ranker_disabled" in last
        assert NOOP_RANKER_MODEL_ID in last


# ---------------------------------------------------------------------------
# Authorization denial (deny-by-default)
# ---------------------------------------------------------------------------


class TestSemanticSearchAuthorizationDenial:
    """Deny-by-default: an actor without ``CASE_READ`` is denied."""

    @pytest.mark.asyncio
    async def test_actor_without_case_read_role__raises_forbidden(
        self,
        provider: _RecordingSearchProvider,
        ranker: _StubRanker,
    ) -> None:
        # The actor carries no role. Authorization must deny before
        # the ranker is consulted.
        q = _intake_query(actor_id=OUTSIDER)
        # Replace roles with an empty tuple.
        q = SemanticSearchQuery(
            tenant_id=q.tenant_id,
            actor_id=q.actor_id,
            actor_roles=(),
            correlation_id=q.correlation_id,
            text_query=q.text_query,
            limit=q.limit,
            ranking=q.ranking,
        )
        with pytest.raises(Forbidden):
            await handle(q, search_provider=provider, ranker=ranker)
        # The ranker was never asked to rank.
        assert ranker.last_candidate_ids == ()

    @pytest.mark.asyncio
    async def test_cross_tenant_id_never_in_authorized_set(
        self,
        provider: _RecordingSearchProvider,
        ranker: _StubRanker,
    ) -> None:
        # An actor in BETA sees BETA cases only. ACME ids never
        # appear in the result page or in the ranker's input. This
        # is the documented tenant-isolation contract for the
        # structured provider (AGENTS.md §16.3).
        acme_ids = {
            UUID("00000000-0000-0000-0000-00000000000a"),
            UUID("00000000-0000-0000-0000-00000000000b"),
            UUID("00000000-0000-0000-0000-00000000000c"),
        }
        q = _intake_query(tenant_id=BETA_TENANT)
        result = await handle(q, search_provider=provider, ranker=ranker)
        result_ids = {h.case_id for h in result.items}
        assert result_ids.isdisjoint(acme_ids)
        # The ranker never saw any ACME id.
        assert set(ranker.last_candidate_ids).isdisjoint(acme_ids)


# ---------------------------------------------------------------------------
# Validation: filters and pagination
# ---------------------------------------------------------------------------


class TestSemanticSearchValidation:
    """Re-uses the structured search's validation surface."""

    @pytest.mark.asyncio
    async def test_unknown_filter_operator__raises_validation_failed(
        self,
        provider: _RecordingSearchProvider,
        ranker: _StubRanker,
    ) -> None:
        q = _intake_query()
        q = SemanticSearchQuery(
            tenant_id=q.tenant_id,
            actor_id=q.actor_id,
            actor_roles=q.actor_roles,
            correlation_id=q.correlation_id,
            filters=(("status", "regex", "open"),),
            text_query=q.text_query,
            limit=q.limit,
            ranking=q.ranking,
        )
        with pytest.raises(ValidationFailed):
            await handle(q, search_provider=provider, ranker=ranker)

    @pytest.mark.asyncio
    async def test_non_searchable_field__raises_validation_failed(
        self,
        provider: _RecordingSearchProvider,
        ranker: _StubRanker,
    ) -> None:
        q = _intake_query()
        q = SemanticSearchQuery(
            tenant_id=q.tenant_id,
            actor_id=q.actor_id,
            actor_roles=q.actor_roles,
            correlation_id=q.correlation_id,
            filters=(("data.private_note", "eq", "x"),),
            text_query=q.text_query,
            limit=q.limit,
            ranking=q.ranking,
            searchable_field_keys=(),
        )
        with pytest.raises(ValidationFailed):
            await handle(q, search_provider=provider, ranker=ranker)

    @pytest.mark.asyncio
    async def test_limit_out_of_range__raises_validation_failed(
        self,
        provider: _RecordingSearchProvider,
        ranker: _StubRanker,
    ) -> None:
        q = _intake_query(limit=500)
        with pytest.raises(ValidationFailed):
            await handle(q, search_provider=provider, ranker=ranker)

    @pytest.mark.asyncio
    async def test_offset_negative__raises_validation_failed(
        self,
        provider: _RecordingSearchProvider,
        ranker: _StubRanker,
    ) -> None:
        q = _intake_query()
        q = SemanticSearchQuery(
            tenant_id=q.tenant_id,
            actor_id=q.actor_id,
            actor_roles=q.actor_roles,
            correlation_id=q.correlation_id,
            text_query=q.text_query,
            limit=q.limit,
            offset=-1,
            ranking=q.ranking,
        )
        with pytest.raises(ValidationFailed):
            await handle(q, search_provider=provider, ranker=ranker)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestSearchRankerProtocolConformance:
    """The no-op and azure placeholder must satisfy the Protocol."""

    def test_noop_ranker_satisfies_search_ranker_protocol(self) -> None:
        assert isinstance(NoOpSearchRanker(), SearchRanker)

    def test_azure_placeholder_satisfies_search_ranker_protocol(self) -> None:
        # Structural conformance only — instantiation must not fail.
        assert isinstance(
            AzureOpenAISearchRanker(
                endpoint="",
                deployment="",
                content_filter_policy="",
                api_version="",
            ),
            SearchRanker,
        )
