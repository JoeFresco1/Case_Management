"""Domain tests for ``app.domain.sla`` (P2-US6 / FM-18).

Pure-domain — no I/O, no fakes, no DB. Scenarios covered:

1. **Factory invariants**: ``start`` rejects empty ``clock_key`` and
   ``due_at <= started_at``; produces an ``active`` clock with
   ``paused_at=None``. The legacy ``running`` alias is accepted on
   the wire but the aggregate normalizes to ``active``.
2. **Pause / resume math**: pause freezes the clock, resume
   pushes ``due_at`` forward by the elapsed pause so the worker
   has the same budget remaining. Idempotency on repeated pause
   / resume calls.
3. **Complete**: idempotent on already-``completed``; refuses
   to complete a ``breached`` clock; cleanly closes a paused
   clock (drops ``paused_at``).
4. **Breach**: idempotent on already-``breached``; refuses to
   breach a completed or paused clock; ``is_overdue`` returns
   True only for active clocks whose deadline has passed.
5. **Pause / resume / complete on terminal clocks**: refused
   with ``ValueError`` so a misuse is loud.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.domain.sla import SlaClock, SlaClockStatus

# ---------------------------------------------------------------------------
# Fixed inputs
# ---------------------------------------------------------------------------

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
BETA_TENANT = UUID("22222222-2222-2222-2222-222222222222")
CASE_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

T0 = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _clock(*, hours_until_due: int = 1) -> SlaClock:
    return SlaClock.start(
        tenant_id=ACME_TENANT,
        case_id=CASE_ID,
        clock_key="review_due",
        started_at=T0,
        due_at=T0 + hours_until_due * HOUR,
    )


# ---------------------------------------------------------------------------
# 1. Factory
# ---------------------------------------------------------------------------


def test_start__creates_active_clock_with_defaults() -> None:
    """``start`` returns an active clock with a non-null id,
    ``paused_at=None``, and the supplied timestamps preserved.

    P2-US6 / FM-18: the later SLA lifecycle added a warning tier
    and renamed the counting state from ``running`` to ``active``;
    the legacy ``running`` alias is still accepted on the wire for
    rows persisted by the H0 scanner. The aggregate always
    normalizes to ``active`` on construction.
    """
    clock = _clock()
    assert clock.id != uuid4()
    assert clock.tenant_id == ACME_TENANT
    assert clock.case_id == CASE_ID
    assert clock.clock_key == "review_due"
    assert clock.started_at == T0
    assert clock.due_at == T0 + HOUR
    assert clock.status is SlaClockStatus.ACTIVE
    assert clock.paused_at is None


def test_start__strips_clock_key_whitespace() -> None:
    clock = SlaClock.start(
        tenant_id=ACME_TENANT,
        case_id=CASE_ID,
        clock_key="  review_due  ",
        started_at=T0,
        due_at=T0 + HOUR,
    )
    assert clock.clock_key == "review_due"


def test_start__rejects_empty_clock_key() -> None:
    with pytest.raises(ValueError, match="clock_key is required"):
        SlaClock.start(
            tenant_id=ACME_TENANT,
            case_id=CASE_ID,
            clock_key="",
            started_at=T0,
            due_at=T0 + HOUR,
        )
    with pytest.raises(ValueError, match="clock_key is required"):
        SlaClock.start(
            tenant_id=ACME_TENANT,
            case_id=CASE_ID,
            clock_key="   ",
            started_at=T0,
            due_at=T0 + HOUR,
        )


def test_start__rejects_due_at_not_strictly_after_started_at() -> None:
    with pytest.raises(ValueError, match="due_at must be strictly after"):
        SlaClock.start(
            tenant_id=ACME_TENANT,
            case_id=CASE_ID,
            clock_key="review_due",
            started_at=T0,
            due_at=T0,
        )
    with pytest.raises(ValueError, match="due_at must be strictly after"):
        SlaClock.start(
            tenant_id=ACME_TENANT,
            case_id=CASE_ID,
            clock_key="review_due",
            started_at=T0,
            due_at=T0 - timedelta(seconds=1),
        )


# ---------------------------------------------------------------------------
# 2. Pause / resume
# ---------------------------------------------------------------------------


def test_pause__flips_status_to_paused_and_stamps_paused_at() -> None:
    clock = _clock()
    paused = clock.pause(at=T0 + HOUR)
    assert paused.status is SlaClockStatus.PAUSED
    assert paused.paused_at == T0 + HOUR


def test_pause__is_idempotent() -> None:
    clock = _clock()
    paused = clock.pause(at=T0 + HOUR)
    again = paused.pause(at=T0 + HOUR + timedelta(minutes=5))
    assert again is paused
    assert again.paused_at == T0 + HOUR


def test_pause__refuses_terminal_clock() -> None:
    clock = _clock()
    breached = clock.mark_breached(at=T0 + 2 * HOUR)
    with pytest.raises(ValueError, match="cannot pause a breached clock"):
        breached.pause(at=T0 + 3 * HOUR)
    completed = clock.complete(at=T0 + timedelta(minutes=30))
    with pytest.raises(ValueError, match="cannot pause a completed clock"):
        completed.pause(at=T0 + HOUR)


def test_resume__pushes_due_at_forward_by_pause_duration() -> None:
    clock = _clock(hours_until_due=1)
    paused = clock.pause(at=T0 + timedelta(minutes=30))
    resumed = paused.resume(at=T0 + timedelta(minutes=30) + HOUR)
    assert resumed.status is SlaClockStatus.ACTIVE
    assert resumed.paused_at is None
    assert resumed.due_at == T0 + HOUR + HOUR


def test_resume__is_idempotent_on_active_clock() -> None:
    clock = _clock()
    again = clock.resume(at=T0 + timedelta(minutes=5))
    assert again is clock


def test_resume__refuses_terminal_clock() -> None:
    clock = _clock()
    breached = clock.mark_breached(at=T0 + 2 * HOUR)
    with pytest.raises(ValueError, match="cannot resume a breached clock"):
        breached.resume(at=T0 + 3 * HOUR)
    completed = clock.complete(at=T0 + timedelta(minutes=30))
    with pytest.raises(ValueError, match="cannot resume a completed clock"):
        completed.resume(at=T0 + HOUR)


def test_resume__rejects_clock_before_pause() -> None:
    clock = _clock()
    paused = clock.pause(at=T0 + HOUR)
    with pytest.raises(ValueError, match="before paused_at"):
        paused.resume(at=T0)


# ---------------------------------------------------------------------------
# 3. Complete
# ---------------------------------------------------------------------------


def test_complete__flips_status_to_completed() -> None:
    clock = _clock()
    completed = clock.complete(at=T0 + timedelta(minutes=30))
    assert completed.status is SlaClockStatus.COMPLETED


def test_complete__is_idempotent() -> None:
    clock = _clock()
    completed = clock.complete(at=T0 + timedelta(minutes=30))
    again = completed.complete(at=T0 + timedelta(minutes=45))
    assert again is completed


def test_complete__refuses_breached_clock() -> None:
    clock = _clock()
    breached = clock.mark_breached(at=T0 + 2 * HOUR)
    with pytest.raises(ValueError, match="cannot complete a breached clock"):
        breached.complete(at=T0 + 3 * HOUR)


def test_complete__drops_paused_at_when_closing_from_paused() -> None:
    clock = _clock()
    paused = clock.pause(at=T0 + HOUR)
    completed = paused.complete(at=T0 + 2 * HOUR)
    assert completed.status is SlaClockStatus.COMPLETED
    assert completed.paused_at is None


# ---------------------------------------------------------------------------
# 4. Breach + is_overdue
# ---------------------------------------------------------------------------


def test_breach__flips_status_to_breached() -> None:
    clock = _clock(hours_until_due=1)
    breached = clock.mark_breached(at=T0 + 2 * HOUR)
    assert breached.status is SlaClockStatus.BREACHED


def test_breach__is_idempotent() -> None:
    clock = _clock(hours_until_due=1)
    first = clock.mark_breached(at=T0 + 2 * HOUR)
    second = first.mark_breached(at=T0 + 3 * HOUR)
    assert second is first


def test_breach__refuses_completed_clock() -> None:
    clock = _clock()
    completed = clock.complete(at=T0 + timedelta(minutes=30))
    with pytest.raises(ValueError, match="cannot breach a completed clock"):
        completed.mark_breached(at=T0 + 2 * HOUR)


def test_breach__refuses_paused_clock() -> None:
    clock = _clock()
    paused = clock.pause(at=T0 + HOUR)
    with pytest.raises(ValueError, match="cannot breach a paused clock"):
        paused.mark_breached(at=T0 + 2 * HOUR)


def test_is_overdue__true_for_active_past_due() -> None:
    clock = _clock(hours_until_due=1)
    assert clock.is_overdue(at=T0) is False
    assert clock.is_overdue(at=T0 + HOUR) is False
    assert clock.is_overdue(at=T0 + HOUR + timedelta(seconds=1)) is True


def test_is_overdue__false_for_paused_clock_past_due() -> None:
    clock = _clock(hours_until_due=1)
    paused = clock.pause(at=T0 + timedelta(minutes=30))
    assert paused.is_overdue(at=T0 + 2 * HOUR) is False


def test_is_overdue__false_for_terminal_clock() -> None:
    clock = _clock(hours_until_due=1)
    breached = clock.mark_breached(at=T0 + 2 * HOUR)
    assert breached.is_overdue(at=T0 + 3 * HOUR) is False
    completed = clock.complete(at=T0 + timedelta(minutes=15))
    assert completed.is_overdue(at=T0 + 3 * HOUR) is False


# ---------------------------------------------------------------------------
# 5. Warning tier
# ---------------------------------------------------------------------------


def test_mark_warning__flips_active_to_warning() -> None:
    """A counting clock inside its warning window flips to
    ``warning``. The transition is the first half of the
    FM-18 acceptance: worker sees deadline approaching before
    the breach.
    """
    clock = SlaClock.start(
        tenant_id=ACME_TENANT,
        case_id=CASE_ID,
        clock_key="review_due",
        started_at=T0,
        due_at=T0 + 48 * HOUR,
        warning_at=T0 + 42 * HOUR,
    )
    warned = clock.mark_warning(at=T0 + 45 * HOUR)
    assert warned.status is SlaClockStatus.WARNING


def test_mark_warning__is_idempotent() -> None:
    clock = SlaClock.start(
        tenant_id=ACME_TENANT,
        case_id=CASE_ID,
        clock_key="review_due",
        started_at=T0,
        due_at=T0 + 48 * HOUR,
        warning_at=T0 + 42 * HOUR,
    )
    warned = clock.mark_warning(at=T0 + 45 * HOUR)
    again = warned.mark_warning(at=T0 + 46 * HOUR)
    assert again is warned


def test_mark_warning__refuses_terminal_clock() -> None:
    clock = SlaClock.start(
        tenant_id=ACME_TENANT,
        case_id=CASE_ID,
        clock_key="review_due",
        started_at=T0,
        due_at=T0 + 48 * HOUR,
        warning_at=T0 + 42 * HOUR,
    )
    breached = clock.mark_breached(at=T0 + 49 * HOUR)
    with pytest.raises(ValueError, match="cannot warn a breached clock"):
        breached.mark_warning(at=T0 + 50 * HOUR)
    completed = clock.complete(at=T0 + HOUR)
    with pytest.raises(ValueError, match="cannot warn a completed clock"):
        completed.mark_warning(at=T0 + 2 * HOUR)


def test_mark_warning__refuses_paused_clock() -> None:
    clock = SlaClock.start(
        tenant_id=ACME_TENANT,
        case_id=CASE_ID,
        clock_key="review_due",
        started_at=T0,
        due_at=T0 + 48 * HOUR,
        warning_at=T0 + 42 * HOUR,
    )
    paused = clock.pause(at=T0 + 30 * HOUR)
    with pytest.raises(ValueError, match="cannot warn a paused clock"):
        paused.mark_warning(at=T0 + 45 * HOUR)


def test_is_in_warning_window__true_inside_window() -> None:
    """``is_in_warning_window`` is the scanner's predicate for the
    warning pass. It returns True for ``[warning_at, due_at)``.
    """
    clock = SlaClock.start(
        tenant_id=ACME_TENANT,
        case_id=CASE_ID,
        clock_key="review_due",
        started_at=T0,
        due_at=T0 + 48 * HOUR,
        warning_at=T0 + 42 * HOUR,
    )
    assert clock.is_in_warning_window(at=T0 + 41 * HOUR) is False
    assert clock.is_in_warning_window(at=T0 + 42 * HOUR) is True
    assert clock.is_in_warning_window(at=T0 + 47 * HOUR) is True
    assert clock.is_in_warning_window(at=T0 + 48 * HOUR) is False
    assert clock.is_in_warning_window(at=T0 + 49 * HOUR) is False


def test_is_in_warning_window__false_without_warning_at() -> None:
    clock = SlaClock.start(
        tenant_id=ACME_TENANT,
        case_id=CASE_ID,
        clock_key="review_due",
        started_at=T0,
        due_at=T0 + 48 * HOUR,
        warning_at=None,
    )
    assert clock.is_in_warning_window(at=T0 + 47 * HOUR) is False


def test_start__rejects_warning_at_outside_window() -> None:
    """A warning offset outside ``(started_at, due_at]`` is
    nonsensical — the window must lie inside the clock's life.
    """
    with pytest.raises(ValueError, match="warning_at"):
        SlaClock.start(
            tenant_id=ACME_TENANT,
            case_id=CASE_ID,
            clock_key="review_due",
            started_at=T0,
            due_at=T0 + 48 * HOUR,
            warning_at=T0,
        )
    with pytest.raises(ValueError, match="warning_at"):
        SlaClock.start(
            tenant_id=ACME_TENANT,
            case_id=CASE_ID,
            clock_key="review_due",
            started_at=T0,
            due_at=T0 + 48 * HOUR,
            warning_at=T0 + 49 * HOUR,
        )


def test_is_overdue__true_for_warning_clock_past_due() -> None:
    """A warning clock that crosses the deadline is overdue; the
    scanner flips it to ``breached`` on the next tick.
    """
    clock = SlaClock.start(
        tenant_id=ACME_TENANT,
        case_id=CASE_ID,
        clock_key="review_due",
        started_at=T0,
        due_at=T0 + 48 * HOUR,
        warning_at=T0 + 42 * HOUR,
    )
    warned = clock.mark_warning(at=T0 + 45 * HOUR)
    assert warned.is_overdue(at=T0 + 49 * HOUR) is True
