"""Unit tests for the pure rule evaluator (P05 §2 / P02 / T067).

Every operator (``all/any/not/eq/ne/in/gt/gte/lt/lte/exists/contains/
regex``), nesting, both accepted shapes (canonical single-key and
authoring ``{op, ...}``), depth capping, and type-error behavior.
``evaluate`` is pure — no fixtures, no I/O, no clock.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.rules import (
    ALLOWED_OPERATORS,
    RuleEvaluationError,
    evaluate,
    normalize_condition,
)

CTX: dict[str, Any] = {
    "case": {
        "priority": "high",
        "score": 42,
        "title": "Service Request",
        "tags": ["vip", "contract"],
        "participants": [{"role": "reviewer"}],
        "empty_list": [],
        "blank": "",
    }
}


def test_evaluate__eq_match__returns_true() -> None:
    assert evaluate({"eq": {"field": "case.priority", "value": "high"}}, CTX) is True


def test_evaluate__eq_mismatch__returns_false() -> None:
    assert evaluate({"eq": {"field": "case.priority", "value": "low"}}, CTX) is False


def test_evaluate__ne__inverts_eq() -> None:
    assert evaluate({"ne": {"field": "case.priority", "value": "low"}}, CTX) is True
    assert evaluate({"ne": {"field": "case.priority", "value": "high"}}, CTX) is False


def test_evaluate__in_collection__returns_true() -> None:
    assert evaluate({"in": {"field": "case.tags", "value": "vip"}}, CTX) is True
    assert evaluate({"in": {"field": "case.tags", "value": "nope"}}, CTX) is False
    # `in` on a string field is substring membership.
    assert evaluate({"in": {"field": "case.title", "value": "Service"}}, CTX) is True


def test_evaluate__ordering_operators__compare_numerically() -> None:
    assert evaluate({"gt": {"field": "case.score", "value": 40}}, CTX) is True
    assert evaluate({"gte": {"field": "case.score", "value": 42}}, CTX) is True
    assert evaluate({"lt": {"field": "case.score", "value": 40}}, CTX) is False
    assert evaluate({"lte": {"field": "case.score", "value": 42}}, CTX) is True


def test_evaluate__ordering_missing_field__fails_closed() -> None:
    assert evaluate({"gt": {"field": "case.absent", "value": 1}}, CTX) is False


def test_evaluate__exists_present__returns_true() -> None:
    assert evaluate({"exists": "case.priority"}, CTX) is True
    assert evaluate({"exists": {"field": "case.tags", "min": 1}}, CTX) is True


def test_evaluate__exists_missing_or_empty__returns_false() -> None:
    assert evaluate({"exists": "case.absent"}, CTX) is False
    assert evaluate({"exists": "case.empty_list"}, CTX) is False
    assert evaluate({"exists": "case.blank"}, CTX) is False
    assert evaluate({"exists": {"field": "case.tags", "min": 5}}, CTX) is False


def test_evaluate__contains__matches_members_and_substrings() -> None:
    assert evaluate({"contains": {"field": "case.tags", "value": "contract"}}, CTX) is True
    assert evaluate({"contains": {"field": "case.title", "value": "Service"}}, CTX) is True
    assert evaluate({"contains": {"field": "case.absent", "value": "x"}}, CTX) is False


def test_evaluate__regex__matches_pattern() -> None:
    assert evaluate({"regex": {"field": "case.title", "pattern": "^Service"}}, CTX) is True
    assert evaluate({"regex": {"field": "case.title", "pattern": "^X"}}, CTX) is False


def test_evaluate__all__requires_every_child() -> None:
    cond = {
        "all": [
            {"eq": {"field": "case.priority", "value": "high"}},
            {"gt": {"field": "case.score", "value": 40}},
        ]
    }
    assert evaluate(cond, CTX) is True
    cond["all"].append({"eq": {"field": "case.priority", "value": "low"}})
    assert evaluate(cond, CTX) is False


def test_evaluate__any__requires_one_child() -> None:
    cond = {
        "any": [
            {"eq": {"field": "case.priority", "value": "low"}},
            {"gt": {"field": "case.score", "value": 40}},
        ]
    }
    assert evaluate(cond, CTX) is True


def test_evaluate__not__negates_child() -> None:
    assert evaluate({"not": {"eq": {"field": "case.priority", "value": "low"}}}, CTX) is True
    assert evaluate({"not": {"eq": {"field": "case.priority", "value": "high"}}}, CTX) is False


def test_evaluate__nested_composites__recurse() -> None:
    cond = {
        "all": [
            {
                "any": [
                    {"eq": {"field": "case.priority", "value": "low"}},
                    {"not": {"eq": {"field": "case.priority", "value": "low"}}},
                ]
            },
            {"exists": "case.tags"},
        ]
    }
    assert evaluate(cond, CTX) is True


def test_evaluate__authoring_shape__evaluates_identically() -> None:
    assert evaluate({"op": "eq", "field": "case.priority", "value": "high"}, CTX) is True
    assert (
        evaluate(
            {
                "op": "all",
                "clauses": [
                    {"op": "eq", "field": "case.priority", "value": "high"},
                    {"op": "exists", "field": "case.tags", "min": 1},
                ],
            },
            CTX,
        )
        is True
    )
    assert (
        evaluate(
            {"op": "not", "clause": {"op": "eq", "field": "case.priority", "value": "low"}}, CTX
        )
        is True
    )


def test_normalize_condition__canonical__passes_through() -> None:
    cond = {"gte": {"field": "case.score", "value": 1}}
    assert normalize_condition(cond) == cond


def test_normalize_condition__mixed_nesting__normalizes_recursively() -> None:
    assert normalize_condition({"all": [{"op": "eq", "field": "f", "value": 1}]}) == {
        "all": [{"eq": {"field": "f", "value": 1}}]
    }


def test_evaluate__unknown_operator__raises() -> None:
    with pytest.raises(RuleEvaluationError):
        evaluate({"frobnicate": {"field": "case.priority"}}, CTX)
    with pytest.raises(RuleEvaluationError):
        evaluate({"op": "frobnicate", "field": "case.priority"}, CTX)


def test_evaluate__multi_key_condition__raises() -> None:
    with pytest.raises(RuleEvaluationError):
        evaluate({"eq": {"field": "case.priority", "value": "high"}, "ne": {}}, CTX)


def test_evaluate__empty_or_non_mapping__raises() -> None:
    with pytest.raises(RuleEvaluationError):
        evaluate({}, CTX)
    with pytest.raises(RuleEvaluationError):
        evaluate(["eq"], CTX)  # type: ignore[arg-type]


def test_evaluate__missing_field_entry__raises() -> None:
    with pytest.raises(RuleEvaluationError):
        evaluate({"eq": {"value": 1}}, CTX)
    with pytest.raises(RuleEvaluationError):
        evaluate({"eq": "case.priority"}, CTX)  # type: ignore[dict-item]


def test_evaluate__missing_value_entry__raises() -> None:
    with pytest.raises(RuleEvaluationError):
        evaluate({"eq": {"field": "case.priority"}}, CTX)


def test_evaluate__bad_min__raises() -> None:
    with pytest.raises(RuleEvaluationError):
        evaluate({"exists": {"field": "case.tags", "min": 0}}, CTX)
    with pytest.raises(RuleEvaluationError):
        evaluate({"exists": {"field": "case.tags", "min": "many"}}, CTX)


def test_evaluate__in_non_collection__raises() -> None:
    with pytest.raises(RuleEvaluationError):
        evaluate({"in": {"field": "case.score", "value": "x"}}, CTX)


def test_evaluate__ordering_booleans__raises() -> None:
    with pytest.raises(RuleEvaluationError):
        evaluate({"gt": {"field": "case.score", "value": True}}, CTX)


def test_evaluate__ordering_incomparable__raises() -> None:
    with pytest.raises(RuleEvaluationError):
        evaluate({"gt": {"field": "case.title", "value": 1}}, CTX)


def test_evaluate__contains_non_collection__raises() -> None:
    with pytest.raises(RuleEvaluationError):
        evaluate({"contains": {"field": "case.score", "value": 1}}, CTX)


def test_evaluate__regex_non_string_field__raises() -> None:
    with pytest.raises(RuleEvaluationError):
        evaluate({"regex": {"field": "case.score", "pattern": "^4"}}, CTX)


def test_evaluate__regex_invalid_pattern__raises() -> None:
    with pytest.raises(RuleEvaluationError):
        evaluate({"regex": {"field": "case.title", "pattern": "(["}}, CTX)


def test_evaluate__all_empty_clauses__raises() -> None:
    with pytest.raises(RuleEvaluationError):
        evaluate({"all": []}, CTX)
    with pytest.raises(RuleEvaluationError):
        evaluate({"op": "all", "clauses": []}, CTX)


def test_evaluate__deep_nesting__exceeds_cap() -> None:
    cond: Any = {"eq": {"field": "case.priority", "value": "high"}}
    for _ in range(8):
        cond = {"all": [cond]}
    with pytest.raises(RuleEvaluationError):
        evaluate(cond, CTX)


def test_allowed_operators__covers_task_catalog() -> None:
    assert (
        frozenset(
            {
                "all",
                "any",
                "not",
                "eq",
                "ne",
                "in",
                "gt",
                "gte",
                "lt",
                "lte",
                "exists",
                "contains",
                "regex",
            }
        )
        == ALLOWED_OPERATORS
    )
