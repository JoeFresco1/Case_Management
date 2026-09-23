"""Shared primitives for the archival + freeze + orphan use cases (FM-041).

Subject-type constants for the audit chain, the duck-typed clock /
actor protocols every use case in this package accepts, and the
sortable correlation-id helper. Kept in one module so the sibling
use-case modules do not re-declare them (a single definition site
means a single place to audit when the audit subject vocabulary
changes).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

# Subject types for the audit chain. Stable strings — the timeline
# reads them verbatim when the UI surfaces the archival row.
SUBJECT_TYPE_CASE: str = "case"
SUBJECT_TYPE_ARCHIVAL: str = "case_archival"
SUBJECT_TYPE_DISPOSAL_FREEZE: str = "disposal_freeze"
SUBJECT_TYPE_ORPHAN_PROPOSAL: str = "orphan_proposal"


class _ClockLike(Protocol):
    """Minimal clock protocol used by the use cases.

    The application layer must not depend on FastAPI / ``app.deps``
    (Constitution VI). The duck-typed ``FixedClock`` test fake
    and any future adapter satisfy this shape.
    """

    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy (see disposition use case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


def _new_correlation_id() -> str:
    """Generate a sortable correlation id.

    Mirrors the disposition use case helper: the published PyPI
    package name is ``uuid7`` but its module is
    ``uuid_extensions`` (the PyPI name was a placeholder during
    early publishing). The lazy import keeps cold-start light
    and survives a missing package.
    """
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())


__all__ = [
    "SUBJECT_TYPE_ARCHIVAL",
    "SUBJECT_TYPE_CASE",
    "SUBJECT_TYPE_DISPOSAL_FREEZE",
    "SUBJECT_TYPE_ORPHAN_PROPOSAL",
    "_ActorProxy",
    "_ClockLike",
    "_new_correlation_id",
]
