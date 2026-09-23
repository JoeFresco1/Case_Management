"""Audit port -- append-only AuditEvent store."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.domain.ports.core import TenantScoped


class AuditEventStore(TenantScoped, Protocol):
    """Append-only audit store.

    The store MUST refuse UPDATE/DELETE at every layer (DB trigger, ORM
    listener, application path). Correcting history is done by writing a
    new `audit_events` row of type `AuditCorrectionRecorded` that
    references the original.

    `add` is the only mutation; there is no `update` or `delete`.
    """

    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID,
        actor_type: str,
        subject_type: str,
        subject_id: UUID,
        case_id: UUID | None,
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None: ...

    async def list_for_case(
        self,
        *,
        tenant_id: UUID,
        case_id: UUID,
        event_types: Sequence[str] | None = None,
        actor_id: UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Sequence[Any]:
        """Return audit rows for ``case_id`` in chronological order.

        Supports the timeline read shape (P09-US1 / US4):

        - Tenant-scoped (``WHERE tenant_id = :tenant_id``).
        - Case-scoped (``WHERE case_id = :case_id``).
        - Optional filters on ``event_type``, ``actor_id``, ``occurred_at``
          range (inclusive on both ends on the SQL adapter).
        - Cursor-based pagination keyed on ``(occurred_at, id)``; the
          cursor is opaque to callers -- the SQL adapter parses it.

        Returns rows as ORM entities. The application layer translates
        them to the wire shape; the port intentionally stays generic so
        the in-memory test fake and the SQL adapter agree on the
        column-level contract.
        """

    async def list_for_export(
        self,
        *,
        tenant_id: UUID,
        case_id: UUID | None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Sequence[Any]:
        """Return audit rows for the structured export (P09-US5 / FM-044).

        A superset of ``list_for_case``: when ``case_id`` is provided the
        scope is the case; when ``None`` the scope is the whole tenant
        within the ``since``/``until`` window (date-range export). The
        hash chain is identical -- every row carries ``prev_hash`` and
        ``hash`` so the consumer can verify integrity (P09-US3).
        """

    async def get_event(
        self,
        *,
        tenant_id: UUID,
        event_id: UUID,
    ) -> Any | None:
        """Fetch a single audit row by id, tenant-scoped.

        Used by ``correct_audit`` to validate the original event exists
        in the same tenant and to capture the subject/correlation context
        that the compensating row references. Returns ``None`` for
        missing or cross-tenant ids so the use case can surface 404
        without leaking existence (Constitution V).
        """

    async def list_access_decisions(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID | None = None,
        subject_type: str | None = None,
        action: str | None = None,
        result: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Sequence[Any]:
        """Return access-decision rows for the security report (P09-US6 / FM-045).

        Filters are AND-ed and tenant-scoped; unspecified filters are
        no-ops. Rows are ordered chronologically by ``occurred_at, id``.
        The cursor uses the same ``(occurred_at_iso, id)`` codec as the
        timeline so a paginated client can resume the report across
        requests.

        Returns rows that expose at least ``id``, ``event_type``,
        ``actor_id``, ``actor_type``, ``subject_type``, ``subject_id``,
        ``occurred_at``, ``correlation_id``, and ``payload`` -- the
        application layer reads those attributes to project the wire
        shape.
        """


__all__ = [
    "AuditEventStore",
]
