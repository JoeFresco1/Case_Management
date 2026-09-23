"""Retention policy publish route (P08-US3 / FM-040).

Registers ``POST /api/v1/admin/retention-policies`` onto the shared
:mod:`retention.common` router. Authorization gate:
``RETENTION_POLICY_PUBLISH`` (process_admin only), evaluated
server-side by the use case (deny-by-default, AGENTS.md §4.R12).
"""

from __future__ import annotations

from fastapi import Depends, Header, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.retention.disposition import (
    PublishRetentionPolicyCommand,
    publish_retention_policy,
)
from app.application.retention.http_dependencies import RetentionHttpDependencyProvider
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.domain.retention import RetentionCategory
from app.interfaces.http.dependencies import get_retention_dependencies
from app.interfaces.http.schemas.retention import (
    PublishRetentionPolicyRequest,
    RetentionPolicyResponse,
)

from .common import (
    _policy_repository,
    _policy_to_response,
    _require_idempotency_key,
    router,
)

# ---------------------------------------------------------------------------
# POST /admin/retention-policies — publish a policy version
# ---------------------------------------------------------------------------


@router.post(
    "/retention-policies",
    response_model=RetentionPolicyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Publish a new version of a retention policy",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks retention.policy_publish",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Idempotency-Key reuse with a different payload",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid category / period / target_key",
        },
    },
)
async def publish_retention_policy_endpoint(
    body: PublishRetentionPolicyRequest,
    request: Request,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: RetentionHttpDependencyProvider = Depends(get_retention_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RetentionPolicyResponse:
    """Publish a new retention policy version.

    The next ``version_number`` is resolved by the repository per
    ``(tenant_id, category, target_key)``; the new row is persisted
    in ``published`` status alongside the
    ``RetentionPolicyPublished`` audit row in one transaction.

    Contracts: ``Idempotency-Key`` is required (transport retries replay
    via ``IdempotencyMiddleware``).

    Args:
        body: Policy category, target key, and period in days.
        request: Current HTTP request (reaches the policy repository).
        user: Authenticated actor (tenant + roles).
        db: Request-scoped database session (committed here).
        clock: Injectable clock (deterministic tests).
        dependencies: Session-bound port provider from the composition root.
        idempotency_key: Raw ``Idempotency-Key`` header.

    Returns:
        The published policy projection.

    Raises:
        HTTPException: 400 on a missing/invalid key; 403/409/422 bubble
            from the use case via the error handler.
    """
    _require_idempotency_key(idempotency_key)
    cmd = PublishRetentionPolicyCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        category=RetentionCategory(body.category),
        target_key=body.target_key,
        period_days=body.period_days,
    )
    result = await publish_retention_policy(
        cmd,
        policy_repo=_policy_repository(request),
        audit=dependencies.for_session(db).audit,
        clock=clock,
    )
    await db.commit()
    return _policy_to_response(result.policy)
