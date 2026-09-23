"""Migration / coexistence router (P14-US6 / FM-059).

Endpoints:

- ``POST /api/v1/admin/coexistence``                       — draft a v1 policy.
- ``GET  /api/v1/admin/coexistence``                       — list policies.
- ``GET  /api/v1/admin/coexistence/{id}``                  — read one policy.
- ``POST /api/v1/admin/coexistence/{id}/import-evidence``  — append import-job id.
- ``POST /api/v1/admin/coexistence/{id}/migration-state``  — advance migration state.

# Authorization

Every endpoint requires ``ADMIN_TENANT_CONFIG``. The
``POST /admin/coexistence`` endpoint additionally requires the
``real_time_sync_enabled=False`` default — which means a request that
flips ``real_time_sync_enabled=True`` is refused by the application
layer with a 403 (Gated per ADR-016). The application layer's check
is the policy half; the domain layer's ``__post_init__`` is the
deterministic backstop.

# Multi-tenant posture

Coexistence policies are tenant-scoped. Cross-tenant reads return 404
so the API never confirms cross-tenant existence (Constitution V).

# Synthetic posture (AGENTS.md §18)

The first slice only accepts the ``synthetic-legacy`` source system
plus the Gated named connectors (``onbase`` / ``panzura`` /
``sharepoint``). The router does not auto-create policies; the
operator exercises the lifecycle to verify the audit chain.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.migration import coexistence as manage_coexistence
from app.application.migration.http_dependencies import MigrationHttpDependencyProvider
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.domain.coexistence import (
    CoexistencePolicy,
    MigrationState,
    OwnershipRule,
    is_terminal_migration_state,
)
from app.domain.errors import NotFound
from app.domain.errors import ValidationFailed as DomainValidationFailed
from app.domain.ports import CoexistencePolicyRepository
from app.interfaces.http.dependencies import get_migration_dependencies

router = APIRouter(prefix="/admin/coexistence", tags=["admin", "migration"])


# ---------------------------------------------------------------------------
# Schemas (local; no business behavior — translation between HTTP and use cases)
# ---------------------------------------------------------------------------


class CoexistencePolicyResponse(BaseModel):
    id: str
    tenant_id: str
    key: str
    version: int
    source_systems: list[str]
    ownership: str
    migration_state: str
    real_time_sync_enabled: bool
    provenance_note: str
    sync_evidence: list[str]
    is_terminal: bool
    created_at: str
    updated_at: str
    created_by: str
    sync_authorized_by: str | None = None
    sync_authorized_at: str | None = None
    ownership_authorized_by: str | None = None
    ownership_authorized_at: str | None = None


class DefineCoexistencePolicyRequest(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    source_systems: list[str] = Field(min_length=1)
    ownership: str
    migration_state: str
    provenance_note: str = ""
    real_time_sync_enabled: bool = False


class DefineCoexistencePolicyResponse(BaseModel):
    policy: CoexistencePolicyResponse


class RecordImportEvidenceRequest(BaseModel):
    import_job_id: UUID


class RecordImportEvidenceResponse(BaseModel):
    policy: CoexistencePolicyResponse


class SetMigrationStateRequest(BaseModel):
    new_state: str


class SetMigrationStateResponse(BaseModel):
    policy: CoexistencePolicyResponse


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------


def _policy_to_response(policy: CoexistencePolicy) -> CoexistencePolicyResponse:
    return CoexistencePolicyResponse(
        id=str(policy.id),
        tenant_id=str(policy.tenant_id),
        key=policy.key,
        version=policy.version,
        source_systems=list(policy.source_systems),
        ownership=policy.ownership.value,
        migration_state=policy.migration_state.value,
        real_time_sync_enabled=policy.real_time_sync_enabled,
        provenance_note=policy.provenance_note,
        sync_evidence=[str(jid) for jid in policy.sync_evidence],
        is_terminal=is_terminal_migration_state(policy.migration_state),
        created_at=policy.created_at.isoformat(),
        updated_at=policy.updated_at.isoformat(),
        created_by=str(policy.created_by),
        sync_authorized_by=(str(policy.sync_authorized_by) if policy.sync_authorized_by else None),
        sync_authorized_at=(
            policy.sync_authorized_at.isoformat() if policy.sync_authorized_at else None
        ),
        ownership_authorized_by=(
            str(policy.ownership_authorized_by) if policy.ownership_authorized_by else None
        ),
        ownership_authorized_at=(
            policy.ownership_authorized_at.isoformat() if policy.ownership_authorized_at else None
        ),
    )


# ---------------------------------------------------------------------------
# Coexistence repository — placeholder mirror of the connector router
# pattern. The first slice ships an in-memory repository at the
# application layer (no SQL adapter yet); the router raises ``NotFound``
# on every read so the endpoints are honest about the current state.
# The full SQL implementation lands alongside the first named
# coexistence policy authorization.
# ---------------------------------------------------------------------------


def _build_policy_repo(db: AsyncSession) -> CoexistencePolicyRepository:  # noqa: ARG001 — db reserved for the SQL adapter
    """Return the router-local coexistence policy placeholder.

    Why the annotation is the domain port: the placeholder implements
    the :class:`CoexistencePolicyRepository` shape (``get`` /
    ``get_by_key`` / ``get_latest_version`` / ``list`` / ``add`` /
    ``update``) so the annotation documents the seam the future SQL
    adapter will fill. Until then every read returns ``None`` / ``[]``
    and writes are no-ops.
    """

    class _PlaceholderCoexistencePolicyRepository:
        async def get(self, tenant_id: UUID, policy_id: UUID) -> CoexistencePolicy | None:
            return None

        async def get_by_key(self, tenant_id: UUID, key: str) -> CoexistencePolicy | None:
            return None

        async def get_latest_version(self, tenant_id: UUID, key: str) -> CoexistencePolicy | None:
            return None

        async def list(
            self,
            tenant_id: UUID,
            *,
            limit: int = 50,
            offset: int = 0,
        ) -> list[CoexistencePolicy]:
            return []

        async def add(self, policy: CoexistencePolicy) -> None:
            return None

        async def update(self, policy: CoexistencePolicy) -> None:
            return None

    return _PlaceholderCoexistencePolicyRepository()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=DefineCoexistencePolicyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Define a v1 coexistence policy for the actor's tenant",
)
async def define_coexistence_policy(
    payload: DefineCoexistencePolicyRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: MigrationHttpDependencyProvider = Depends(get_migration_dependencies),
) -> DefineCoexistencePolicyResponse:
    """Draft a fresh v1 coexistence policy.

    The default ``real_time_sync_enabled=False`` produces a policy
    that names source systems and migration state but does NOT
    declare a sync channel. Setting the flag to ``True`` is Gated per
    ADR-016: the application layer refuses the request (the
    authorization record is required and no role grants it today);
    the domain refuses the row construction regardless of caller.
    """
    repositories = dependencies.for_session(db)
    role_repo = repositories.role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="coexistence_policy"),
        role_repository=role_repo,
    )

    if payload.real_time_sync_enabled:
        # Gated per ADR-016 / Constitution V.10 / XII.5. The application
        # layer surfaces this as a 403; the domain would refuse anyway
        # without sync_authorized_by.
        raise HTTPException(
            status_code=403,
            detail="real_time_sync_enabled=True is Gated per ADR-016; "
            "no role grants the named-implement action today",
        )

    try:
        ownership = OwnershipRule(payload.ownership)
    except ValueError as err:
        raise DomainValidationFailed(
            f"unknown ownership rule {payload.ownership!r}",
            field="ownership",
        ) from err
    try:
        migration_state = MigrationState(payload.migration_state)
    except ValueError as err:
        raise DomainValidationFailed(
            f"unknown migration state {payload.migration_state!r}",
            field="migration_state",
        ) from err

    policy_repo = _build_policy_repo(db)
    audit_store = repositories.audit
    outbox = repositories.outbox

    cmd = manage_coexistence.DefineCoexistencePolicyCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        key=payload.key,
        source_systems=tuple(payload.source_systems),
        ownership=ownership,
        migration_state=migration_state,
        provenance_note=payload.provenance_note,
        real_time_sync_enabled=payload.real_time_sync_enabled,
        correlation_id=correlation.correlation_id,
    )
    result = await manage_coexistence.define_coexistence_policy(
        cmd,
        policy_repo=policy_repo,
        audit=audit_store,
        outbox=outbox,
        clock=clock,
    )
    await db.commit()
    return DefineCoexistencePolicyResponse(policy=_policy_to_response(result.policy))


@router.get(
    "",
    response_model=list[CoexistencePolicyResponse],
    summary="List coexistence policies for the actor's tenant",
)
async def list_coexistence_policies(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: MigrationHttpDependencyProvider = Depends(get_migration_dependencies),
) -> list[CoexistencePolicyResponse]:
    role_repo = dependencies.for_session(db).role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="coexistence_policy"),
        role_repository=role_repo,
    )
    policy_repo = _build_policy_repo(db)
    rows = await manage_coexistence.list_coexistence_policies(
        manage_coexistence.ListCoexistencePoliciesQuery(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            limit=limit,
            offset=offset,
        ),
        policy_repo=policy_repo,
    )
    return [_policy_to_response(p) for p in rows]


@router.get(
    "/{policy_id}",
    response_model=CoexistencePolicyResponse,
    summary="Read a single coexistence policy",
)
async def get_coexistence_policy(
    policy_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: MigrationHttpDependencyProvider = Depends(get_migration_dependencies),
) -> CoexistencePolicyResponse:
    role_repo = dependencies.for_session(db).role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="coexistence_policy"),
        role_repository=role_repo,
    )
    policy_repo = _build_policy_repo(db)
    try:
        policy = await manage_coexistence.get_coexistence_policy(
            manage_coexistence.GetCoexistencePolicyQuery(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                policy_id=policy_id,
            ),
            policy_repo=policy_repo,
        )
    except NotFound as err:
        raise HTTPException(status_code=404, detail="coexistence policy not found") from err
    return _policy_to_response(policy)


@router.post(
    "/{policy_id}/import-evidence",
    response_model=RecordImportEvidenceResponse,
    status_code=status.HTTP_200_OK,
    summary="Append an import-job id to the policy's evidence trail",
)
async def record_import_evidence(
    policy_id: UUID,
    payload: RecordImportEvidenceRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: MigrationHttpDependencyProvider = Depends(get_migration_dependencies),
) -> RecordImportEvidenceResponse:
    repositories = dependencies.for_session(db)
    role_repo = repositories.role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="coexistence_policy"),
        role_repository=role_repo,
    )
    policy_repo = _build_policy_repo(db)
    audit_store = repositories.audit
    outbox = repositories.outbox
    try:
        result = await manage_coexistence.record_import_evidence(
            manage_coexistence.RecordImportEvidenceCommand(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                policy_id=policy_id,
                import_job_id=payload.import_job_id,
                correlation_id=correlation.correlation_id,
            ),
            policy_repo=policy_repo,
            audit=audit_store,
            outbox=outbox,
            clock=clock,
        )
    except NotFound as err:
        raise HTTPException(status_code=404, detail="coexistence policy not found") from err
    await db.commit()
    return RecordImportEvidenceResponse(policy=_policy_to_response(result.policy))


@router.post(
    "/{policy_id}/migration-state",
    response_model=SetMigrationStateResponse,
    status_code=status.HTTP_200_OK,
    summary="Advance the policy's migration state (operator-driven)",
)
async def set_migration_state(
    policy_id: UUID,
    payload: SetMigrationStateRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: MigrationHttpDependencyProvider = Depends(get_migration_dependencies),
) -> SetMigrationStateResponse:
    repositories = dependencies.for_session(db)
    role_repo = repositories.role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="coexistence_policy"),
        role_repository=role_repo,
    )
    try:
        new_state = MigrationState(payload.new_state)
    except ValueError as err:
        raise DomainValidationFailed(
            f"unknown migration state {payload.new_state!r}",
            field="new_state",
        ) from err
    policy_repo = _build_policy_repo(db)
    audit_store = repositories.audit
    outbox = repositories.outbox
    try:
        result = await manage_coexistence.set_migration_state(
            manage_coexistence.SetMigrationStateCommand(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                policy_id=policy_id,
                new_state=new_state,
                correlation_id=correlation.correlation_id,
            ),
            policy_repo=policy_repo,
            audit=audit_store,
            outbox=outbox,
            clock=clock,
        )
    except NotFound as err:
        raise HTTPException(status_code=404, detail="coexistence policy not found") from err
    await db.commit()
    return SetMigrationStateResponse(policy=_policy_to_response(result.policy))


__all__ = ["router"]
