"""Project a case's SLA clocks into the case-summary ``sla_state`` shape.

The summary builder (``app.application.cases.summary.build_summary``)
calls :func:`summarize_sla_state` once per case to render the
``sla_state`` field the workspace UI renders as
``<SlaBadge />`` (P02-US6 / FM-018).

The projection is pure: it takes the case's live clocks and a
``now`` timestamp and returns a single :class:`SlaStateSummary`
value object. The repository / ORM hop lives in the router so the
domain layer stays free of SQLAlchemy imports; the projection
itself is a pure function over a list of :class:`SlaClock`.

Tiers (worst-first):

- ``breached`` — at least one clock has crossed ``due_at`` without
  being completed. The case is past a deadline; the timeline shows
  the breach and the configured escalation.
- ``warning`` — no clock has breached, but at least one is inside
  its ``warning_at..due_at`` window. The case is approaching a
  deadline.
- ``active`` — at least one clock is counting down well ahead of
  its deadline. The case has live work being measured.
- ``paused`` — every counting clock is paused. The case has work in
  flight but the clocks are frozen.
- ``completed`` — every clock reached its terminal success state.
  The case beat every deadline.
- ``none`` — the case has no clocks at all (P10 / H0 default).

The next deadline is the soonest ``due_at`` across the clock set
the projection considered; ``breached`` clocks show the past
deadline so the operator sees when the case went overdue.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.domain.sla import SlaClock, SlaClockStatus


@dataclass(frozen=True, slots=True)
class SlaStateSummary:
    """The canonical SLA state projection for a case.

    Attributes:
        state: The worst tier across the case's clocks. One of
            ``"breached"``, ``"warning"``, ``"active"``, ``"paused"``,
            ``"completed"``, ``"none"``.
        next_due_at: The soonest ``due_at`` across the clocks that
            drove the worst tier. ``None`` when the case has no
            clocks or every clock is in a terminal state.
        breached_count: How many clocks are in ``breached`` state.
        warning_count: How many clocks are in ``warning`` state.
        active_count: How many clocks are in ``active`` state.
        paused_count: How many clocks are in ``paused`` state.
        completed_count: How many clocks are in ``completed`` state.
        clock_keys: The stable keys of the clocks that drove the
            worst tier (so the UI can show "review_due at 14:00").
    """

    state: str
    next_due_at: datetime | None
    breached_count: int
    warning_count: int
    active_count: int
    paused_count: int
    completed_count: int
    clock_keys: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire shape used by ``sla_state``.

        Mirrors the Pydantic ``SlaStateView`` schema so the
        application layer can emit the same JSON without taking a
        Pydantic dependency.
        """
        return {
            "state": self.state,
            "next_due_at": (self.next_due_at.isoformat() if self.next_due_at else None),
            "breached_count": self.breached_count,
            "warning_count": self.warning_count,
            "active_count": self.active_count,
            "paused_count": self.paused_count,
            "completed_count": self.completed_count,
            "clock_keys": list(self.clock_keys),
        }


def summarize_sla_state(clocks: Iterable[SlaClock], *, now: datetime) -> SlaStateSummary:
    """Project a list of :class:`SlaClock` into an :class:`SlaStateSummary`.

    The function is deterministic and pure; the caller supplies
    ``now`` so tests can pin the timeline. The function never
    inspects ``paused_at`` or any other field beyond the status and
    ``due_at`` — the scanner is the writer that flips status, the
    projection just reads. ``now`` is accepted for forward
    compatibility (a future tier may project "active-and-closing"
    differently than "active-and-early") and is intentionally
    unused today.
    """
    _ = now
    breached: list[SlaClock] = []
    warning: list[SlaClock] = []
    active: list[SlaClock] = []
    paused: list[SlaClock] = []
    completed: list[SlaClock] = []
    for clock in clocks:
        status = _coerce(clock.status)
        if status is SlaClockStatus.BREACHED:
            breached.append(clock)
        elif status is SlaClockStatus.WARNING:
            warning.append(clock)
        elif status is SlaClockStatus.PAUSED:
            paused.append(clock)
        elif status is SlaClockStatus.COMPLETED:
            completed.append(clock)
        else:
            # ACTIVE or the legacy RUNNING alias.
            active.append(clock)
    if breached:
        return SlaStateSummary(
            state="breached",
            next_due_at=_earliest(breached),
            breached_count=len(breached),
            warning_count=len(warning),
            active_count=len(active),
            paused_count=len(paused),
            completed_count=len(completed),
            clock_keys=tuple(c.clock_key for c in breached),
        )
    if warning:
        return SlaStateSummary(
            state="warning",
            next_due_at=_earliest(warning),
            breached_count=0,
            warning_count=len(warning),
            active_count=len(active),
            paused_count=len(paused),
            completed_count=len(completed),
            clock_keys=tuple(c.clock_key for c in warning),
        )
    if active:
        return SlaStateSummary(
            state="active",
            next_due_at=_earliest(active),
            breached_count=0,
            warning_count=0,
            active_count=len(active),
            paused_count=len(paused),
            completed_count=len(completed),
            clock_keys=tuple(c.clock_key for c in active),
        )
    if paused:
        return SlaStateSummary(
            state="paused",
            next_due_at=_earliest(paused),
            breached_count=0,
            warning_count=0,
            active_count=0,
            paused_count=len(paused),
            completed_count=len(completed),
            clock_keys=tuple(c.clock_key for c in paused),
        )
    if completed:
        return SlaStateSummary(
            state="completed",
            next_due_at=None,
            breached_count=0,
            warning_count=0,
            active_count=0,
            paused_count=0,
            completed_count=len(completed),
            clock_keys=tuple(c.clock_key for c in completed),
        )
    return SlaStateSummary(
        state="none",
        next_due_at=None,
        breached_count=0,
        warning_count=0,
        active_count=0,
        paused_count=0,
        completed_count=0,
        clock_keys=(),
    )


def _coerce(status: SlaClockStatus) -> SlaClockStatus:
    """Map the legacy ``running`` alias to ``active``.

    The wire format accepts both for backwards compatibility with
    rows persisted by the H0 scanner; the projection treats them
    identically. The aggregate's :func:`start` already normalizes
    on construction; this guard covers clocks that were
    deserialized directly from the database.
    """
    if status is SlaClockStatus.RUNNING:
        return SlaClockStatus.ACTIVE
    return status


def _earliest(clocks: list[SlaClock]) -> datetime | None:
    if not clocks:
        return None
    return min(c.due_at for c in clocks)


__all__ = ["SlaStateSummary", "summarize_sla_state"]
