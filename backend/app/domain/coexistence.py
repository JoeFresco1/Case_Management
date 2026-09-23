from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Final
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation

# ---------------------------------------------------------------------------
# Migration state — the observable lifecycle of a case inside the policy.
#
# Every value is a *state* a case row can hold (or be classified under)
# once the policy is in effect. ``LIVE`` is intentionally absent: a case
# is "live in this platform" by virtue of NOT being ``divergent`` and
# NOT being ``unmigrated``. Encoding it as a separate state would let
# the policy imply the platform claims "live means active" — that is
# the legacy system's vocabulary, not this one's.
# ---------------------------------------------------------------------------


class MigrationState(str, Enum):  # noqa: UP042 - intentional str+Enum
    """The case-level migration states a coexistence policy recognizes.

    The set is closed: an unrecognized state is rejected at the boundary
    so a typo cannot silently widen the policy's vocabulary.
    """

    UNMIGRATED = "unmigrated"
    IMPORTED = "imported"
    RECONCILED = "reconciled"
    DIVERGENT = "divergent"
    RETIRED_FROM_SOURCE = "retired_from_source"


_RECOGNIZED_MIGRATION_STATES: Final[frozenset[MigrationState]] = frozenset(MigrationState)

_TERMINAL_MIGRATION_STATES: Final[frozenset[MigrationState]] = frozenset(
    {MigrationState.RETIRED_FROM_SOURCE}
)


def is_terminal_migration_state(state: MigrationState) -> bool:
    """Return True iff ``state`` is a terminal migration state.

    Only ``RETIRED_FROM_SOURCE`` is terminal — once the legacy system
    is retired, the case's lifecycle under this policy is closed and
    the platform is the system of record (P14 §8 Q4).
    """
    return state in _TERMINAL_MIGRATION_STATES


# ---------------------------------------------------------------------------
# Ownership rule — the explicit "who owns the case data" declaration.
#
# ``LEGACY_SYSTEM_OF_RECORD`` means "the legacy system is authoritative
# while the case is in scope of this policy; this platform holds a
# reconciled mirror". ``THIS_PLATFORM_SYSTEM_OF_RECORD`` means "this
# platform is authoritative; the legacy ref is provenance, not
# authority" (P14 §8 Q4). ``SHARED`` is reserved for future use; today
# it requires ``ownership_authorized_by`` because it implies a write
# channel the platform does not have.
# ---------------------------------------------------------------------------


class OwnershipRule(str, Enum):  # noqa: UP042
    """How authority over a case is split between legacy and this platform."""

    LEGACY_SYSTEM_OF_RECORD = "legacy_system_of_record"
    THIS_PLATFORM_SYSTEM_OF_RECORD = "this_platform_system_of_record"
    # Reserved — Gated per Constitution V.10 / XII.5. SHARED implies a
    # bidirectional sync the first slice does not ship. The domain
    # refuses to construct the row without ``ownership_authorized_by``.
    SHARED = "shared"


_RECOGNIZED_OWNERSHIP_RULES: Final[frozenset[OwnershipRule]] = frozenset(OwnershipRule)


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoexistencePolicy:
    """A tenant-scoped, versioned declaration of how legacy coexists with this platform.

    Attributes:
        id: Stable id; assigned at draft time and never recycled.
        tenant_id: Owning tenant. Cross-tenant lookups return ``None``
            at the repository (Constitution V).
        key: Operator-facing identifier (stable, lowercase alnum +
            ``_`` / ``-``). The key is the handle the case-side
            ``SourceSystemRef`` references.
        version: Monotonic version number. The first policy has
            ``version=1``; each subsequent publish increments by one.
            A policy at the same key+version is immutable.
        source_systems: Tuple of named legacy systems this policy
            refers to (e.g. ``("onbase",)``). Cases may only carry a
            ``SourceSystemRef`` whose ``system`` is in this tuple.
        ownership: The :class:`OwnershipRule` that declares authority.
        migration_state: The :class:`MigrationState` that classifies
            a case under this policy. The policy is recorded against
            a state so the operator console can answer "what does the
            policy say about cases in state X?".
        real_time_sync_enabled: Whether this policy permits real-time
            sync. **Default and required value today is ``False``**.
            Setting this to ``True`` requires ``sync_authorized_by``;
            see the Gated section below.
        sync_authorized_by: Required iff ``real_time_sync_enabled`` is
            ``True``. Records who recorded the explicit authorization.
            None while ``real_time_sync_enabled=False``.
        sync_authorized_at: Required iff ``real_time_sync_enabled`` is
            ``True``.
        ownership_authorized_by: Required iff ``ownership`` is
            ``SHARED``. The application layer must already have
            verified the actor before calling the factory; the domain
            is the deterministic backstop.
        ownership_authorized_at: Required iff ``ownership`` is
            ``SHARED``.
        sync_evidence: Tuple of import-job ids that constitute the
            authoritative audit trail of "legacy data flowed into this
            platform". Empty tuple on draft. Append-only — entries are
            never removed; only added as new batched imports complete.
        provenance_note: Operator-written free-text describing where
            this policy came from (e.g. "ADR-016; legal review
            2026-09-01"). Never carries payload data.
        created_at / created_by: Authoring metadata.
        updated_at: Last modification timestamp.
    """

    id: UUID
    tenant_id: UUID
    key: str
    version: int
    source_systems: tuple[str, ...]
    ownership: OwnershipRule
    migration_state: MigrationState
    real_time_sync_enabled: bool
    provenance_note: str
    created_at: datetime
    created_by: UUID
    updated_at: datetime
    sync_authorized_by: UUID | None = None
    sync_authorized_at: datetime | None = None
    ownership_authorized_by: UUID | None = None
    ownership_authorized_at: datetime | None = None
    sync_evidence: tuple[UUID, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self._validate_key()
        self._validate_version()
        self._validate_source_systems()
        self._validate_ownership_rule()
        self._validate_migration_state()
        self._validate_sync_gate()
        self._validate_shared_ownership_gate()

    def _validate_key(self) -> None:
        if not self.key:
            raise InvariantViolation("coexistence policy requires a non-empty key")
        if not all(c.isalnum() or c in ("_", "-") for c in self.key):
            raise InvariantViolation(
                "coexistence policy key may only contain alnum, underscore, or dash"
            )

    def _validate_version(self) -> None:
        if self.version < 1:
            raise InvariantViolation("coexistence policy version must be >= 1")

    def _validate_source_systems(self) -> None:
        if not self.source_systems:
            raise InvariantViolation(
                "coexistence policy must name at least one source system "
                "(use a synthetic source label for local-only demonstration)"
            )
        for src in self.source_systems:
            if not src or not all(c.isalnum() or c in ("_", "-") for c in src):
                raise InvariantViolation(
                    f"invalid source system identifier {src!r}; "
                    "expected lowercase alnum / underscore / dash"
                )

    def _validate_ownership_rule(self) -> None:
        if self.ownership not in _RECOGNIZED_OWNERSHIP_RULES:
            raise InvariantViolation(f"unrecognized ownership rule: {self.ownership!r}")

    def _validate_migration_state(self) -> None:
        if self.migration_state not in _RECOGNIZED_MIGRATION_STATES:
            raise InvariantViolation(f"unrecognized migration state: {self.migration_state!r}")

    def _validate_sync_gate(self) -> None:
        # Gated real-time sync gate. Constitution V.10 / XII.5: the
        # first slice does not ship real-time sync. The application
        # layer MUST refuse to construct with real_time_sync_enabled
        # unless the actor has the corresponding (currently
        # non-existent) authorization action. The domain is the
        # deterministic backstop: if the field is set, the
        # authorization record MUST be present.
        if self.real_time_sync_enabled:
            if self.sync_authorized_by is None:
                raise InvariantViolation(
                    "coexistence policy declares real_time_sync_enabled=True but "
                    "lacks sync_authorized_by; first slice ships with real-time "
                    "sync disabled per Constitution V.10 / XII.5 / P14 §1"
                )
            if self.sync_authorized_at is None:
                raise InvariantViolation(
                    "coexistence policy declares real_time_sync_enabled=True but "
                    "lacks sync_authorized_at"
                )
            return
        # Explicitly reject a stray authorization when sync is OFF.
        # This keeps the audit trail clean — "sync is off and there
        # is no authorization record" is one row, not "sync is off
        # but here's an old authorization no one is acting on".
        if self.sync_authorized_by is not None or self.sync_authorized_at is not None:
            raise InvariantViolation(
                "coexistence policy has real_time_sync_enabled=False but "
                "carries a sync authorization record; clear it or set the flag"
            )

    def _validate_shared_ownership_gate(self) -> None:
        # SHARED ownership gate. Same pattern: SHARED implies a
        # bidirectional write channel that does not exist in the first
        # slice. Refuse without the authorization record.
        if self.ownership is not OwnershipRule.SHARED:
            return
        if self.ownership_authorized_by is None:
            raise InvariantViolation(
                "coexistence policy declares ownership='shared' but lacks "
                "ownership_authorized_by; first slice ships without "
                "bidirectional ownership per Constitution V.10 / XII.5"
            )
        if self.ownership_authorized_at is None:
            raise InvariantViolation(
                "coexistence policy declares ownership='shared' but lacks ownership_authorized_at"
            )

    # ----- factories --------------------------------------------------------

    @staticmethod
    def draft(
        *,
        tenant_id: UUID,
        key: str,
        source_systems: tuple[str, ...],
        ownership: OwnershipRule,
        migration_state: MigrationState,
        created_by: UUID,
        now: datetime,
        provenance_note: str = "",
        real_time_sync_enabled: bool = False,
        sync_authorized_by: UUID | None = None,
        sync_authorized_at: datetime | None = None,
        ownership_authorized_by: UUID | None = None,
        ownership_authorized_at: datetime | None = None,
    ) -> CoexistencePolicy:
        """Construct a fresh v1 policy.

        The application layer is responsible for verifying the actor's
        authorization before calling this factory. The domain's
        ``__post_init__`` is the deterministic backstop: any of the
        Gated conditions (``real_time_sync_enabled=True`` or
        ``ownership=SHARED``) raise without their authorization
        record, regardless of caller.
        """
        return CoexistencePolicy(
            id=uuid4(),
            tenant_id=tenant_id,
            key=key,
            version=1,
            source_systems=tuple(source_systems),
            ownership=ownership,
            migration_state=migration_state,
            real_time_sync_enabled=real_time_sync_enabled,
            provenance_note=provenance_note,
            created_at=now,
            created_by=created_by,
            updated_at=now,
            sync_authorized_by=sync_authorized_by,
            sync_authorized_at=sync_authorized_at,
            ownership_authorized_by=ownership_authorized_by,
            ownership_authorized_at=ownership_authorized_at,
            sync_evidence=(),
        )

    # ----- transitions ------------------------------------------------------

    def record_import_evidence(
        self,
        *,
        import_job_id: UUID,
        at: datetime,
    ) -> CoexistencePolicy:
        """Append an import-job id to the policy's sync evidence.

        The policy is append-only: ``sync_evidence`` is a tuple and
        entries are never removed or reordered. The migration_state is
        also bumped forward to ``IMPORTED`` if the policy was still
        ``UNMIGRATED`` — the first successful import is the
        observable transition. ``RECONCILED`` / ``DIVERGENT`` /
        ``RETIRED_FROM_SOURCE`` are operator-driven states the
        application layer sets via :meth:`set_migration_state`.

        Idempotent on already-recorded ids: a duplicate import-job id
        is silently ignored so an operator retry does not produce a
        second audit row for the same evidence.
        """
        if import_job_id in self.sync_evidence:
            return self
        new_state = self.migration_state
        if new_state is MigrationState.UNMIGRATED:
            new_state = MigrationState.IMPORTED
        return replace(
            self,
            sync_evidence=self.sync_evidence + (import_job_id,),
            migration_state=new_state,
            updated_at=at,
        )

    def set_migration_state(
        self,
        *,
        new_state: MigrationState,
        at: datetime,
    ) -> CoexistencePolicy:
        """Move the policy's declared migration state.

        Operator-driven transitions: ``IMPORTED → RECONCILED``,
        ``IMPORTED → DIVERGENT``, ``DIVERGENT → RECONCILED``, and the
        terminal ``→ RETIRED_FROM_SOURCE`` (after which the policy is
        closed; the application layer must issue a fresh policy to
        re-open coexistence).

        The domain refuses backward transitions (``RECONCILED →
        DIVERGENT`` is allowed because reconciliation can fail again;
        ``RETIRED_FROM_SOURCE`` is terminal). Other backward
        transitions are refused.
        """
        if new_state not in _RECOGNIZED_MIGRATION_STATES:
            raise InvariantViolation(f"unrecognized migration state: {new_state!r}")
        if is_terminal_migration_state(self.migration_state):
            raise InvariantViolation(
                f"cannot transition migration state from terminal {self.migration_state.value!r}"
            )
        # RECONCILED ↔ DIVERGENT is the only allowed backward-and-forward pair;
        # everything else must move forward (UNMIGRATED → IMPORTED → ...).
        if (
            new_state is MigrationState.UNMIGRATED
            and self.migration_state is not MigrationState.UNMIGRATED
        ):
            raise InvariantViolation(
                "cannot transition migration state backwards to 'unmigrated' once imports "
                "have been recorded"
            )
        return replace(self, migration_state=new_state, updated_at=at)

    # ----- queries ----------------------------------------------------------

    def describes_sync(self) -> bool:
        """True iff this policy declares a real-time sync channel.

        Per Constitution V.10 / XII.5, the first slice always returns
        ``False``. A True return is the audit signal that the platform
        has a real-time channel with legacy — which today no platform
        adapter exists for.
        """
        return self.real_time_sync_enabled

    def is_terminal(self) -> bool:
        """True iff the policy's migration lifecycle is closed."""
        return is_terminal_migration_state(self.migration_state)

    def references(self, source_system: str) -> bool:
        """True iff ``source_system`` is one of the legacy systems this policy names.

        The application layer uses this to gate case-side
        ``SourceSystemRef`` writes: a ref to a system not named by
        the active coexistence policy is a misconfiguration, not a
        bug, and is refused with a domain error.
        """
        return source_system in self.source_systems


__all__ = [
    "CoexistencePolicy",
    "MigrationState",
    "OwnershipRule",
    "is_terminal_migration_state",
]
