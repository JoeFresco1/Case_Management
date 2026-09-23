"""Concurrent-edit (keyword) lock — pure rule over the active checkout (T5).

:func:`evaluate_edit_lock` decides whether a requester may mutate
document metadata/bytes given the active checkout and the clock. It is
*total* (never raises for domain states) so the use case can branch on
the :class:`EditLockDecision` and audit both allows and denials: no
active/released/expired lock -> allowed, own checkout -> allowed,
foreign live checkout -> denied with holder + expiry for the structured
``conflict`` envelope. All time comes from an explicit ``now`` (R11 —
the application layer threads the ``Clock`` port).

Symbols: ``EditLockDecision``, ``evaluate_edit_lock``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from .checkout import CheckoutLock


@dataclass(frozen=True, slots=True)
class EditLockDecision:
    """Outcome of :func:`evaluate_edit_lock` (pure value, no I/O).

    ``allowed`` is the verdict; ``holder``/``expires_at`` are populated
    exactly when denied by a live foreign checkout so the caller can
    render the same structured conflict without a second lookup.
    """

    allowed: bool
    holder: UUID | None = None
    expires_at: datetime | None = None
    reason: str = ""


def evaluate_edit_lock(
    *,
    active_lock: CheckoutLock | None,
    requester_id: UUID,
    now: datetime,
) -> EditLockDecision:
    """Decide whether ``requester_id`` may mutate document content (pure).

    Concurrent-edit rule (T5 "concurrent-edit (keyword) locking"):

    - No active lock row, or a released/expired row -> allowed (free).
    - Live lock held by the requester -> allowed (own checkout).
    - Live lock held by another user -> denied, carrying the holder +
      expiry so the HTTP boundary renders ``conflict``.

    The function is total (never raises for domain states) so the use
    case can branch on the decision and audit both allows and denials.
    """
    if active_lock is None or not active_lock.is_live(now=now):
        return EditLockDecision(allowed=True, reason="no_live_checkout")
    if active_lock.holder_user_id == requester_id:
        return EditLockDecision(allowed=True, reason="own_checkout")
    return EditLockDecision(
        allowed=False,
        holder=active_lock.holder_user_id,
        expires_at=active_lock.expires_at,
        reason="foreign_checkout",
    )


__all__ = [
    "CheckoutLock",
    "EditLockDecision",
    "UUID",
    "dataclass",
    "datetime",
    "evaluate_edit_lock",
]
