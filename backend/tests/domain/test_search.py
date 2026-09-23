"""Unit tests for the canonical search domain (P06 / FM-031, FM-032).

Covers :mod:`app.domain.search`: query validation, authorization at
result time, snippet redaction by reference id, audit-shape hygiene
(no payload PHI), and the provider-spec translation. All helpers are
pure -- no fixtures, no I/O, no clock, no database.

Two-tenant design: ``test_authorize_results__mixed_tenants__keeps_only_requesting_tenant``
seeds hits from tenant A and tenant B and asserts the tenant-B rows
vanish for a tenant-A request. The SQL adapter's own
``WHERE tenant_id = :tenant_id`` is exercised separately in
``tests/application/search/test_sql_search_provider.py``; this suite
proves the domain second gate holds even when the engine over-returns.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.domain.errors import ValidationFailed
from app.domain.ports.search import SearchHit
from app.domain.search import (
    QueryFilter,
    SearchAuditEvent,
    SearchQuery,
    SearchResult,
    apply_result_time_policy,
    authorize_results,
    build_audit_event,
    from_provider_hit,
    query_shape_for_audit,
    redact_snippet,
    to_provider_spec,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")

ALICE = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

SEARCHABLE = ("participant_name", "notes")
SENSITIVE = ("ssn",)


def _hit(
    *,
    tenant_id: UUID = TENANT_A,
    case_id: UUID | None = None,
    snippet: dict[str, Any] | None = None,
) -> SearchResult:
    """Build one candidate hit for the given tenant."""
    return SearchResult(
        tenant_id=tenant_id,
        case_id=case_id or uuid4(),
        reference="C-ACME-2026-000001",
        status="open",
        priority="normal",
        title="Service Request",
        snippet=dict(snippet or {}),
    )


def _query(**overrides: Any) -> SearchQuery:
    """Build a valid query; callers override one field per test."""
    base: dict[str, Any] = {
        "tenant_id": TENANT_A,
        "actor_id": ALICE,
        "searchable_field_keys": SEARCHABLE,
        "sensitive_field_keys": SENSITIVE,
    }
    base.update(overrides)
    return SearchQuery(**base)


# ---------------------------------------------------------------------------
# 1. Query construction -- success
# ---------------------------------------------------------------------------


def test_search_query__valid_shape__constructs() -> None:
    # given: a worker filtering on a core column plus a searchable field
    # when: the query is constructed
    query = _query(
        text_query="intake",
        filters=(QueryFilter(field="status", op="eq", value="open"),),
    )
    # then: the shape survives verbatim for the engine translation
    assert query.limit == 50
    assert query.offset == 0
    assert query.filters[0].field == "status"


def test_search_query__data_prefix_searchable__constructs() -> None:
    # given: a data.<key> filter on a declared-searchable key
    # when: the query is constructed
    query = _query(filters=(QueryFilter(field="data.notes", op="like", value="vip"),))
    # then: no validation error; the key strips cleanly downstream
    assert query.filters[0].field == "data.notes"


# ---------------------------------------------------------------------------
# 2. Query construction -- validation failures
# ---------------------------------------------------------------------------


def test_search_query__limit_zero__raises_validation() -> None:
    with pytest.raises(ValidationFailed):
        _query(limit=0)


def test_search_query__limit_over_max__raises_validation() -> None:
    with pytest.raises(ValidationFailed):
        _query(limit=201)


def test_search_query__negative_offset__raises_validation() -> None:
    with pytest.raises(ValidationFailed):
        _query(offset=-1)


def test_search_query__unknown_operator__raises_validation() -> None:
    with pytest.raises(ValidationFailed):
        _query(filters=(QueryFilter(field="status", op="regex", value="open"),))


def test_search_query__empty_in_list__raises_validation() -> None:
    with pytest.raises(ValidationFailed):
        _query(filters=(QueryFilter(field="status", op="in", value=[]),))


def test_search_query__non_searchable_field__raises_validation() -> None:
    # given: a filter on a field the schema never declared searchable
    # when/then: construction rejects it (fail-closed, never broadened)
    with pytest.raises(ValidationFailed):
        _query(filters=(QueryFilter(field="data.ssn_undeclared", op="eq", value="x"),))


def test_search_query__non_searchable_data_key__raises_validation() -> None:
    with pytest.raises(ValidationFailed):
        _query(filters=(QueryFilter(field="data.internal_memo", op="like", value="x"),))


# ---------------------------------------------------------------------------
# 3. Authorization at result time
# ---------------------------------------------------------------------------


def test_authorize_results__visible_cases__preserves_order() -> None:
    # given: two hits the actor may read
    first = _hit()
    second = _hit()
    visible = frozenset({first.case_id, second.case_id})
    # when: the result-time gate runs
    kept = authorize_results([first, second], tenant_id=TENANT_A, visible_case_ids=visible)
    # then: both survive in engine order
    assert kept == (first, second)


def test_authorize_results__invisible_case__drops_row() -> None:
    # given: one visible hit and one the actor may not read
    # when: the gate runs with only the first id granted
    shown = _hit()
    hidden = _hit()
    kept = authorize_results(
        [shown, hidden], tenant_id=TENANT_A, visible_case_ids=frozenset({shown.case_id})
    )
    # then: the hidden row vanishes entirely (no id, no snippet leak)
    assert kept == (shown,)


def test_authorize_results__empty_grants__denies_by_default() -> None:
    # given: engine rows but no grants (deny-by-default)
    # when/then: nothing survives
    kept = authorize_results([_hit(), _hit()], tenant_id=TENANT_A, visible_case_ids=frozenset())
    assert kept == ()


def test_authorize_results__mixed_tenants__keeps_only_requesting_tenant() -> None:
    # given: hits from tenant A and tenant B (engine over-returned)
    mine = _hit(tenant_id=TENANT_A)
    theirs = _hit(tenant_id=TENANT_B)
    visible = frozenset({mine.case_id, theirs.case_id})
    # when: tenant A requests
    kept = authorize_results([mine, theirs], tenant_id=TENANT_A, visible_case_ids=visible)
    # then: the tenant-B row is dropped even though its id was granted
    assert kept == (mine,)


def test_authorize_results__cross_tenant_id_collision__still_drops() -> None:
    # given: a tenant-B row sharing a case id the actor may see in A
    # (pathological id reuse across tenants must never grant)
    shared_id = uuid4()
    mine = _hit(tenant_id=TENANT_A, case_id=shared_id)
    foreign = _hit(tenant_id=TENANT_B, case_id=shared_id)
    # when: tenant A requests with the shared id visible
    kept = authorize_results(
        [mine, foreign], tenant_id=TENANT_A, visible_case_ids=frozenset({shared_id})
    )
    # then: only the tenant-A row survives
    assert kept == (mine,)


# ---------------------------------------------------------------------------
# 4. Snippet redaction by reference id
# ---------------------------------------------------------------------------


def test_redact_snippet__restricted_key__becomes_ref_marker() -> None:
    # given: a snippet with a restricted excerpt
    # when: the actor lacks the sensitive-read grant
    out = redact_snippet(
        {"notes": "vip client", "ssn": "123-45-6789"},
        sensitive_keys=frozenset(SENSITIVE),
        can_see_sensitive=False,
    )
    # then: the public excerpt survives; the secret becomes a ref marker
    assert out["notes"] == "vip client"
    marker = out["ssn"]
    assert marker == {"redacted": True, "reason": "sensitive", "ref": "ssn"}
    assert "123-45-6789" not in str(out)


def test_redact_snippet__with_grant__returns_verbatim_copy() -> None:
    # given: the actor holds the sensitive-read grant
    snippet = {"ssn": "123-45-6789"}
    # when: redaction runs with the grant
    out = redact_snippet(snippet, sensitive_keys=frozenset(SENSITIVE), can_see_sensitive=True)
    # then: the value passes through, and the input was not mutated
    assert out == snippet
    assert out is not snippet


def test_redact_snippet__public_key_with_sensitive_looking_value__passes_through() -> None:
    # given: a public key whose text merely looks sensitive
    # when: redaction runs without the grant
    out = redact_snippet(
        {"notes": "ssn 123-45-6789 mentioned"},
        sensitive_keys=frozenset(SENSITIVE),
        can_see_sensitive=False,
    )
    # then: key-based (never content-based) redaction leaves it alone
    assert out == {"notes": "ssn 123-45-6789 mentioned"}


def test_redact_snippet__input_never_mutated() -> None:
    snippet = {"ssn": "secret"}
    redact_snippet(snippet, sensitive_keys=frozenset(SENSITIVE), can_see_sensitive=False)
    assert snippet == {"ssn": "secret"}


# ---------------------------------------------------------------------------
# 5. Composed result-time policy
# ---------------------------------------------------------------------------


def test_apply_policy__unauthorized_row__never_appears_even_redacted() -> None:
    # given: a visible restricted hit and an invisible public hit
    shown = _hit(snippet={"ssn": "123-45-6789", "notes": "hi"})
    hidden = _hit(snippet={"notes": "you cannot see this"})
    # when: the composed policy runs without the sensitive grant
    kept = apply_result_time_policy(
        [shown, hidden],
        tenant_id=TENANT_A,
        visible_case_ids=frozenset({shown.case_id}),
        sensitive_keys=frozenset(SENSITIVE),
        can_see_sensitive=False,
    )
    # then: one row survives, redacted; the hidden row is gone entirely
    assert len(kept) == 1
    assert kept[0].case_id == shown.case_id
    assert kept[0].snippet["ssn"] == {"redacted": True, "reason": "sensitive", "ref": "ssn"}
    assert kept[0].snippet["notes"] == "hi"
    assert all(h.case_id != hidden.case_id for h in kept)


def test_apply_policy__two_tenants__foreign_snippet_never_leaks() -> None:
    # given: same-tenant and cross-tenant hits with sensitive snippets
    mine = _hit(tenant_id=TENANT_A, snippet={"ssn": "a-secret"})
    foreign = _hit(tenant_id=TENANT_B, snippet={"ssn": "b-secret"})
    # when: tenant A applies the policy
    kept = apply_result_time_policy(
        [mine, foreign],
        tenant_id=TENANT_A,
        visible_case_ids=frozenset({mine.case_id, foreign.case_id}),
        sensitive_keys=frozenset(SENSITIVE),
        can_see_sensitive=False,
    )
    # then: only the tenant-A marker row survives; no foreign content
    assert [h.tenant_id for h in kept] == [TENANT_A]
    assert "b-secret" not in str(kept)


# ---------------------------------------------------------------------------
# 6. Audit shape hygiene (no payload PHI)
# ---------------------------------------------------------------------------


def test_query_shape__sensitive_filter_value__becomes_ref_marker() -> None:
    # given: a query filtering on a sensitive key with a secret value
    query = SearchQuery(
        tenant_id=TENANT_A,
        actor_id=ALICE,
        filters=(QueryFilter(field="data.ssn", op="eq", value="123-45-6789"),),
        searchable_field_keys=("ssn", *SEARCHABLE),
        sensitive_field_keys=SENSITIVE,
    )
    # when: the audit shape is built
    shape = query_shape_for_audit(query)
    # then: the value is a ref marker; the secret never reaches the row
    assert shape["filters"] == [
        {
            "field": "data.ssn",
            "op": "eq",
            "value": {"redacted": True, "reason": "sensitive", "ref": "ssn"},
        }
    ]
    assert "123-45-6789" not in str(shape)


def test_build_audit_event__carries_shape_count_and_actors() -> None:
    # given: a validated query
    query = _query(filters=(QueryFilter(field="status", op="eq", value="open"),))
    # when: the audit fact is built for 3 returned rows
    event = build_audit_event(query, result_count=3, occurred_at=FIXED_NOW)
    # then: tenant/actor/count/shape are all present, ad-hoc id is None
    assert isinstance(event, SearchAuditEvent)
    assert event.tenant_id == TENANT_A
    assert event.actor_id == ALICE
    assert event.saved_search_id is None
    assert event.result_count == 3
    assert event.occurred_at == FIXED_NOW
    assert event.query_shape["filters"] == [{"field": "status", "op": "eq", "value": "open"}]


def test_build_audit_event__saved_search_id__recorded() -> None:
    saved_id = uuid4()
    event = build_audit_event(
        _query(), result_count=0, occurred_at=FIXED_NOW, saved_search_id=saved_id
    )
    assert event.saved_search_id == saved_id


# ---------------------------------------------------------------------------
# 7. Provider translation
# ---------------------------------------------------------------------------


def test_to_provider_spec__maps_query__preserves_tenant_and_keys() -> None:
    # given: a domain query and a pre-computed sensitive grant
    query = _query(
        text_query="intake",
        filters=(QueryFilter(field="status", op="eq", value="open"),),
    )
    # when: translated for the engine
    spec = to_provider_spec(query, can_see_sensitive=False)
    # then: tenant, paging, grant, and key sets ride along unchanged
    assert spec.tenant_id == TENANT_A
    assert spec.text_query == "intake"
    assert spec.limit == 50
    assert spec.offset == 0
    assert spec.can_see_sensitive is False
    assert spec.searchable_field_keys == SEARCHABLE
    assert spec.sensitive_field_keys == SENSITIVE
    assert [(f.field, f.op) for f in spec.filters] == [("status", "eq")]


def test_from_provider_hit__stamps_tenant__keeps_snippet() -> None:
    # given: an engine hit (tenant-scoped by construction, no tenant field)
    case_id = uuid4()
    hit = SearchHit(
        case_id=case_id,
        reference="C-ACME-2026-000007",
        priority="urgent",
        status="open",
        title="Urgent intake",
        data_snippet={"notes": "vip"},
    )
    # when: lifted into the domain result
    result = from_provider_hit(hit, tenant_id=TENANT_A)
    # then: the requesting tenant is stamped; the snippet rides verbatim
    # (redaction happens later in the composed policy)
    assert result.tenant_id == TENANT_A
    assert result.case_id == case_id
    assert result.snippet == {"notes": "vip"}
