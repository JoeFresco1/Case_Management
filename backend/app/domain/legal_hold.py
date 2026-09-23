"""LegalHold aggregate (P08-US1, US2 / FM-039 / FR-038).

A legal hold is a security administrator's directive that prevents
disposition of a case, its documents, and its audit chain while the
hold is active. H0 of the slice shipped a single ``legal_hold`` boolean
flag on the ``Case`` aggregate (``app.domain.case``); H1 replaces the
flag with a per-case ``LegalHold`` record so the hold carries the
actor who placed it, the reason, and the actor + reason + timestamp
who released it. The refusal rule stays where it was — on the case
aggregate — so every code path that flips the case to a terminal
status inherits the guard.

# Why a separate aggregate (not just a flag)

The flag was sufficient to *refuse* disposition; it could not answer
"who placed the hold, when, and why?" The retention pillar's audit
contract (FM-039 / P09) requires that the chain record the action and
the rationale, not just the resulting state. A :class:`LegalHold`
aggregate carries that evidence in the row itself and the
``HoldPlaced`` / ``HoldReleased`` audit events record the same facts
on the per-tenant hash chain so an auditor can replay both sides.

# Invariants

- At most one active hold per case. A second :meth:`place` against a
  case that already has an active hold raises :class:`Conflict`; the
  release must come first. This matches the pillar's v1 decision
  ("a single binary flag per case", Q1).
- A release against a case that has no active hold raises
  :class:`InvariantViolation` — the caller must ``get_active`` first
  to know which row to release, and the use case enforces that.
- The reason is required. A blank reason is rejected so the audit
  row never carries a meaningless rationale.

# INTEGRATION_SEAM

The repository (persistence) layer is responsible for the
``(tenant_id, case_id)`` lookup; the domain just shapes the
aggregate. A follow-up dispatch will add a SQL adapter
(``SqlLegalHoldRepository``); this slice ships the domain + the
application use case + the HTTP router wired against the in-memory
test fake so the seam is locatable and type-checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid4

from app.domain.errors import Conflict, InvariantViolation, ValidationFailed

# why: the held-disposition refusal code is a cross-layer contract
# (application return + audit payload + HTTP details). A single
# constant keeps the three surfaces from drifting.
REFUSED_HOLD_CODE: Final[str] = "refused_hold"

# why: a held case blocks disposition of the case row, every attached
# document, and the audit evidence itself. The tuple is the closed set
# the refusal cites so a caller can prove "nothing in any scope was
# deleted" without enumerating tables.
BLOCKED_SCOPES: Final[tuple[str, str, str]] = ("case", "document", "audit")


@dataclass(frozen=True, slots=True)
class LegalHold:
    """A per-case legal hold record (P08-US1, US2 / FM-039).

    ``id`` is the aggregate's stable identity (UUID4 generated at
    construction); ``case_id`` ties the hold to its case. ``placed_by``
    + ``placed_reason`` + ``placed_at`` are the immutable record of
    who placed the hold and why. ``released_by`` + ``released_reason``
    + ``released_at`` are populated on :meth:`release` and remain
    ``None`` while the hold is active.

    Why a value object rather than a row-level flag: the hold carries
    actor + reason, which the audit chain references, which the
    timeline displays, and which a future retention policy will need
    when computing disposition eligibility (FM-040).
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    placed_by: UUID
    placed_reason: str
    placed_at: datetime
    released_by: UUID | None = None
    released_reason: str | None = None
    released_at: datetime | None = None

    @staticmethod
    def place(
        *,
        tenant_id: UUID,
        case_id: UUID,
        placed_by: UUID,
        reason: str,
        now: datetime,
    ) -> LegalHold:
        """Construct a fresh, active hold.

        Validates the rationale up front so a row never lands without
        one. The caller (use case) is responsible for confirming the
        case has no active hold *before* calling :meth:`place`; the
        domain-level dedup happens in the repository via the
        ``(tenant_id, case_id)`` lookup.
        """
        if not tenant_id or not case_id or not placed_by:
            raise InvariantViolation("tenant_id, case_id, and placed_by are required")
        _validate_reason(reason)
        return LegalHold(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=case_id,
            placed_by=placed_by,
            placed_reason=reason.strip(),
            placed_at=now,
        )

    def release(
        self,
        *,
        released_by: UUID,
        reason: str,
        now: datetime,
    ) -> LegalHold:
        """Return a new ``LegalHold`` with the release fields populated.

        Raises :class:`InvariantViolation` when the hold is already
        released — a second release is a programmer error, not a
        legitimate business action. The repository's
        ``(tenant_id, case_id) -> active hold`` lookup guarantees a
        case can only have one active hold, so the caller cannot
        accidentally target a stale row.
        """
        if self.released_at is not None:
            raise InvariantViolation(
                f"legal hold {self.id} is already released at {self.released_at.isoformat()}"
            )
        if not released_by:
            raise InvariantViolation("released_by is required")
        _validate_reason(reason)
        return LegalHold(
            id=self.id,
            tenant_id=self.tenant_id,
            case_id=self.case_id,
            placed_by=self.placed_by,
            placed_reason=self.placed_reason,
            placed_at=self.placed_at,
            released_by=released_by,
            released_reason=reason.strip(),
            released_at=now,
        )

    @property
    def is_active(self) -> bool:
        """True while the hold is preventing disposition."""
        return self.released_at is None

    def to_payload(self) -> dict[str, object]:
        """Render the hold as a JSON-safe dict for the audit payload.

        The shape is the contract between the use case and the audit
        store. Field names are stable; the timeline reads them
        verbatim when the UI surfaces the hold row.
        """
        out: dict[str, object] = {
            "hold_id": str(self.id),
            "case_id": str(self.case_id),
            "placed_by": str(self.placed_by),
            "placed_reason": self.placed_reason,
            "placed_at": self.placed_at.isoformat(),
            "is_active": self.is_active,
        }
        if self.released_by is not None:
            out["released_by"] = str(self.released_by)
        if self.released_reason is not None:
            out["released_reason"] = self.released_reason
        if self.released_at is not None:
            out["released_at"] = self.released_at.isoformat()
        return out


class HoldAlreadyPresent(Conflict):
    """Raised by the use case when a case already has an active hold.

    The use case looks up the active hold via the repository; if one
    exists, it raises this exception so the HTTP boundary returns a
    structured 409 (``conflict``). The :class:`Conflict` superclass
    already maps to 409 in ``interfaces/http/errors.py``; the
    subclass is here so the use case's contract is greppable.
    """

    def __init__(self, case_id: UUID) -> None:
        super().__init__(
            f"case {case_id} already has an active legal hold; release it before placing another"
        )
        self.case_id = case_id


class HoldNotPresent(Conflict):
    """Raised by the use case when a case has no active hold to release.

    Symmetric with :class:`HoldAlreadyPresent`: a release against a
    case with no active hold is a 409, not a 404, so the caller can
    tell "no hold to release" from "no case". The repository's
    ``get_active`` returns ``None`` for missing/cross-tenant case
    ids; the use case treats that as ``HoldNotPresent`` for an
    in-tenant case and ``NotFound`` for a missing case.
    """

    def __init__(self, case_id: UUID) -> None:
        super().__init__(f"case {case_id} has no active legal hold to release")
        self.case_id = case_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_reason(reason: str) -> None:
    if not reason or not reason.strip():
        raise ValidationFailed(
            "reason is required for legal hold placement or release",
            field="reason",
        )


@dataclass(frozen=True, slots=True)
class DispositionRefusal:
    """A held-disposition refusal (P08-US1 / FM-039 refusal seam).

    The value object is the single decision point the application
    layer delegates to when a disposition execution meets an active
    hold. It carries the machine-readable ``code`` (``refused_hold``),
    the authorising policy version that was cited, the blocking hold
    id, and the blocked scopes (``case``/``document``/``audit``).

    Why a value object (not an exception): the execution use case
    *returns* the refusal so the caller can render ``refused_hold``
    without catching, while the audit payload reuses the same shape
    verbatim. Exceptions would force every caller into try/except;
    the value object keeps the happy path and the refusal path
    symmetric.

    Attributes:
        code: Always ``refused_hold`` (see :data:`REFUSED_HOLD_CODE`).
        policy_id: The published policy that surfaced the request.
        policy_version: The policy ``version_number`` cited as proof
            of which horizon was evaluated when the refusal fired.
        hold_id: The active hold that blocked disposition.
        case_id: The case nothing was deleted for.
        blocked_scopes: The scopes left untouched — always
            ``("case", "document", "audit")`` for v1.
        reason: Human-readable rationale for the audit row.
    """

    code: str
    policy_id: UUID
    policy_version: int
    hold_id: UUID
    case_id: UUID
    blocked_scopes: tuple[str, ...]
    reason: str

    def to_payload(self) -> dict[str, Any]:
        """Render the refusal as a JSON-safe dict for the audit payload."""
        return {
            "code": self.code,
            "policy_id": str(self.policy_id),
            "policy_version": self.policy_version,
            "hold_id": str(self.hold_id),
            "case_id": str(self.case_id),
            "blocked_scopes": list(self.blocked_scopes),
            "reason": self.reason,
        }


def refuse_disposition_under_hold(
    *,
    hold: LegalHold,
    case_id: UUID,
    policy_id: UUID,
    policy_version: int,
) -> DispositionRefusal:
    """Build the refusal for an active hold (pure, no I/O).

    The caller must have already confirmed ``hold.is_active``; the
    helper re-checks fail-closed so a released hold can never produce
    a ``refused_hold`` row (a released hold means disposition may
    proceed — the execution path owns that decision).

    Args:
        hold: The active hold that blocks disposition.
        case_id: The case execution was attempted for.
        policy_id: The policy that surfaced the disposition request.
        policy_version: The cited policy version number.

    Returns:
        The refusal value object with ``blocked_scopes`` covering
        the case, its documents, and its audit evidence.

    Raises:
        InvariantViolation: If the hold is not active (fail-closed —
            a stale/released hold must never block execution).
    """
    # why: fail-closed — a released hold must not fabricate a refusal
    # that would permanently wedge the disposition queue.
    if not hold.is_active:
        raise InvariantViolation("cannot refuse disposition for an inactive hold")
    return DispositionRefusal(
        code=REFUSED_HOLD_CODE,
        policy_id=policy_id,
        policy_version=policy_version,
        hold_id=hold.id,
        case_id=case_id,
        blocked_scopes=BLOCKED_SCOPES,
        reason=f"case {case_id} is under active legal hold {hold.id}; "
        f"disposition refused under policy v{policy_version}; "
        "no case, document, or audit rows were deleted",
    )


__all__ = [
    "BLOCKED_SCOPES",
    "DispositionRefusal",
    "HoldAlreadyPresent",
    "HoldNotPresent",
    "LegalHold",
    "REFUSED_HOLD_CODE",
    "refuse_disposition_under_hold",
]
