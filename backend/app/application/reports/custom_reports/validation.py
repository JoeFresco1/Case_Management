"""Pure closed-vocabulary validator for custom-report specs (P12-US7 / FM-053).

No I/O, no clock, no actor context — the rules are small but
interconnected (operator must match field type; value must match
operator), so they are collected in one place to keep the public
surface (``define`` and ``render``) free of duplicated checks. Split out
of the former single-file ``custom_reports.py`` during the R2 package
split.
"""

from __future__ import annotations

from uuid import UUID

from app.domain.errors import ValidationFailed

from .shapes import DefineCustomReportCommand, FilterClause
from .vocabulary import (
    _FILTERABLE_FIELDS,
    _OPERATORS_FOR_VALUE,
    AggregationKind,
    FilterOperator,
    GroupByField,
)


def _validate_command(cmd: DefineCustomReportCommand) -> None:  # noqa: PLR0912
    """Run the closed-vocabulary validator.

    Why a single function: the rules are small but interconnected
    (operator must match field type; value must match operator).
    Collecting them in one place keeps the public surface (``define``
    and ``render``) free of duplicated checks. Returns nothing on
    success; raises ``ValidationFailed`` on the first violation.
    """
    if not cmd.key or not cmd.key.strip():
        raise ValidationFailed("definition.key is required", field="key")
    if not cmd.name or not cmd.name.strip():
        raise ValidationFailed("definition.name is required", field="name")
    if not cmd.group_by:
        raise ValidationFailed(
            "definition.group_by must contain at least one field",
            field="group_by",
        )
    if not cmd.aggregations:
        raise ValidationFailed(
            "definition.aggregations must contain at least one kind",
            field="aggregations",
        )
    for field_kind in cmd.group_by:
        if not isinstance(field_kind, GroupByField):
            raise ValidationFailed(
                f"unknown group_by field: {field_kind!r}",
                field="group_by",
            )
    for agg in cmd.aggregations:
        if not isinstance(agg, AggregationKind):
            raise ValidationFailed(
                f"unknown aggregation: {agg!r}",
                field="aggregations",
            )
    if cmd.time_window_hours is not None and (
        cmd.time_window_hours <= 0 or cmd.time_window_hours > 24 * 365
    ):
        # A practical upper bound — a year. Longer windows belong to
        # a future analytics surface with materialized views.
        raise ValidationFailed(
            "time_window_hours must be in [1, 8760] when set",
            field="time_window_hours",
        )
    for clause in cmd.filters:
        if clause.field not in _FILTERABLE_FIELDS:
            raise ValidationFailed(
                f"unknown filter field: {clause.field!r}",
                field="filters",
            )
        allowed_ops = _OPERATORS_FOR_VALUE[clause.field]
        if clause.op not in allowed_ops:
            raise ValidationFailed(
                f"operator {clause.op.value!r} is not allowed on {clause.field!r}",
                field="filters",
            )
        _validate_clause_value(clause)


def _validate_clause_value(clause: FilterClause) -> None:
    """Check that ``clause.value`` matches ``clause.op``.

    The validator only enforces the structural shape; the runtime
    evaluation in :func:`~.render._match_clause` is the semantic check.
    Keeping the two separate lets us reuse the validator at definition
    time without binding to the case / task repositories.
    """
    if clause.op is FilterOperator.IN:
        if not isinstance(clause.value, tuple) or not clause.value:
            raise ValidationFailed(
                f"operator 'in' requires a non-empty tuple value on {clause.field!r}",
                field="filters",
            )
        for entry in clause.value:
            if not isinstance(entry, (str, UUID)):
                raise ValidationFailed(
                    "operator 'in' accepts only string or UUID entries, "
                    f"got {type(entry).__name__}",
                    field="filters",
                )
        return
    scalar_ops = {
        FilterOperator.EQ,
        FilterOperator.NE,
        FilterOperator.GT,
        FilterOperator.GTE,
        FilterOperator.LT,
        FilterOperator.LTE,
    }
    if clause.op in scalar_ops and not isinstance(clause.value, (str, int, UUID)):
        raise ValidationFailed(
            f"operator {clause.op.value!r} requires a scalar value on {clause.field!r}",
            field="filters",
        )


__all__ = [
    "_validate_clause_value",
    "_validate_command",
]
