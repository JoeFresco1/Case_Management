"""Retention-clock pause-and-resume helpers (HoldPeriod accounting)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.errors import ValidationFailed
from app.domain.retention.policy import RetentionPolicy

# ---------------------------------------------------------------------------
# Retention-clock pause semantics (002-leftovers spec §9 Q5 gate decision).
# ---------------------------------------------------------------------------
#
# Gate decision (T6 Run, "attached retention-clock decision"): **pause and
# resume**. While a case is under active legal hold the disposition clock
# is *frozen* — hold time does not count toward the retention horizon.
# On release the clock *resumes* from where it froze; the horizon shifts
# forward by the total hold duration. The rejected alternative was
# reset-on-release (restart the full retention period at release), which
# would silently extend retention beyond the published policy and make
# the evidence report unable to replay the horizon from policy + holds.
#
# Why explicit ``HoldPeriod`` rows (not a LegalHold import): the hold
# aggregate lives in :mod:`app.domain.legal_hold` and the retention
# aggregate must not import it (cross-aggregate invariants belong at the
# application boundary — see module docstring). The application layer
# translates holds into ``HoldPeriod`` rows via
# ``LegalHoldRepository.history_for_case`` and calls the pure helpers
# below. No I/O here; all functions are deterministic and tz-aware
# (naive datetimes raise — the platform contract is "UTC from Clock").


@dataclass(frozen=True, slots=True)
class HoldPeriod:
    """One frozen-clock interval for pause-and-resume accounting.

    Attributes:
        started_at: When the hold was placed (clock froze here).
        ended_at: When the hold was released, or ``None`` while the
            hold is still active (the freeze extends to ``now``).
    """

    started_at: datetime
    ended_at: datetime | None = None


def _require_aware(*moments: datetime) -> None:
    """Raise on naive datetimes (fail-closed UTC discipline).

    Mixing naive and aware datetimes raises ``TypeError`` at comparison
    time — a runtime crash on legal inputs. Rejecting early keeps the
    pause math total (never half-compared).
    """
    for moment in moments:
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValidationFailed(
                "retention-clock moments must be timezone-aware (UTC)",
                field="at",
            )


def total_hold_pause(
    *,
    periods: list[HoldPeriod],
    now: datetime,
) -> timedelta:
    """Return the total frozen-clock duration across ``periods``.

    An open period (``ended_at is None``) accrues up to ``now``. A
    period ending in the future is clamped to ``now`` so clock-skew
    cannot manufacture negative eligibility. Overlapping periods are
    summed as-given — the repository guarantees at most one active
    hold per case, so overlaps indicate a caller bug the scan
    surfaces rather than hides.
    """
    _require_aware(now, *(p.started_at for p in periods))
    _require_aware(*(p.ended_at for p in periods if p.ended_at is not None))
    total = timedelta(0)
    for period in periods:
        end = period.ended_at if period.ended_at is not None else now
        end = min(end, now)
        if end > period.started_at:
            total += end - period.started_at
    return total


def effective_due_at(
    *,
    case_created_at: datetime,
    policy: RetentionPolicy,
    periods: list[HoldPeriod] | None = None,
    now: datetime,
) -> datetime:
    """Return the hold-adjusted disposition horizon for a case.

    ``due_at = case_created_at + policy.period + total_hold_pause``.
    With no holds the result equals the legacy ``created_at + period``
    so existing callers (eligibility list, route, evidence report)
    observe identical horizons until holds exist.
    """
    _require_aware(case_created_at, now)
    pause = total_hold_pause(periods=periods or [], now=now)
    return case_created_at + policy.period + pause


def is_eligible_with_hold_pause(
    *,
    case_created_at: datetime,
    policy: RetentionPolicy,
    now: datetime,
    legal_hold_active: bool,
    periods: list[HoldPeriod] | None = None,
) -> bool:
    """Return True iff the case is eligible under pause-and-resume.

    The rule extends :func:`is_eligible_for_disposition`:

    - An active hold still blocks immediately (frozen clock — the
      case cannot become eligible *while* held, regardless of age).
    - Otherwise eligibility is evaluated against the hold-adjusted
      horizon: ``now >= case_created_at + period + total_pause``.
      Released-hold time shifts the horizon forward instead of
      counting toward it (resume, not reset).
    - The policy must still be active at ``now``.
    """
    if legal_hold_active:
        return False
    if not policy.is_active_at(at=now):
        return False
    return now >= effective_due_at(
        case_created_at=case_created_at,
        policy=policy,
        periods=periods,
        now=now,
    )
