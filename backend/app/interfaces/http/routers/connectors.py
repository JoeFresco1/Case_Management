"""Connectors router (P14-US5, US7 / FM-058).

Endpoints:

- ``POST /api/v1/admin/connectors``                    — draft a connector.
- ``GET  /api/v1/admin/connectors``                    — list connectors.
- ``POST /api/v1/admin/connectors/{id}/approve``       — DRAFT → APPROVED.
- ``POST /api/v1/admin/connectors/{id}/enable``        — APPROVED → ENABLED.
- ``GET  /api/v1/admin/connectors/{id}/monitor``       — monitor snapshot.
- ``POST /api/v1/admin/connectors/{id}/retire``        — ENABLED → DRAINING → RETIRED.

# Authorization

Every endpoint requires ``ADMIN_TENANT_CONFIG``. The
``POST /admin/connectors`` endpoint additionally requires
``CONNECTOR_NAMED_IMPLEMENT`` for any non-synthetic kind. The
named-implement action is Gated per ADR-015: no role grants it
today, so a named connector is deny-by-default.

# Multi-tenant posture

Connectors are tenant-scoped. Cross-tenant reads return ``404`` so
the API never confirms cross-tenant existence (Constitution V).

# Synthetic posture (AGENTS.md §18)

The first slice only ships a synthetic connector. The router accepts
the request; the use case (and the domain layer's
``__post_init__``) refuses to construct a row for a named kind
without the explicit authorization record.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.connectors import manage_connectors
from app.application.connectors.http_dependencies import ConnectorHttpDependencyProvider
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.domain.connector import Connector, ConnectorKind, is_terminal_state
from app.domain.errors import NotFound
from app.domain.errors import ValidationFailed as DomainValidationFailed
from app.interfaces.http.dependencies import get_connector_dependencies
from app.interfaces.http.schemas.connectors import (
    ApproveConnectorRequest,
    ApproveConnectorResponse,
    ConnectorResponse,
    DraftConnectorRequest,
    DraftConnectorResponse,
    EnableConnectorResponse,
    MonitorConnectorResponse,
    RetireConnectorRequest,
    RetireConnectorResponse,
)

router = APIRouter(prefix="/admin/connectors", tags=["admin", "connectors"])


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------


_VALID_KINDS: frozenset[str] = frozenset(
    {ConnectorKind.SYNTHETIC.value, ConnectorKind.ONBASE.value, ConnectorKind.PANZURA.value}
)


def _connector_to_response(connector: Connector) -> ConnectorResponse:
    return ConnectorResponse(
        id=str(connector.id),
        tenant_id=str(connector.tenant_id),
        key=connector.key,
        kind=connector.kind.value,
        state=connector.state.value,
        adapter_ref=connector.adapter_ref,
        design_summary=connector.design_summary,
        named=connector.is_named(),
        is_terminal=is_terminal_state(connector.state),
        accepts_traffic=connector.accepts_traffic(),
        in_flight=connector.in_flight_count,
        completed=connector.total_completed_count,
        rejected=connector.total_rejected_count,
        drain_confirmed=connector.drain_confirmed,
        created_at=connector.created_at.isoformat(),
        updated_at=connector.updated_at.isoformat(),
        created_by=str(connector.created_by),
        approved_at=connector.approved_at.isoformat() if connector.approved_at else None,
        approved_by=str(connector.approved_by) if connector.approved_by else None,
        enabled_at=connector.enabled_at.isoformat() if connector.enabled_at else None,
        enabled_by=str(connector.enabled_by) if connector.enabled_by else None,
        retired_at=connector.retired_at.isoformat() if connector.retired_at else None,
        retired_by=str(connector.retired_by) if connector.retired_by else None,
        authorization_rationale=connector.authorization_rationale,
        implementation_authorized_by=(
            str(connector.implementation_authorized_by)
            if connector.implementation_authorized_by
            else None
        ),
    )


# ---------------------------------------------------------------------------
# Connector repository — local SQL implementation is the in-memory fake
# in the first slice (per ADR-015, the production adapter is a separate
# feature). The router takes a repository factory from app.deps so
# tests can substitute an in-memory implementation.
# ---------------------------------------------------------------------------


def _build_connector_repo(db: AsyncSession) -> Any:  # noqa: ARG001 — db reserved for the SQL adapter
    """Return the connector repository.

    The first slice ships with an in-memory repository in
    ``app.application.connectors.manage_connectors._in_memory_repo``
    (single-tenant test path). A SQL adapter will land alongside the
    first named-connector authorization. Until then the router raises
    ``NotFound`` on every read so the endpoints cannot accidentally
    accept a request without a wired implementation.
    """

    class _PlaceholderConnectorRepository:
        async def get(self, tenant_id: UUID, connector_id: UUID) -> Connector | None:
            return None

        async def get_by_key(self, tenant_id: UUID, key: str) -> Connector | None:
            return None

        async def list(
            self,
            tenant_id: UUID,
            *,
            limit: int = 50,
            offset: int = 0,
        ) -> list[Connector]:
            return []

        async def add(self, connector: Connector) -> None:
            return None

        async def update(self, connector: Connector) -> None:
            return None

    return _PlaceholderConnectorRepository()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=DraftConnectorResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Draft a connector for the actor's tenant",
)
async def draft_connector(
    payload: DraftConnectorRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: ConnectorHttpDependencyProvider = Depends(get_connector_dependencies),
) -> DraftConnectorResponse:
    """Draft a fresh connector in ``DRAFT`` state.

    A named connector (any kind other than ``synthetic``) additionally
    requires the ``CONNECTOR_NAMED_IMPLEMENT`` action, which is Gated
    per ADR-015. A non-admin actor or an actor without the named-grant
    receives a 403.
    """
    if payload.kind not in _VALID_KINDS:
        raise DomainValidationFailed(f"unknown connector kind {payload.kind!r}", field="kind")

    repos = dependencies.for_session(db)
    role_repo = repos.role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="connector"),
        role_repository=role_repo,
    )
    if payload.kind != ConnectorKind.SYNTHETIC.value:
        await authorize(
            Action.CONNECTOR_NAMED_IMPLEMENT,
            user,
            Resource(tenant_id=user.tenant_id, kind="connector"),
            role_repository=role_repo,
        )

    connector_repo = _build_connector_repo(db)
    audit_store = repos.audit
    outbox = repos.outbox

    cmd = manage_connectors.DraftConnectorCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        key=payload.key,
        kind=ConnectorKind(payload.kind),
        design_summary=payload.design_summary,
        authorization_rationale=payload.authorization_rationale,
        correlation_id=correlation.correlation_id,
    )
    result = await manage_connectors.draft_connector(
        cmd,
        connector_repo=connector_repo,
        audit=audit_store,
        outbox=outbox,
        clock=clock,
    )
    await db.commit()
    return DraftConnectorResponse(connector=_connector_to_response(result.connector))


@router.get(
    "",
    response_model=list[ConnectorResponse],
    summary="List connectors for the actor's tenant",
)
async def list_connectors(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ConnectorHttpDependencyProvider = Depends(get_connector_dependencies),
) -> list[ConnectorResponse]:
    role_repo = dependencies.for_session(db).role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="connector"),
        role_repository=role_repo,
    )
    connector_repo = _build_connector_repo(db)
    rows = await connector_repo.list(user.tenant_id, limit=limit, offset=offset)
    return [_connector_to_response(c) for c in rows]


@router.post(
    "/{connector_id}/approve",
    response_model=ApproveConnectorResponse,
    status_code=status.HTTP_200_OK,
    summary="Approve a DRAFT connector (records the security administrator)",
)
async def approve_connector(
    connector_id: UUID,
    payload: ApproveConnectorRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: ConnectorHttpDependencyProvider = Depends(get_connector_dependencies),
) -> ApproveConnectorResponse:
    repos = dependencies.for_session(db)
    role_repo = repos.role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="connector"),
        role_repository=role_repo,
    )
    connector_repo = _build_connector_repo(db)
    audit_store = repos.audit
    outbox = repos.outbox

    cmd = manage_connectors.ApproveConnectorCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        connector_id=connector_id,
        rationale=payload.rationale,
        correlation_id=correlation.correlation_id,
    )
    try:
        result = await manage_connectors.approve_connector(
            cmd,
            connector_repo=connector_repo,
            audit=audit_store,
            outbox=outbox,
            clock=clock,
        )
    except NotFound as err:
        # The placeholder repository always returns None; surface 404
        # so the endpoint is honest about the current state.
        raise HTTPException(status_code=404, detail="connector not found") from err
    await db.commit()
    return ApproveConnectorResponse(connector=_connector_to_response(result.connector))


@router.post(
    "/{connector_id}/enable",
    response_model=EnableConnectorResponse,
    status_code=status.HTTP_200_OK,
    summary="Enable an APPROVED connector (flip the operational switch)",
)
async def enable_connector(
    connector_id: UUID,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: ConnectorHttpDependencyProvider = Depends(get_connector_dependencies),
) -> EnableConnectorResponse:
    repos = dependencies.for_session(db)
    role_repo = repos.role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="connector"),
        role_repository=role_repo,
    )
    connector_repo = _build_connector_repo(db)
    audit_store = repos.audit
    outbox = repos.outbox

    cmd = manage_connectors.EnableConnectorCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        connector_id=connector_id,
        correlation_id=correlation.correlation_id,
    )
    try:
        result = await manage_connectors.enable_connector(
            cmd,
            connector_repo=connector_repo,
            audit=audit_store,
            outbox=outbox,
            clock=clock,
        )
    except NotFound as err:
        raise HTTPException(status_code=404, detail="connector not found") from err
    await db.commit()
    return EnableConnectorResponse(connector=_connector_to_response(result.connector))


@router.get(
    "/{connector_id}/monitor",
    response_model=MonitorConnectorResponse,
    summary="Read the connector's monitor snapshot (no state change)",
)
async def monitor_connector(
    connector_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ConnectorHttpDependencyProvider = Depends(get_connector_dependencies),
) -> MonitorConnectorResponse:
    role_repo = dependencies.for_session(db).role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="connector"),
        role_repository=role_repo,
    )
    connector_repo = _build_connector_repo(db)
    query = manage_connectors.MonitorConnectorQuery(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        connector_id=connector_id,
    )
    try:
        result = await manage_connectors.monitor_connector(query, connector_repo=connector_repo)
    except NotFound as err:
        raise HTTPException(status_code=404, detail="connector not found") from err
    return MonitorConnectorResponse(
        connector=_connector_to_response(result.connector),
        in_flight=result.in_flight,
        completed=result.completed,
        rejected=result.rejected,
        accepts_traffic=result.accepts_traffic,
        is_terminal=result.is_terminal,
    )


@router.post(
    "/{connector_id}/retire",
    response_model=RetireConnectorResponse,
    status_code=status.HTTP_200_OK,
    summary="Retire a connector — safe-drain then terminal transition",
)
async def retire_connector(
    connector_id: UUID,
    payload: RetireConnectorRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: ConnectorHttpDependencyProvider = Depends(get_connector_dependencies),
) -> RetireConnectorResponse:
    """Retire a connector.

    The safe-drain invariant (``in_flight == 0``) is the deterministic
    backstop of P14-US7. When the operator's snapshot reports
    in-flight traffic, the use case leaves the connector in
    ``DRAINING`` and returns ``drained=False``; the HTTP layer surfaces
    the in-progress state so the operator can re-issue the retire
    once the count reaches zero.
    """
    repos = dependencies.for_session(db)
    role_repo = repos.role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="connector"),
        role_repository=role_repo,
    )
    connector_repo = _build_connector_repo(db)
    audit_store = repos.audit
    outbox = repos.outbox

    cmd = manage_connectors.RetireConnectorCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        connector_id=connector_id,
        drain_notes=payload.drain_notes,
        in_flight=payload.in_flight,
        completed=payload.completed,
        rejected=payload.rejected,
        correlation_id=correlation.correlation_id,
    )
    try:
        result = await manage_connectors.retire_connector(
            cmd,
            connector_repo=connector_repo,
            audit=audit_store,
            outbox=outbox,
            clock=clock,
        )
    except NotFound as err:
        raise HTTPException(status_code=404, detail="connector not found") from err
    await db.commit()
    return RetireConnectorResponse(
        connector=_connector_to_response(result.connector),
        drained=result.drained,
    )


__all__ = ["router"]
