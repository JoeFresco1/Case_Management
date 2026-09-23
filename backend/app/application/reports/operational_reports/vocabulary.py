"""Report source marker and kind vocabulary for operational reports (P12 §1).

Split out of the former single-file ``operational_reports.py`` during the
R2 package split; no behavior lives here — only the canonical
:data:`REPORT_SOURCE` string every report projects onto the wire and the
closed :class:`ReportKind` enum that distinguishes point-in-time
snapshots from historical aggregates.
"""

from __future__ import annotations

from enum import StrEnum

REPORT_SOURCE = "system-of-record"
"""Canonical source marker for every operational report (P12 §1).

Why a constant: the pillar requires every report to identify the
system it was sourced from. All operational reports read the live
system of record (never a cache or warehouse), so the wire can
answer "where did this number come from" with one stable string. A
future Synapse / Power BI engine changes this single constant.
"""


class ReportKind(StrEnum):
    """The kind of report — snapshot vs aggregate.

    P12 pillar statement (§1) requires every report to declare its
    kind. Snapshots are point-in-time reads of **current state**
    (open cases, overdue tasks, failed work). Aggregates are counts
    over a **historical window** (custom reports with
    ``time_window_hours`` set, FM-053). The two are never mixed: a
    snapshot carries ``is_historical=False`` and no window, an
    aggregate carries ``is_historical=True`` and an explicit
    ``[window_start, window_end]`` pair so a consumer can tell
    "as of now" apart from "over the last N hours".
    """

    SNAPSHOT = "snapshot"
    AGGREGATE = "aggregate"


__all__ = ["REPORT_SOURCE", "ReportKind"]
