"""Worker-home router: dashboard config + preferences + admin policy (T5 worker UX).

# INTEGRATION_SEAM: HTTP entry

Endpoints (all tenant-scoped; ``/api/v1/home`` for workers and
``/api/v1/admin/preference-policy`` for admins):

- ``GET /home/dashboard`` — own dashboard config (404 with a stable
  ``not_found`` code when never saved so the home view renders defaults).
- ``PUT /home/dashboard`` — replace own config (``Idempotency-Key``
  required; cross-user writes are ``Forbidden`` by the use case).
- ``GET /home/preferences`` — raw stored user values (pre-policy).
- ``PUT /home/preferences`` — replace own values (``Idempotency-Key``
  required; closed vocabulary enforced by the domain).
- ``GET /home/preferences/effective`` — merged view: user values through
  the admin policy (locked keys return the admin value with
  ``locked=True`` — acceptance: admin policy overrides user preference).
- ``GET /admin/preference-policy`` — tenant policy (any authenticated
  actor may read).
- ``PUT /admin/preference-policy`` — publish tenant policy
  (``ADMIN_TENANT_CONFIG`` only; ``Idempotency-Key`` required).

Idempotency: mutations require the ``Idempotency-Key`` header
(AGENTS.md §10); the ``IdempotencyMiddleware`` replays successful
responses under ``/api/v1``.
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.worker_home import worker_home as worker_home_uc
from app.application.worker_home.http_dependencies import WorkerHomeHttpDependencyProvider
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.domain.worker_home import PreferencePolicy
from app.interfaces.http.schemas.worker_home import (
    DashboardResponse,
    EffectivePreferencesResponse,
    PreferencePolicyResponse,
    PublishPolicyRequest,
    SaveDashboardRequest,
    SavePreferencesRequest,
    StoredPreferencesResponse,
    dashboard_to_response,
    effective_to_response,
    policy_to_response,
)

router = APIRouter(tags=["worker-home"])


def get_worker_home_dependencies(request: Request) -> WorkerHomeHttpDependencyProvider:
    """Resolve the worker-home provider installed by the composition root."""
    provider = getattr(request.app.state, "worker_home_http_dependencies", None)
    if provider is None:
        raise RuntimeError("worker-home HTTP dependencies are not configured")
    return cast(WorkerHomeHttpDependencyProvider, provider)


def _require_idempotency_key(idempotency_key: str | None) -> str:
    """Refuse mutations without an ``Idempotency-Key`` (AGENTS.md §10)."""
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    return idempotency_key.strip()


@router.get(
    "/home/dashboard",
    response_model=DashboardResponse,
    summary="Read the actor's home dashboard config",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No dashboard saved yet"}},
)
async def read_dashboard(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: WorkerHomeHttpDependencyProvider = Depends(get_worker_home_dependencies),
) -> DashboardResponse:
    """Return the actor's dashboard config or 404 when never saved."""
    repos = dependencies.for_session(db)
    config = await worker_home_uc.get_dashboard(
        tenant_id=user.tenant_id, user_id=user.user_id, dashboards=repos.dashboards
    )
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "no dashboard saved"},
        )
    return dashboard_to_response(config)


@router.put(
    "/home/dashboard",
    response_model=DashboardResponse,
    summary="Replace the actor's home dashboard config",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Bad widget/grouping/sort"},
    },
)
async def save_dashboard(
    body: SaveDashboardRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: WorkerHomeHttpDependencyProvider = Depends(get_worker_home_dependencies),
    clock: Clock = Depends(get_clock),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DashboardResponse:
    """Replace the actor's home layout (favorites, shortcuts, task pane)."""
    _require_idempotency_key(idempotency_key)
    repos = dependencies.for_session(db)
    config = await worker_home_uc.save_dashboard(
        worker_home_uc.SaveDashboardCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            target_user_id=user.user_id,
            widgets=tuple(body.widgets),
            favorites=tuple(body.favorites),
            shortcuts=tuple((entry.label, entry.target) for entry in body.shortcuts),
            grouping=body.grouping,
            sort=body.sort,
            task_pane_case_id=body.task_pane_case_id,
        ),
        dashboards=repos.dashboards,
        audit=repos.audit,
        outbox=repos.outbox,
        clock=clock,
    )
    await db.commit()
    return dashboard_to_response(config)


@router.get(
    "/home/preferences",
    response_model=StoredPreferencesResponse,
    summary="Read the actor's stored preference values (pre-policy)",
)
async def read_preferences(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: WorkerHomeHttpDependencyProvider = Depends(get_worker_home_dependencies),
) -> StoredPreferencesResponse:
    """Return raw stored values (empty when never saved)."""
    repos = dependencies.for_session(db)
    stored = await repos.preferences.get(tenant_id=user.tenant_id, user_id=user.user_id)
    values: dict[str, str] = dict(stored.values) if stored is not None else {}
    return StoredPreferencesResponse(user_id=user.user_id, tenant_id=user.tenant_id, values=values)


@router.put(
    "/home/preferences",
    response_model=StoredPreferencesResponse,
    summary="Replace the actor's preference values",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Unknown key/value"},
    },
)
async def save_preferences(
    body: SavePreferencesRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: WorkerHomeHttpDependencyProvider = Depends(get_worker_home_dependencies),
    clock: Clock = Depends(get_clock),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> StoredPreferencesResponse:
    """Replace the actor's preferences (admin policy still applies on read)."""
    _require_idempotency_key(idempotency_key)
    repos = dependencies.for_session(db)
    record = await worker_home_uc.save_preferences(
        worker_home_uc.SavePreferencesCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            target_user_id=user.user_id,
            values=dict(body.values),
        ),
        preferences=repos.preferences,
        audit=repos.audit,
        outbox=repos.outbox,
        clock=clock,
    )
    await db.commit()
    return StoredPreferencesResponse(
        user_id=record.user_id, tenant_id=record.tenant_id, values=dict(record.values)
    )


@router.get(
    "/home/preferences/effective",
    response_model=EffectivePreferencesResponse,
    summary="Read the actor's effective preferences (admin policy applied)",
)
async def read_effective_preferences(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: WorkerHomeHttpDependencyProvider = Depends(get_worker_home_dependencies),
) -> EffectivePreferencesResponse:
    """Return merged preferences where locked admin values win."""
    repos = dependencies.for_session(db)
    resolved = await worker_home_uc.effective_preferences(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        preferences=repos.preferences,
        policies=repos.policies,
    )
    return effective_to_response(user_id=user.user_id, tenant_id=user.tenant_id, resolved=resolved)


@router.get(
    "/admin/preference-policy",
    response_model=PreferencePolicyResponse,
    summary="Read the tenant preference policy",
)
async def read_policy(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: WorkerHomeHttpDependencyProvider = Depends(get_worker_home_dependencies),
) -> PreferencePolicyResponse:
    """Return the tenant policy (empty maps when never published)."""
    repos = dependencies.for_session(db)
    policy = await repos.policies.get(tenant_id=user.tenant_id)
    if policy is None:
        policy = PreferencePolicy.create(tenant_id=user.tenant_id)
    return policy_to_response(policy)


@router.put(
    "/admin/preference-policy",
    response_model=PreferencePolicyResponse,
    summary="Publish the tenant preference policy (admin only)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks admin.tenant_config"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Bad locked/allowed values"},
    },
)
async def publish_policy(
    body: PublishPolicyRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: WorkerHomeHttpDependencyProvider = Depends(get_worker_home_dependencies),
    clock: Clock = Depends(get_clock),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PreferencePolicyResponse:
    """Publish locked values + allowlists (``ADMIN_TENANT_CONFIG`` only)."""
    _require_idempotency_key(idempotency_key)
    repos = dependencies.for_session(db)
    policy = await worker_home_uc.publish_policy(
        worker_home_uc.PublishPolicyCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            locked=dict(body.locked),
            allowed={key: list(values) for key, values in body.allowed.items()},
        ),
        policies=repos.policies,
        audit=repos.audit,
        outbox=repos.outbox,
        clock=clock,
    )
    await db.commit()
    return policy_to_response(policy)


__all__ = [
    "get_worker_home_dependencies",
    "publish_policy",
    "read_dashboard",
    "read_effective_preferences",
    "read_policy",
    "read_preferences",
    "router",
    "save_dashboard",
    "save_preferences",
]
