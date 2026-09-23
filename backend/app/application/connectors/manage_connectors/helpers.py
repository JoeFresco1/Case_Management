"""Shared helpers: repository load, audit append, and user proxy for connector use cases.

One small module so :mod:`.draft`, :mod:`.admin`, and :mod:`.retire` share the
audit / authorization plumbing without importing each other. Split out of the
former single-file ``manage_connectors.py`` during the R2 package split.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.domain.connector import Connector
from app.domain.errors import NotFound

from .constants import SUBJECT_TYPE_CONNECTOR


async def _load(
    connector_repo: Any,
    tenant_id: UUID,
    connector_id: UUID,
) -> Connector:
    """Load one connector row or raise :class:`NotFound`.

    Args:
        connector_repo: Tenant-scoped connector repository port.
        tenant_id: The tenant the connector must belong to — cross-tenant
            reads return ``None`` at the repository (Constitution V).
        connector_id: The connector id from the command / query.

    Returns:
        The connector aggregate.

    Raises:
        NotFound: When the repository has no row for this id in this
            tenant, so the API never confirms cross-tenant existence.
    """
    row = await connector_repo.get(tenant_id, connector_id)
    if row is None:
        raise NotFound(f"connector {connector_id} not found in tenant")
    return row  # type: ignore[no-any-return]


@dataclass(frozen=True, slots=True)
class _UserProxy:
    """Duck-typed ``UserContext`` for the policy (see create_case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


def _user_context(cmd: Any) -> Any:
    """Build the policy-facing user context from a command's actor fields.

    Args:
        cmd: A frozen command / query carrying ``tenant_id``, ``actor_id``
            and ``actor_roles`` (every entry point in this package).

    Returns:
        A :class:`_UserProxy` the authorization policy can evaluate.
        Typed ``Any`` to mirror the pre-split duck-typing contract.
    """
    return _UserProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )


async def _emit_audit(
    audit: Any,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    subject_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    correlation_id: str,
    occurred_at: Any,
) -> None:
    """Append one audit row for a connector lifecycle event.

    Args:
        audit: The append-only audit store port.
        tenant_id: Tenant the event belongs to.
        actor_id: The user on whose behalf the event occurred.
        subject_id: The connector id being described.
        event_type: Stable event string from :mod:`.constants`.
        payload: Safe diagnostic payload — keys and scalar state values
            only, never secrets (Constitution V).
        correlation_id: Request correlation id for the timeline.
        occurred_at: UTC timestamp from the injected clock.

    Returns:
        ``None``. Failures propagate so the surrounding transaction
        rolls back — a mutation must never commit without its audit row.
    """
    await audit.append(
        tenant_id=tenant_id,
        event_type=event_type,
        actor_id=actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_CONNECTOR,
        subject_id=subject_id,
        case_id=None,
        correlation_id=correlation_id,
        causation_id=None,
        payload=payload,
        occurred_at=occurred_at,
    )


__all__ = [
    "_UserProxy",
    "_emit_audit",
    "_load",
    "_user_context",
]
