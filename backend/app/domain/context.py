"""Framework-neutral context contracts shared by domain and application code.

This module intentionally has no FastAPI, persistence, or infrastructure
dependencies. The HTTP boundary may construct these values, but the domain
and application layers own their contracts here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


class Clock(Protocol):
    """Provide the current time to code that must remain deterministic in tests."""

    def now(self) -> datetime:
        """Return the current timestamp used by the caller."""


@dataclass(frozen=True, slots=True)
class UserContext:
    """Identify the authenticated actor and tenant for an operation."""

    user_id: UUID
    tenant_id: UUID
    display_name: str
    roles: tuple[str, ...]
    is_system: bool = False


__all__ = ["Clock", "UserContext"]
