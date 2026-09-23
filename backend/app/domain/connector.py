"""Connector aggregate (P14-US5, US7 / FM-058).

A :class:`Connector` is a versioned, separately authorized component that
moves material between the platform and an external system. The
connector framework is the seam where the deterministic core meets the
enterprise perimeter (AGENTS.md §12). **Authorization is the contract**:
no connector is enabled because a URL or credential is present; every
transition is an explicit, audit-recorded operator action.

# Lifecycle (P14 §2 / FM-058)

The lifecycle is linear with one safe intermediate state:

::

    DRAFT ──approve──▶ APPROVED ──enable──▶ ENABLED ──retire──▶ DRAINING ──drained──▶ RETIRED
                          │                    │
                          └──(discard draft)───┘

- ``DRAFT`` — the operator is preparing the design; no inbound/outbound
  calls. Not reachable from outside the platform; the row is purely
  administrative.
- ``APPROVED`` — a security administrator has recorded approval
  (P14-US5). The connector still does nothing; enable is a separate
  action so authorization and operation are not conflated.
- ``ENABLED`` — the connector is permitted to act. The implementation
  (the bytes / the adapter) is loaded; the connector accepts traffic.
  ``approved_by`` and ``approved_at`` are the audit anchor.
- ``DRAINING`` — a retire command has been issued. New traffic is
  refused; in-flight work either completes or fails safely (P14-US7).
  This is the safe intermediate state: the operator can observe
  progress, replay dead letters, and abort back to ``ENABLED`` if the
  retire was accidental.
- ``RETIRED`` — terminal. The implementation is unloaded; the row
  remains as the audit anchor. A retired connector cannot be
  re-enabled; a new connector with a new id is required.

# Gated named-connector implementation (Constitution V.10 / XII.5)

A **named** connector (``ConnectorKind`` value other than
``SYNTHETIC``) is a separately authorized artifact. Implementing one
requires a security administrator to record an
``implementation_authorized_by`` reference; the domain refuses to
construct or transition a named connector without it. Synthetic
connectors are the only kind that can be approved in the first slice
(Constitution V.10, AGENTS.md §18 — synthetic data only).

# Multi-tenant posture

Every aggregate carries ``tenant_id``. The repository port in
``app.domain.ports`` (``ConnectorRepository``, declared in the
application layer's per-port) returns ``None`` for cross-tenant reads
so existence is never leaked (Constitution V).

# Why a separate domain module

Connectors are not cases, not channels, not workflows. The framework
is small but its authorization model is the most sensitive in the
platform — a wrong enable writes to the enterprise perimeter. Keeping
the aggregate in its own module means the authorization code is
audited in one place, not scattered across feature folders.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Final
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation

# ---------------------------------------------------------------------------
# Connector kinds — the canonical set of connectors this slice understands.
#
# A connector kind is the **named** target. The first slice only ships
# with ``SYNTHETIC``: a connector that exercises the lifecycle in tests
# without touching any enterprise system. Adding a new kind
# (e.g. ``ONBASE``) is a Gated product decision per Constitution V.10 /
# XII.5 — the kind is added here when the explicit authorization is
# recorded in a separate ADR, and the named-connector authorization
# gate below is the enforcement point.
# ---------------------------------------------------------------------------


class ConnectorKind(str, Enum):  # noqa: UP042 - intentional str+Enum
    """The canonical set of connector targets.

    ``SYNTHETIC`` is the only kind the first slice ships with
    (Constitution V.10, AGENTS.md §18). The named connectors
    (``ONBASE``, ``PANZURA``, ``SHAREPOINT``, …) are reserved — they
    are added here in lockstep with the explicit product authorization
    that lets us build their adapters. The domain refuses to construct
    a row for a named kind without ``implementation_authorized_by``.
    """

    SYNTHETIC = "synthetic"
    # Reserved — Gated per Constitution V.10 / XII.5 / Pillar P14 §1.
    # A connector of this kind can only be created when the security
    # administrator has recorded explicit authorization. No role grants
    # the corresponding Action today; the seam is documented in
    # ADR-015 and the application layer's authorization check is the
    # enforcement point.
    ONBASE = "onbase"
    PANZURA = "panzura"


# The set of kinds the platform recognizes. A kind outside this set is
# rejected at construction so a typo cannot silently bypass the
# named-connector authorization gate.
_RECOGNIZED_KINDS: Final[frozenset[ConnectorKind]] = frozenset(ConnectorKind)

# The set of kinds that DO NOT require named-connector authorization.
# Only the synthetic kind is on this list in the first slice. Every
# other kind is **named** and must be separately authorized.
_UNRESTRICTED_KINDS: Final[frozenset[ConnectorKind]] = frozenset({ConnectorKind.SYNTHETIC})

# Lifecycle event names — stable strings the operator console buckets on.
LIFECYCLE_DRAFT = "draft"
LIFECYCLE_APPROVED = "approved"
LIFECYCLE_ENABLED = "enabled"
LIFECYCLE_DRAINING = "draining"
LIFECYCLE_RETIRED = "retired"


class ConnectorState(str, Enum):  # noqa: UP042
    """The lifecycle state of a :class:`Connector`.

    The state is the only mutating field on the aggregate. Every
    transition is a method that returns a new ``Connector``; in-place
    mutation is impossible because the aggregate is a frozen dataclass
    (Constitution XV — boring over clever).
    """

    DRAFT = "draft"
    APPROVED = "approved"
    ENABLED = "enabled"
    DRAINING = "draining"
    RETIRED = "retired"


_TERMINAL_STATES: Final[frozenset[ConnectorState]] = frozenset({ConnectorState.RETIRED})


def is_terminal_state(state: ConnectorState) -> bool:
    """Return True iff ``state`` is a terminal lifecycle state."""
    return state in _TERMINAL_STATES


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Connector:
    """A tenant-scoped, versioned, separately authorized connector.

    The aggregate carries everything the operator console and the
    audit chain need to reconstruct "who authorized this connector, who
    enabled it, when was it retired". The implementation (the bytes
    that move data) is referenced by ``adapter_ref`` — the actual
    adapter is the integration seam (AGENTS.md §12).

    Attributes:
        id: Stable id; assigned at draft time and never recycled.
        tenant_id: Owning tenant. Cross-tenant lookups return ``None``
            at the repository (Constitution V).
        key: Operator-facing identifier (stable, lowercase alnum +
            ``_`` / ``-``). The key is the IdP-friendly handle the
            operator UI shows.
        kind: The connector kind (see :class:`ConnectorKind`). Named
            kinds require ``implementation_authorized_by``.
        state: Current lifecycle state.
        adapter_ref: Opaque pointer to the implementation (e.g. a
            module path). The first slice sets this to
            ``"app.infrastructure.connectors.synthetic"`` for synthetic
            connectors; named connectors set it to a not-yet-existing
            placeholder per ADR-015.
        design_summary: Free-text description of the design. The
            security administrator reads this when approving. Never
            carries payload data (Constitution V).
        created_at / created_by: Authoring metadata.
        updated_at: Last transition timestamp.
        approved_at / approved_by: Set on the DRAFT → APPROVED
            transition. The audit anchor.
        enabled_at / enabled_by: Set on the APPROVED → ENABLED
            transition. Optional; cleared if the operator drains
            (the drain does not change the audit anchor, only the
            state).
        implementation_authorized_by: Required for any **named**
            connector (``kind`` not in :data:`_UNRESTRICTED_KINDS`).
            Records who recorded the explicit authorization that lets
            this connector exist at all. The synthetic kind leaves
            this ``None``.
    """

    id: UUID
    tenant_id: UUID
    key: str
    kind: ConnectorKind
    state: ConnectorState
    adapter_ref: str
    design_summary: str
    created_at: datetime
    created_by: UUID
    updated_at: datetime
    approved_at: datetime | None = None
    approved_by: UUID | None = None
    enabled_at: datetime | None = None
    enabled_by: UUID | None = None
    retired_at: datetime | None = None
    retired_by: UUID | None = None
    # Tagged-authorization record. Set for named connectors on
    # construction; ``None`` for synthetic connectors.
    implementation_authorized_by: UUID | None = None
    implementation_authorized_at: datetime | None = None
    # Free-text rationale recorded by the security administrator
    # alongside the authorization. Required for named connectors.
    authorization_rationale: str = ""
    # Internal counters used by monitor: transitions, in-flight count.
    # The monitor endpoint surfaces these so the operator can see
    # "how much work is the drainer still chewing through".
    in_flight_count: int = 0
    total_completed_count: int = 0
    total_rejected_count: int = 0
    # Notes recorded on retire. The drain notes are the operator's
    # free-text account of how the drain went.
    drain_notes: str = ""
    # Outbox-related flag: a connector is "drained" when the operator
    # confirms no in-flight traffic remains. The flag is a hint, not a
    # guarantee — the operator still has to confirm the safe-drain
    # invariant (audit chain) before transitioning to RETIRED.
    drain_confirmed: bool = False

    def __post_init__(self) -> None:
        if not self.key:
            raise InvariantViolation("connector requires a non-empty key")
        if not all(c.isalnum() or c in ("_", "-") for c in self.key):
            raise InvariantViolation("connector key may only contain alnum, underscore, or dash")
        if self.kind not in _RECOGNIZED_KINDS:
            raise InvariantViolation(f"unrecognized connector kind: {self.kind!r}")
        if not self.adapter_ref:
            raise InvariantViolation("connector requires a non-empty adapter_ref")
        if self.state not in ConnectorState:
            raise InvariantViolation(f"invalid state: {self.state!r}")

        # Named-connector authorization gate. A connector of a kind
        # outside ``_UNRESTRICTED_KINDS`` MUST carry the explicit
        # authorization record. This is the deterministic core of
        # P14-US5 / Constitution V.10 / XII.5 — the application layer
        # ALSO checks the action, but the domain refuses to construct
        # the row regardless of the caller, so a misconfigured
        # application path cannot bypass the gate.
        if self.kind not in _UNRESTRICTED_KINDS:
            if self.implementation_authorized_by is None:
                raise InvariantViolation(
                    f"named connector of kind {self.kind.value!r} requires "
                    "implementation_authorized_by; synthetic-only in first slice"
                )
            if self.implementation_authorized_at is None:
                raise InvariantViolation(
                    f"named connector of kind {self.kind.value!r} requires "
                    "implementation_authorized_at"
                )
            if not self.authorization_rationale.strip():
                raise InvariantViolation(
                    f"named connector of kind {self.kind.value!r} requires a "
                    "non-empty authorization_rationale"
                )

    # ----- factories --------------------------------------------------------

    @staticmethod
    def draft(
        *,
        tenant_id: UUID,
        key: str,
        kind: ConnectorKind,
        design_summary: str,
        created_by: UUID,
        now: datetime,
        adapter_ref: str = "app.infrastructure.connectors.synthetic",
        implementation_authorized_by: UUID | None = None,
        implementation_authorized_at: datetime | None = None,
        authorization_rationale: str = "",
    ) -> Connector:
        """Construct a fresh ``DRAFT`` connector.

        ``implementation_authorized_by`` is REQUIRED for named
        connectors (any kind other than ``SYNTHETIC``); the application
        layer must have already verified the actor's authorization
        before calling this factory. The domain's own check (see
        ``__post_init__``) is the deterministic backstop.
        """
        return Connector(
            id=uuid4(),
            tenant_id=tenant_id,
            key=key,
            kind=kind,
            state=ConnectorState.DRAFT,
            adapter_ref=adapter_ref,
            design_summary=design_summary,
            created_at=now,
            created_by=created_by,
            updated_at=now,
            implementation_authorized_by=implementation_authorized_by,
            implementation_authorized_at=implementation_authorized_at,
            authorization_rationale=authorization_rationale,
        )

    # ----- transitions ------------------------------------------------------

    def approve(self, *, by: UUID, at: datetime, rationale: str) -> Connector:
        """DRAFT → APPROVED. Records the security administrator.

        Idempotent on already-approved rows: a re-approval returns the
        connector unchanged so an operator UI retry does not produce
        a duplicate audit event. A retired connector cannot be
        re-approved (the state machine is one-way).
        """
        if self.state is ConnectorState.APPROVED:
            return self
        if self.state is not ConnectorState.DRAFT:
            raise InvariantViolation(f"cannot approve a connector in state {self.state.value!r}")
        if not rationale.strip():
            raise InvariantViolation("approval requires a non-empty rationale")
        return replace(
            self,
            state=ConnectorState.APPROVED,
            approved_at=at,
            approved_by=by,
            updated_at=at,
        )

    def enable(self, *, by: UUID, at: datetime) -> Connector:
        """APPROVED → ENABLED. Operator flips the switch.

        Idempotent on already-enabled rows. Draining and retired
        connectors cannot be enabled (the operator must retire a fresh
        connector with the same name if they need to revive the
        capability).
        """
        if self.state is ConnectorState.ENABLED:
            return self
        if self.state is not ConnectorState.APPROVED:
            raise InvariantViolation(f"cannot enable a connector in state {self.state.value!r}")
        return replace(
            self,
            state=ConnectorState.ENABLED,
            enabled_at=at,
            enabled_by=by,
            updated_at=at,
        )

    def begin_drain(self, *, by: UUID, at: datetime, notes: str = "") -> Connector:
        """ENABLED → DRAINING. The retire command starts a safe drain.

        In-flight work continues; new traffic is refused by the
        application layer's monitor (the adapter check is on the
        ``state``). The drain notes are operator-facing and recorded
        on the row so the audit chain can answer "what was the
        operator's plan at retire time".
        """
        if self.state is ConnectorState.DRAINING:
            return self
        if self.state is not ConnectorState.ENABLED:
            raise InvariantViolation(
                f"cannot begin drain on a connector in state {self.state.value!r}"
            )
        return replace(
            self,
            state=ConnectorState.DRAINING,
            updated_at=at,
            drain_notes=notes,
        )

    def confirm_drain(self, *, in_flight: int, completed: int, rejected: int) -> Connector:
        """Record the operator's confirmation that the drain is complete.

        The operator is responsible for verifying the safe-drain
        invariant (no in-flight traffic, all errors captured on the
        audit chain). The domain does not assert it; the application
        layer reads the count from the connector's adapter and the
        operator confirms. The domain only stores the snapshot so the
        retirement audit can answer "what did the operator see at
        retire time".
        """
        if self.state is not ConnectorState.DRAINING:
            raise InvariantViolation("confirm_drain is only valid in the DRAINING state")
        if in_flight < 0 or completed < 0 or rejected < 0:
            raise InvariantViolation("drain counters must be non-negative")
        return replace(
            self,
            in_flight_count=in_flight,
            total_completed_count=completed,
            total_rejected_count=rejected,
            drain_confirmed=in_flight == 0,
        )

    def retire(self, *, by: UUID, at: datetime) -> Connector:
        """DRAINING → RETIRED. Terminal.

        The retirement refuses if the operator has not confirmed the
        safe-drain invariant (``in_flight_count == 0`` AND
        ``drain_confirmed``). This is the deterministic backstop of
        P14-US7: a connector that retires with in-flight work loses
        that work silently, which is a Constitution V violation.
        """
        if self.state is ConnectorState.RETIRED:
            return self
        if self.state is not ConnectorState.DRAINING:
            raise InvariantViolation(f"cannot retire a connector in state {self.state.value!r}")
        if not self.drain_confirmed or self.in_flight_count > 0:
            raise InvariantViolation(
                "cannot retire a connector with in-flight traffic; "
                "confirm drain first (in_flight_count must be 0)"
            )
        return replace(
            self,
            state=ConnectorState.RETIRED,
            retired_at=at,
            retired_by=by,
            updated_at=at,
        )

    # ----- queries ----------------------------------------------------------

    def is_named(self) -> bool:
        """True iff this is a **named** connector (not synthetic)."""
        return self.kind not in _UNRESTRICTED_KINDS

    def is_terminal(self) -> bool:
        """True iff the lifecycle is in a terminal state (``RETIRED``)."""
        return is_terminal_state(self.state)

    def accepts_traffic(self) -> bool:
        """True iff the connector is permitted to act on traffic.

        Only the ``ENABLED`` state accepts traffic. ``DRAINING``
        refuses new traffic by definition; ``APPROVED`` is enabled in
        name only — the operator has not flipped the switch.
        """
        return self.state is ConnectorState.ENABLED


__all__ = [
    "Connector",
    "ConnectorKind",
    "ConnectorState",
    "LIFECYCLE_APPROVED",
    "LIFECYCLE_DRAINING",
    "LIFECYCLE_DRAFT",
    "LIFECYCLE_ENABLED",
    "LIFECYCLE_RETIRED",
    "is_terminal_state",
]
