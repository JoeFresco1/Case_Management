"""Dependency contracts for the admin outbox HTTP composition boundary.

The admin outbox router needs two things that live outside
``app.interfaces``:

1. A session-scoped port bundle (the role repository used by the
   ``ADMIN_TENANT_CONFIG`` authorization check). This module describes
   that bundle without importing FastAPI, SQLAlchemy, or any
   infrastructure adapter. The composition root supplies the concrete
   provider.
2. The application-scoped outbox dispatcher (``count_lag`` /
   ``list_pending`` / ``replay``), resolved from ``request.app.state``
   and viewed through the infra-free :class:`OutboxAdminService`
   Protocol so the router never imports the infrastructure dispatcher
   class.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID

from app.domain.ports import RoleRepository

OUTBOX_MAX_ATTEMPTS: Final[int] = 5
"""Retry cap mirrored from the outbox dispatcher.

Why a mirror instead of an import: the dispatcher lives in
``app.infrastructure.messaging`` which ``app.interfaces`` must not
import (AGENTS.md §4.R4 / import-linter contract "Interfaces do not
import infrastructure directly"). The value is part of the operator
contract (a row at the cap is dead-lettered); if the dispatcher's
``MAX_ATTEMPTS`` ever changes, this constant must change with it.
"""


@dataclass(frozen=True, slots=True)
class OutboxRowView:
    """Application-level projection of one pending outbox row.

    The attribute set matches the durable ``outbox`` row column-for-
    column so the current dispatcher (which returns its own row type)
    satisfies this view structurally. A future slice may return these
    views from a translating adapter instead; the router does not
    change either way.
    """

    id: UUID
    tenant_id: UUID
    event_type: str
    event_id: UUID
    created_at: datetime
    dispatched_at: datetime | None
    attempt_count: int
    last_error: str | None


class OutboxAdminService(Protocol):
    """Read/replay surface of the outbox dispatcher for the operator console."""

    async def count_lag(self) -> dict[str, int]:
        """Return lag counters (``total`` / ``pending`` / ``dead_lettered`` / ``retrying``)."""
        ...

    async def list_pending(
        self, *, include_dead_lettered: bool = True, limit: int = 50
    ) -> Sequence[OutboxRowView]:
        """Return pending rows, oldest first, capped at ``limit``.

        Args:
            include_dead_lettered: when True (default) rows that hit
                ``OUTBOX_MAX_ATTEMPTS`` are included so the operator
                sees poison messages; when False only auto-retryable
                rows are returned.
            limit: maximum rows in the page (default 50, max 200).

        Returns:
            The pending row views in ``created_at`` ascending order.
        """
        ...

    async def replay(self, row_id: UUID) -> bool:
        """Reset a row's retry state so the next dispatcher tick re-attempts it.

        Args:
            row_id: the outbox row to reset.

        Returns:
            True when a pending row was reset; False when the row did
            not exist or was already dispatched (no-op).
        """
        ...


@dataclass(frozen=True, slots=True)
class AdminOutboxHttpRepositories:
    """Ports required by the admin outbox endpoints for one session."""

    role: RoleRepository


class AdminOutboxHttpDependencyProvider(Protocol):
    """Create a request-scoped admin outbox port bundle from a persistence session."""

    def for_session(self, session: object) -> AdminOutboxHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "OUTBOX_MAX_ATTEMPTS",
    "AdminOutboxHttpDependencyProvider",
    "AdminOutboxHttpRepositories",
    "OutboxAdminService",
    "OutboxRowView",
]
