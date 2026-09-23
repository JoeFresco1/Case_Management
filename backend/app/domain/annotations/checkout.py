"""``CheckoutLock`` and ``CheckoutConflict`` — persistent exclusive checkout (T5 / FM-023 ext).

At most one *active* (non-expired, non-released) checkout exists per
document. The lock carries the holder plus a bounded lease so a crashed
client cannot wedge a document forever; a conflicting acquire raises
:class:`CheckoutConflict`, which carries the current holder + expiry so
the HTTP boundary can render the structured ``conflict`` envelope without
probing lock state (T5 acceptance: "checkout/lock conflicts return
structured ``conflict`` with holder + expiry"). Lease bounds are enforced
in the pure domain (``_validate_lease``), never by the caller.

Checkout *history* is the append-only sequence of lock rows; the
repository lists the trail. All time comes from an explicit ``now``
argument threaded by the application layer from the ``Clock`` port (R11).

``# INTEGRATION_SEAM``: persistence goes through the ``CheckoutLockRepository``
port (``app.domain.ports.viewer``); local Postgres adapter today, Azure SQL
behind the same Protocol.

Symbols: ``CheckoutConflict``, ``CheckoutLock``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from app.domain.errors import Conflict, InvariantViolation, ValidationFailed

from .constants import DEFAULT_CHECKOUT_LEASE_SECONDS
from .helpers import _validate_lease


class CheckoutConflict(Conflict):
    """A checkout acquire/renew lost to a live lock held by someone else.

    Carries the current ``holder`` plus ``expires_at`` so the HTTP
    boundary can render the T5 structured ``conflict`` envelope
    (holder + expiry) without the client probing lock state. Never
    carries document bytes or PHI — holder/expiry are safe metadata.
    """

    def __init__(self, *, document_id: UUID, holder: UUID, expires_at: datetime) -> None:
        super().__init__(
            f"document {document_id} is checked out by {holder} until {expires_at.isoformat()}"
        )
        self.document_id = document_id
        self.holder = holder
        self.expires_at = expires_at

    def to_details(self) -> dict[str, object]:
        """Render the structured conflict details for the HTTP envelope."""
        return {
            "reason": "checkout_conflict",
            "document_id": str(self.document_id),
            "holder": str(self.holder),
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class CheckoutLock:
    """A persistent exclusive checkout of a document (T5 / FM-023 ext).

    At most one *active* (non-expired, non-released) lock exists per
    document. The lock carries the ``holder_user_id`` and ``expires_at``
    lease so a crashed client cannot wedge a document forever: once
    ``expires_at`` passes, any user may take over (the takeover is
    audited as a distinct ``CheckoutTakeover`` event by the use case).

    Checkout *history* is the append-only sequence of these rows; this
    aggregate shapes one row, the repository lists the trail.
    """

    id: UUID
    tenant_id: UUID
    document_id: UUID
    holder_user_id: UUID
    acquired_at: datetime
    expires_at: datetime
    released_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate lease shape (pure, no I/O)."""
        if self.expires_at <= self.acquired_at:
            raise ValidationFailed("expires_at must be after acquired_at", field="expires_at")

    @staticmethod
    def acquire(
        *,
        tenant_id: UUID,
        document_id: UUID,
        holder_user_id: UUID,
        now: datetime,
        lease_seconds: int = DEFAULT_CHECKOUT_LEASE_SECONDS,
    ) -> CheckoutLock:
        """Mint a fresh checkout lock for ``holder_user_id``.

        The caller (use case) checks for a live conflicting lock first
        via :meth:`check_acquire` and raises :class:`CheckoutConflict`
        carrying holder + expiry. Lease bounds are enforced here so no
        caller can mint a zero-length or multi-year lock.
        """
        lease = _validate_lease(lease_seconds)
        return CheckoutLock(
            id=uuid4(),
            tenant_id=tenant_id,
            document_id=document_id,
            holder_user_id=holder_user_id,
            acquired_at=now,
            expires_at=now + timedelta(seconds=lease),
        )

    def is_live(self, *, now: datetime) -> bool:
        """True while the lock blocks other holders.

        A lock is live iff it is unreleased and ``now`` is strictly
        before ``expires_at``. Expiry is exclusive so a lock expiring
        exactly at ``now`` is already takeable (no fencepost wedge).
        """
        return self.released_at is None and now < self.expires_at

    def check_acquire(self, *, requester_id: UUID, now: datetime) -> None:
        """Refuse a conflicting acquire, or return ``None`` when free.

        Pure rule behind the T5 acceptance "checkout/lock conflicts
        return structured ``conflict`` with holder + expiry":

        - No live lock (released or expired) -> allowed (returns None);
          the caller mints a fresh row (takeover after expiry is a new
          row, preserving history).
        - Live lock held by the *same* user -> allowed (idempotent
          re-acquire; the caller renews instead of duplicating).
        - Live lock held by *another* user -> raise
          :class:`CheckoutConflict` carrying holder + expiry.

        Args:
            requester_id: User attempting the checkout.
            now: Instant the conflict is evaluated at.

        Raises:
            CheckoutConflict: A live lock by another holder blocks this.
        """
        if not self.is_live(now=now):
            return
        if self.holder_user_id == requester_id:
            return
        raise CheckoutConflict(
            document_id=self.document_id,
            holder=self.holder_user_id,
            expires_at=self.expires_at,
        )

    def renew(self, *, requester_id: UUID, now: datetime, lease_seconds: int) -> CheckoutLock:
        """Extend this lock's lease; only the holder may renew.

        Renewing an expired or released lock raises
        :class:`InvariantViolation` (the caller must acquire fresh so
        history shows the gap). Renewing as a non-holder raises
        :class:`CheckoutConflict` with the true holder + expiry so the
        caller learns whom to ask, not just "denied".
        """
        if self.released_at is not None:
            raise InvariantViolation(f"checkout {self.id} is released and cannot be renewed")
        if now >= self.expires_at:
            raise InvariantViolation(
                f"checkout {self.id} expired at {self.expires_at.isoformat()}; acquire fresh"
            )
        if requester_id != self.holder_user_id:
            raise CheckoutConflict(
                document_id=self.document_id,
                holder=self.holder_user_id,
                expires_at=self.expires_at,
            )
        lease = _validate_lease(lease_seconds)
        return replace(self, expires_at=now + timedelta(seconds=lease))

    def release(self, *, requester_id: UUID, now: datetime) -> CheckoutLock:
        """Release this lock; only the holder may release.

        Release is idempotent-safe at the use-case level (releasing an
        already-released row raises :class:`InvariantViolation` so a
        double-release is a loud programmer error, not a silent no-op).
        A non-holder release raises :class:`CheckoutConflict` carrying
        the true holder + expiry.
        """
        if self.released_at is not None:
            raise InvariantViolation(f"checkout {self.id} is already released")
        if requester_id != self.holder_user_id:
            raise CheckoutConflict(
                document_id=self.document_id,
                holder=self.holder_user_id,
                expires_at=self.expires_at,
            )
        return replace(self, released_at=now)


__all__ = [
    "CheckoutConflict",
    "CheckoutLock",
    "Conflict",
    "DEFAULT_CHECKOUT_LEASE_SECONDS",
    "InvariantViolation",
    "UUID",
    "ValidationFailed",
    "dataclass",
    "datetime",
    "replace",
    "timedelta",
    "uuid4",
]
