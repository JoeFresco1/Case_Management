"""Saved-search + search-audit ports (P06-US3, US5, US6 / FM-032).

Saved searches are tenant-scoped rows the worker can re-execute. Every
execution emits a ``SavedSearchAudit`` row carrying the query shape,
the timestamp, the executing actor, and the result count so a security
administrator can review search activity (P09-US6-style surface scoped
to the search domain).

Tenant isolation is mandatory (AGENTS.md §3 R1, §12): every method
takes ``tenant_id`` first and the SQL adapter MUST include
``WHERE tenant_id = :tenant_id`` on every query.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.domain.ports.core import TenantScoped
from app.domain.saved_search import SavedSearch


class SavedSearchRepository(TenantScoped, Protocol):
    """Persistence port for :class:`app.domain.saved_search.SavedSearch`.

    The repository is the durable surface for named, reusable queries
    (P06-US3 / FM-032). The application use case is the only place that
    calls ``add``; the actor must already hold ``Action.CASE_READ``.

    Cross-tenant reads return ``None`` so a misconfigured caller cannot
    leak saved-search existence (Constitution V).
    """

    async def add(self, saved: SavedSearch) -> None:
        """Persist a freshly created saved-search row.

        Implementations MUST insert the row in the same transaction as
        the audit row so a partial commit never leaves a saved search
        without provenance (Constitution V).
        """

    async def get(self, tenant_id: UUID, saved_search_id: UUID) -> SavedSearch | None:
        """Return the saved-search row, or ``None``.

        Returns ``None`` for missing ids AND for ids belonging to a
        different tenant; the two cases are intentionally
        indistinguishable so the API never confirms cross-tenant
        existence (Constitution V).
        """

    async def list_for_owner(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[SavedSearch]:
        """Return the owner's saved-search rows, newest first.

        Used by the case worker's "My saved searches" pane. The
        ``owner_user_id`` is the row's owner column; the SQL adapter
        MUST also carry ``WHERE tenant_id = :tenant_id`` so a
        cross-tenant owner id is silently ignored (Constitution V).
        """

    async def delete(self, tenant_id: UUID, saved_search_id: UUID) -> bool:
        """Delete the saved-search row, returning ``True`` iff a row was removed.

        Returns ``False`` for missing ids AND for ids belonging to a
        different tenant. The owner is the only authorized deleter
        (enforced at the use-case layer; the repository is a thin
        surface).
        """


class SavedSearchAuditRepository(TenantScoped, Protocol):
    """Append-only audit row for every saved-search execution (P06-US6 / FM-032).

    The row carries the executing actor, the saved-search id (or
    ``None`` for an ad-hoc search), the canonical query shape, the
    timestamp, and the result count. Security administrators read the
    rows via ``list_for_security_report`` so they can answer "who
    searched for what and saw how many results".

    Audit immutability is enforced at four layers (AGENTS.md §4.R12 /
    plan §4.R12): DB trigger, ORM listener, no application path
    mutates, no migration rewrites. The repository only exposes
    ``append`` and the read shape.
    """

    async def append(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        saved_search_id: UUID | None,
        query_shape: dict[str, Any],
        result_count: int,
        occurred_at: datetime,
    ) -> None:
        """Append a single audit row.

        Implementations MUST include the row in the same transaction as
        the search execution so a failed search rolls back its audit
        row (P09-US2).
        """

    async def list_for_security_report(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID | None = None,
        saved_search_id: UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Any]:
        """Return audit rows for the security report (P06-US5 / FM-032).

        Filters are AND-ed and tenant-scoped (``WHERE tenant_id =
        :tenant_id``). Rows are ordered chronologically by
        ``occurred_at, id`` so a paginated caller can resume across
        requests without skipping or duplicating rows.

        The returned rows must expose ``id``, ``actor_id``,
        ``saved_search_id``, ``query_shape``, ``result_count``, and
        ``occurred_at`` so the application layer can project the wire
        shape without re-reading the database.
        """


__all__ = [
    "SavedSearchAuditRepository",
    "SavedSearchRepository",
]
