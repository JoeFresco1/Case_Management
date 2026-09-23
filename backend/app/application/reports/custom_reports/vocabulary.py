"""Closed vocabulary for the custom-report designer (P12-US7 / FM-053).

What "controlled" means: the spec is a closed vocabulary — the validator
rejects any field that is not on the allowlist, any operator that is not
on the operator allowlist, and any aggregation that is not on the
aggregation allowlist. The closed lists live here so a future schema
extension is a one-file change. The validator itself lives in
:mod:`.validation` and is pure: no I/O, no clock, no actor context — it
can be reused by the simulator (P02-US5) when a "simulate report"
surface lands.

Split out of the former single-file ``custom_reports.py`` during the R2
package split; no behavior lives here — only the enum types and the
operator/field allowlists.
"""

from __future__ import annotations

from enum import StrEnum


class GroupByField(StrEnum):
    """The fields the custom report is allowed to group by.

    Closed list: extending it is a one-line change here AND a
    corresponding ``_GROUP_BY_PROJECTORS`` entry in :mod:`.render`.
    Anything not on the list is rejected as ``unknown group_by field``.
    """

    CASE_STATUS = "case.status"
    CASE_PRIORITY = "case.priority"
    QUEUE_KEY = "case.queue"
    WORKFLOW_STATE = "case.workflow_state"
    ASSIGNEE_USER = "task.assignee_user"
    ASSIGNEE_QUEUE = "task.assignee_queue"


class AggregationKind(StrEnum):
    """The aggregations a custom report is allowed to compute.

    Closed list: extending it requires a renderer in :mod:`.render`. The
    list is deliberately small: a custom report that surfaces sensitive
    payloads would require a different audit trail; v1 surfaces only
    counts and overdue-window sums that the operational read models
    already produce.
    """

    COUNT = "count"
    OVERDUE_SUM = "overdue_sum"


class FilterOperator(StrEnum):
    """Filter operators the custom report accepts.

    The list mirrors the rule evaluator's safe subset
    (AGENTS.md §16.2 — operators ``all/any/not/eq/ne/in/gt/gte/lt/lte/
    exists/contains/regex``). Nested boolean composition (``all/``any/
    ``not``) is exposed via :class:`~.shapes.FilterClause` so the schema
    can express "status == 'open' AND (priority == 'high' OR priority ==
    'urgent')" without inventing a new operator set.
    """

    EQ = "eq"
    NE = "ne"
    IN = "in"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"


# Allowed operators per field type. ``case.priority`` is the
# PillarP01 enum; ``task.due_at`` is a timestamp. The validator
# refuses any operator that does not match the field's type so a
# ``gt`` on a string field is rejected up front.
_OPERATORS_FOR_VALUE: dict[str, frozenset[FilterOperator]] = {
    "case.status": frozenset({FilterOperator.EQ, FilterOperator.NE, FilterOperator.IN}),
    "case.priority": frozenset({FilterOperator.EQ, FilterOperator.NE, FilterOperator.IN}),
    "case.queue": frozenset({FilterOperator.EQ, FilterOperator.NE, FilterOperator.IN}),
    "task.assignee_user": frozenset({FilterOperator.EQ, FilterOperator.NE}),
    "task.assignee_queue": frozenset({FilterOperator.EQ, FilterOperator.NE, FilterOperator.IN}),
    "task.due_at": frozenset(
        {
            FilterOperator.GT,
            FilterOperator.GTE,
            FilterOperator.LT,
            FilterOperator.LTE,
        }
    ),
    "task.due_overdue": frozenset({FilterOperator.EQ}),
}


# The set of fields the custom report is allowed to filter on — the
# input contract. The ``_GROUP_BY_PROJECTORS`` dict in :mod:`.render`
# is the projection layer.
_FILTERABLE_FIELDS: frozenset[str] = frozenset(_OPERATORS_FOR_VALUE)


__all__ = [
    "AggregationKind",
    "FilterOperator",
    "GroupByField",
    "_FILTERABLE_FIELDS",
    "_OPERATORS_FOR_VALUE",
]
