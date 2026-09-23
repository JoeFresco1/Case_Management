"""Shared internal helpers for the custom-report use cases.

Split out of the former single-file ``custom_reports.py`` during the R2
package split; only tiny value objects shared by more than one use case
live here.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy (see view_case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


__all__ = ["_ActorProxy"]
