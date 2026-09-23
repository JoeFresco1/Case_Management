"""Shared protocols, actor proxy, hold translation, and gate codes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast
from uuid import UUID

from app.domain.legal_hold import LegalHold
from app.domain.ports import LegalHoldRepository
from app.domain.retention import HoldPeriod

# Subject types for the audit chain. Stable strings — the timeline
# reads them verbatim when the UI surfaces the disposition row.
SUBJECT_TYPE_RETENTION_POLICY = "retention_policy"
SUBJECT_TYPE_DISPOSITION_REQUEST = "disposition_request"
SUBJECT_TYPE_CASE = "case"

# why: gate-refusal codes are a cross-layer contract (application
# Conflict message + HTTP 409 details + audit payload). Single
# constants keep the approve gate, the execute gate, and the tests
# from drifting. ``REFUSED_HOLD_CODE`` lives in legal_hold; the
# freeze/archival codes live here because archival.py owns the freeze
# rows while disposition.py owns the approval gate.
DISPOSAL_FROZEN_CODE: str = "disposal_frozen"
CASE_FROZEN_CODE: str = "case_frozen"


class _ClockLike(Protocol):
    """Minimal clock protocol used by the use cases.

    The application layer must not depend on FastAPI / ``app.deps``
    (Constitution VI). The duck-typed ``FixedClock`` test fake and
    any future adapter satisfy this shape.
    """

    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy (see create_case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


class _HoldHistoryReader(Protocol):
    """Structural hold-history surface for pause-and-resume accounting.

    The ``LegalHoldRepository`` port only promises ``get_active``;
    adapters that retain released rows (in-memory + future SQL) also
    expose ``history_for_case``. The use cases probe for it with
    :func:`_hold_periods_for_case` and fall back to ``[]`` so adapters
    without history keep the legacy ``created_at + period`` horizon.
    """

    async def history_for_case(self, tenant_id: UUID, case_id: UUID) -> Sequence[LegalHold]: ...


async def _hold_periods_for_case(
    hold_repo: LegalHoldRepository,
    tenant_id: UUID,
    case_id: UUID,
) -> list[HoldPeriod]:
    """Translate a case's hold history into frozen-clock periods.

    Returns ``[]`` when the adapter has no history surface (legacy
    horizon preserved) or when the case was never held. The mapping
    is placed→started, released→ended (``None`` while active).
    """
    reader = getattr(hold_repo, "history_for_case", None)
    if not callable(reader):
        return []
    holds = cast("Sequence[LegalHold]", await reader(tenant_id, case_id))
    return [HoldPeriod(started_at=h.placed_at, ended_at=h.released_at) for h in holds]


def _new_correlation_id() -> str:
    """Generate a sortable correlation id.

    The published PyPI package name is ``uuid7`` but its module is
    ``uuid_extensions`` (the PyPI name was a placeholder during early
    publishing). The lazy import keeps cold-start light and survives
    a missing package.
    """
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())
