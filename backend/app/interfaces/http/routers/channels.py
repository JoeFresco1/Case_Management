"""Channels + quarantine router (P04-US4, US5 / FM-027).

Endpoints:

- ``POST   /api/v1/admin/channels``                — enable / reconfigure a channel.
- ``GET    /api/v1/admin/channels``                — list channels for the tenant.
- ``POST   /api/v1/admin/channels/{id}/disable``   — disable a channel.
- ``POST   /api/v1/admin/channels/{id}/captures``  — route a single captured
  item through the channel's policy; bad rows are quarantined.
- ``GET    /api/v1/admin/quarantine``              — operator inspection.
- ``POST   /api/v1/admin/quarantine/{id}/release`` — operator release.
- ``POST   /api/v1/admin/quarantine/{id}/reject``  — operator reject.

# Authorization

Every endpoint requires ``ADMIN_TENANT_CONFIG``. The use case enforces
the same check; both must agree (defense-in-depth).

# Multi-tenant posture

Channels and quarantine records are tenant-scoped. Cross-tenant reads
return ``404`` so the API never confirms the existence of a foreign
tenant's configuration (Constitution V).

# Synthetic posture

Quarantine records carry ``source_label`` and ``channel_key`` only;
they never embed payload data. Operators inspect the diagnostic
``context`` to decide whether to release or reject.

# Channel-kind surface (P04-US4 / FM-027..FM-030)

Supported ``kind`` values are ``import`` / ``api`` / ``file_upload`` /
``email`` / ``scan`` / ``webhook`` (mirroring
:class:`app.domain.channel.ChannelKind`). Watched-folder ingestion does
**not** get its own kind: it maps to ``IMPORT`` with the key
convention ``"folder-<name>"`` (e.g. ``"folder-intake-tray"``) so the
policy vocabulary stays closed and the folder name remains a
tenant-scoped key, not a new channel surface. Mailbox / scan / OCR
engines stay behind injected Protocols or local stubs (AGENTS.md §12);
no vendor SDK is imported here or in the application layer.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.channels import manage_channels
from app.application.channels.http_dependencies import ChannelHttpDependencyProvider
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.domain.channel import (
    ChannelKind,
    QuarantineRecord,
    QuarantineStatus,
    ValidationPolicy,
    ValidationSeverity,
)
from app.domain.errors import ValidationFailed as DomainValidationFailed
from app.interfaces.http.dependencies import get_channel_dependencies
from app.interfaces.http.schemas.channels import (
    ChannelResponse,
    DisableChannelResponse,
    EnableChannelRequest,
    EnableChannelResponse,
    InspectQuarantineResponse,
    QuarantineCaptureRequest,
    QuarantineCaptureResponse,
    QuarantineRecordResponse,
    ResolveQuarantineRequest,
    ResolveQuarantineResponse,
    ValidationPolicyRequest,
)

router = APIRouter(tags=["admin", "channels", "quarantine"])


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------


_VALID_SEVERITIES: frozenset[str] = frozenset({"quarantine", "reject"})
# Why closed but complete: the router validates the wire ``kind`` before
# the use case runs so unknown kinds fail fast with a 422-shaped
# ``ValidationFailed(field="kind")``. Watched-folder has no entry here
# on purpose — it is ``IMPORT`` with key ``"folder-<name>"`` (see module
# docstring), keeping the surface to the six domain kinds.
_VALID_KINDS: frozenset[str] = frozenset(
    {
        ChannelKind.IMPORT.value,
        ChannelKind.API.value,
        ChannelKind.FILE_UPLOAD.value,
        ChannelKind.EMAIL.value,
        ChannelKind.SCAN.value,
        ChannelKind.WEBHOOK.value,
    }
)
_VALID_QUARANT_STATUSES: frozenset[str] = frozenset(
    {QuarantineStatus.HELD.value, QuarantineStatus.RELEASED.value, QuarantineStatus.REJECTED.value}
)


def _policy_from_request(req: ValidationPolicyRequest) -> ValidationPolicy:
    severities = (
        req.missing_field_severity,
        req.unknown_field_severity,
        req.invalid_enum_severity,
    )
    for sev in severities:
        if sev not in _VALID_SEVERITIES:
            raise DomainValidationFailed(f"unknown severity {sev!r}", field="severity")
    return ValidationPolicy(
        required_fields=tuple(req.required_fields),
        allowed_classifications=tuple(req.allowed_classifications),
        max_record_bytes=req.max_record_bytes,
        missing_field_severity=ValidationSeverity(req.missing_field_severity),
        unknown_field_severity=ValidationSeverity(req.unknown_field_severity),
        invalid_enum_severity=ValidationSeverity(req.invalid_enum_severity),
    )


def _policy_to_dict(policy: ValidationPolicy) -> dict[str, Any]:
    return {
        "required_fields": list(policy.required_fields),
        "allowed_classifications": list(policy.allowed_classifications),
        "max_record_bytes": policy.max_record_bytes,
        "missing_field_severity": policy.missing_field_severity.value,
        "unknown_field_severity": policy.unknown_field_severity.value,
        "invalid_enum_severity": policy.invalid_enum_severity.value,
    }


def _channel_to_response(channel: Any) -> ChannelResponse:
    return ChannelResponse(
        id=str(channel.id),
        tenant_id=str(channel.tenant_id),
        key=channel.key,
        kind=channel.kind.value,
        enabled=channel.enabled,
        policy=_policy_to_dict(channel.policy),
        created_at=channel.created_at.isoformat(),
        updated_at=channel.updated_at.isoformat(),
        created_by=str(channel.created_by),
    )


def _record_to_response(record: QuarantineRecord) -> QuarantineRecordResponse:
    return QuarantineRecordResponse(
        id=str(record.id),
        tenant_id=str(record.tenant_id),
        channel_id=str(record.channel_id),
        channel_key=record.channel_key,
        source_label=record.source_label,
        captured_at=record.captured_at.isoformat(),
        captured_by=str(record.captured_by),
        error_code=record.error_code,
        error_message=record.error_message,
        correlation_id=record.correlation_id,
        status=record.status.value,
        resolved_at=record.resolved_at.isoformat() if record.resolved_at else None,
        resolved_by=str(record.resolved_by) if record.resolved_by else None,
        resolution_note=record.resolution_note,
        context=dict(record.context),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/admin/channels",
    response_model=EnableChannelResponse,
    status_code=status.HTTP_200_OK,
    summary="Enable or reconfigure a channel for the actor's tenant",
)
async def enable_channel(
    payload: EnableChannelRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: ChannelHttpDependencyProvider = Depends(get_channel_dependencies),
) -> EnableChannelResponse:
    """Enable or reconfigure a channel.

    When ``channel_id`` is supplied the existing channel's policy /
    enabled flag is updated; otherwise a fresh row is created. Both
    branches are idempotent on the application layer's transition.

    Accepted ``kind`` values are ``import`` / ``api`` / ``file_upload`` /
    ``email`` / ``scan`` / ``webhook``. Watched-folder ingestion uses
    ``kind="import"`` with key ``"folder-<name>"``. Unknown kinds raise
    ``ValidationFailed(field="kind")`` before any DB touch.
    """
    if payload.kind not in _VALID_KINDS:
        raise DomainValidationFailed(f"unknown channel kind {payload.kind!r}", field="kind")
    repos = dependencies.for_session(db)
    role_repo = repos.role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="channel"),
        role_repository=role_repo,
    )
    channel_repo = repos.channel
    audit_store = repos.audit
    outbox = repos.outbox

    cmd = manage_channels.EnableChannelCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        key=payload.key,
        kind=ChannelKind(payload.kind),
        policy=_policy_from_request(payload.policy),
        enabled=payload.enabled,
        existing_channel_id=(UUID(payload.channel_id) if payload.channel_id else None),
        correlation_id=correlation.correlation_id,
    )
    result = await manage_channels.enable_channel(
        cmd,
        channel_repo=channel_repo,
        audit=audit_store,
        outbox=outbox,
        clock=clock,
    )
    await db.commit()
    return EnableChannelResponse(
        channel=_channel_to_response(result.channel),
        created=result.created,
    )


@router.get(
    "/admin/channels",
    response_model=list[ChannelResponse],
    summary="List channels for the actor's tenant",
)
async def list_channels(
    enabled_only: bool = Query(default=False),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ChannelHttpDependencyProvider = Depends(get_channel_dependencies),
) -> list[ChannelResponse]:
    repos = dependencies.for_session(db)
    role_repo = repos.role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="channel"),
        role_repository=role_repo,
    )
    channel_repo = repos.channel
    channels = await channel_repo.list(user.tenant_id, enabled_only=enabled_only)
    return [_channel_to_response(c) for c in channels]


@router.post(
    "/admin/channels/{channel_id}/disable",
    response_model=DisableChannelResponse,
    status_code=status.HTTP_200_OK,
    summary="Disable a channel (fail-closed for new captures)",
)
async def disable_channel(
    channel_id: UUID,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: ChannelHttpDependencyProvider = Depends(get_channel_dependencies),
) -> DisableChannelResponse:
    repos = dependencies.for_session(db)
    role_repo = repos.role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="channel"),
        role_repository=role_repo,
    )
    channel_repo = repos.channel
    audit_store = repos.audit
    outbox = repos.outbox

    cmd = manage_channels.DisableChannelCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        channel_id=channel_id,
        correlation_id=correlation.correlation_id,
    )
    result = await manage_channels.disable_channel(
        cmd,
        channel_repo=channel_repo,
        audit=audit_store,
        outbox=outbox,
        clock=clock,
    )
    await db.commit()
    return DisableChannelResponse(channel=_channel_to_response(result.channel))


@router.post(
    "/admin/channels/{channel_id}/captures",
    response_model=QuarantineCaptureResponse,
    status_code=status.HTTP_200_OK,
    summary="Route a captured item through the channel's policy",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks admin.tenant_config"},
        status.HTTP_404_NOT_FOUND: {"description": "Channel not found"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Invalid payload"},
    },
)
async def quarantine_capture(
    channel_id: UUID,
    payload: QuarantineCaptureRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: ChannelHttpDependencyProvider = Depends(get_channel_dependencies),
) -> QuarantineCaptureResponse:
    """Route a single captured row through the channel's policy.

    The endpoint is idempotent on the *policy verdict* side (a row that
    fails the same policy twice produces two quarantine records — the
    audit trail records each attempt separately, so duplicates are
    never silently lost).

    The ``Idempotency-Key`` header is required (mirrors
    ``capture.py``): the ``IdempotencyMiddleware`` replays identical
    requests verbatim; the explicit guard keeps the contract visible
    in OpenAPI and fail-closed when the middleware is disabled.
    """
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_idempotency_key",
                "message": "Idempotency-Key header is required for this endpoint",
            },
        )
    repos = dependencies.for_session(db)
    role_repo = repos.role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="channel"),
        role_repository=role_repo,
    )
    channel_repo = repos.channel
    quarantine_repo = repos.quarantine
    audit_store = repos.audit
    outbox = repos.outbox

    cmd = manage_channels.QuarantineCaptureCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        channel_id=channel_id,
        source_label=payload.source_label,
        values=payload.values,
        correlation_id=correlation.correlation_id,
    )
    result = await manage_channels.quarantine_capture(
        cmd,
        channel_repo=channel_repo,
        quarantine_repo=quarantine_repo,
        audit=audit_store,
        outbox=outbox,
        clock=clock,
    )
    await db.commit()
    return QuarantineCaptureResponse(
        accepted=result.decision.accepted,
        error_code=result.decision.error_code or None,
        error_message=result.decision.error_message or None,
        record=(_record_to_response(result.record) if result.record is not None else None),
    )


@router.get(
    "/admin/quarantine",
    response_model=InspectQuarantineResponse,
    summary="List quarantine records for the operator console",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks admin.tenant_config"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Invalid status filter"},
    },
)
async def inspect_quarantine(
    channel_id: UUID | None = Query(default=None),
    record_status: str | None = Query(
        default=None,
        alias="status",
        description="held | released | rejected",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ChannelHttpDependencyProvider = Depends(get_channel_dependencies),
) -> InspectQuarantineResponse:
    """List held quarantine records with bounded ``limit/offset`` pagination.

    ``limit`` defaults to 50 and is capped at 200 (mirrors
    ``Page`` contract ``DEFAULT_LIMIT``/``MAX_LIMIT``); ``offset`` is
    zero-based. Unknown ``status`` values raise ``ValidationFailed``
    (422) without touching the database.
    """
    repos = dependencies.for_session(db)
    role_repo = repos.role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="quarantine_record"),
        role_repository=role_repo,
    )
    status_filter: QuarantineStatus | None = None
    if record_status is not None:
        if record_status not in _VALID_QUARANT_STATUSES:
            raise DomainValidationFailed(
                f"unknown quarantine status {record_status!r}",
                field="status",
            )
        status_filter = QuarantineStatus(record_status)

    quarantine_repo = repos.quarantine
    result = await manage_channels.inspect_quarantine(
        manage_channels.InspectQuarantineQuery(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            channel_id=channel_id,
            status=status_filter,
            limit=limit,
            offset=offset,
        ),
        quarantine_repo=quarantine_repo,
    )
    return InspectQuarantineResponse(
        items=[_record_to_response(r) for r in result.items],
        total=result.total,
    )


@router.post(
    "/admin/quarantine/{quarantine_id}/release",
    response_model=ResolveQuarantineResponse,
    status_code=status.HTTP_200_OK,
    summary="Release a held quarantine record back into the pipeline",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks admin.tenant_config"},
        status.HTTP_404_NOT_FOUND: {"description": "Quarantine record not found"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Record already resolved"},
    },
)
async def release_quarantine(
    quarantine_id: UUID,
    payload: ResolveQuarantineRequest = ResolveQuarantineRequest(),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: ChannelHttpDependencyProvider = Depends(get_channel_dependencies),
) -> ResolveQuarantineResponse:
    """Release a held record; requires ``Idempotency-Key`` (see captures)."""
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_idempotency_key",
                "message": "Idempotency-Key header is required for this endpoint",
            },
        )
    repos = dependencies.for_session(db)
    role_repo = repos.role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="quarantine_record"),
        role_repository=role_repo,
    )
    quarantine_repo = repos.quarantine
    audit_store = repos.audit
    outbox = repos.outbox

    cmd = manage_channels.ReleaseQuarantineCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        quarantine_id=quarantine_id,
        note=payload.note,
        correlation_id=correlation.correlation_id,
    )
    result = await manage_channels.release_quarantine(
        cmd,
        quarantine_repo=quarantine_repo,
        audit=audit_store,
        outbox=outbox,
        clock=clock,
    )
    await db.commit()
    return ResolveQuarantineResponse(record=_record_to_response(result.record))


@router.post(
    "/admin/quarantine/{quarantine_id}/reject",
    response_model=ResolveQuarantineResponse,
    status_code=status.HTTP_200_OK,
    summary="Reject a quarantine record (terminal)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks admin.tenant_config"},
        status.HTTP_404_NOT_FOUND: {"description": "Quarantine record not found"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Record already resolved"},
    },
)
async def reject_quarantine(
    quarantine_id: UUID,
    payload: ResolveQuarantineRequest = ResolveQuarantineRequest(),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: ChannelHttpDependencyProvider = Depends(get_channel_dependencies),
) -> ResolveQuarantineResponse:
    """Reject a held record as terminal; requires ``Idempotency-Key``."""
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_idempotency_key",
                "message": "Idempotency-Key header is required for this endpoint",
            },
        )
    repos = dependencies.for_session(db)
    role_repo = repos.role
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="quarantine_record"),
        role_repository=role_repo,
    )
    quarantine_repo = repos.quarantine
    audit_store = repos.audit
    outbox = repos.outbox

    cmd = manage_channels.RejectQuarantineCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        quarantine_id=quarantine_id,
        note=payload.note,
        correlation_id=correlation.correlation_id,
    )
    result = await manage_channels.reject_quarantine(
        cmd,
        quarantine_repo=quarantine_repo,
        audit=audit_store,
        outbox=outbox,
        clock=clock,
    )
    await db.commit()
    return ResolveQuarantineResponse(record=_record_to_response(result.record))


__all__ = ["router"]
