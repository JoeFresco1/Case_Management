"""Inbound email capture router (P04-US6 / FM-028).

Endpoint:

- ``POST /api/v1/capture/email`` — capture one approved inbound email
  as a document on a case, with channel + message provenance, deduped
  by RFC 5322 ``Message-Id``. A duplicate ``Message-Id`` returns the
  prior result without writing a second document.

# Authorization

The endpoint authorizes against ``document.upload`` on the supplied
case — the same gate that guards the direct upload path. A caller who
can upload a document to a case can also file an inbound email against
that case; no new role is required for the first slice.

# Idempotency — two layers

1. **HTTP ``Idempotency-Key`` header** (AGENTS.md §10): replay by
   request fingerprint; applies to every mutating endpoint under
   ``/api/v1`` via the middleware.
2. **Content idempotency on ``Message-Id``** (this slice): the use
   case looks up ``CapturedEmailRepository.get_by_message_id`` before
   writing anything and returns the prior result on a hit. The two
   layers compose — a retried POST with the same ``Idempotency-Key``
   AND same ``Message-Id`` still hits the same artifact.

# Multi-tenant posture

Captures are tenant-scoped. A cross-tenant Message-Id lookup returns
the prior result for the actor's tenant only; the
``CapturedEmailRepository.get_by_message_id`` filter is
``WHERE tenant_id = :tenant_id``.

# Synthetic posture

Only the *transport* is synthetic in this slice — the JSON body is
what the operator UI sends today; the Graph / IMAP connector (P14)
will feed the application layer directly. Email bodies are stored as
opaque ``message/rfc822`` bytes; the platform never parses them.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.capture import email_capture as email_capture_uc
from app.application.capture import scan_capture as scan_capture_uc
from app.application.capture.http_dependencies import CaptureHttpDependencyProvider
from app.config import Settings, get_settings
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.domain.errors import ValidationFailed
from app.domain.ports import DocumentStore
from app.interfaces.http.dependencies import get_capture_dependencies
from app.interfaces.http.schemas.capture import (
    CapturedEmailResponse,
    CaptureEmailRequest,
    CaptureEmailResponse,
    CaptureScanRequest,
    CaptureScanResponse,
    decode_body_b64,
)
from app.interfaces.http.schemas.documents import document_to_response

router = APIRouter(tags=["capture"])


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------


def _get_document_store(request: Request) -> DocumentStore:
    """Resolve the configured ``DocumentStore`` adapter.

    Mirrors ``app.interfaces.http.routers.documents.get_document_store``.
    Kept local so the capture router does not import the documents
    router (which would pull in the multipart handling we do not
    need).
    """
    store = getattr(request.app.state, "document_store", None)
    if store is None:
        raise RuntimeError("document store is not configured")
    return cast(DocumentStore, store)


class _SettingsScanAuthorization:
    """Settings-backed :class:`ScanAuthorizationProvider` adapter.

    The flag is intentionally separate from ``app_env`` so the operator
    can disable scan capture even in ``local`` / ``test`` without
    disabling other capture channels. Default is **disabled**: the
    feature is Gated (FM-029 / AGENTS.md §3) and must be opted into
    explicitly. See ADR-006 for the rationale.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def is_enabled(self) -> bool:
        return bool(getattr(self._settings, "scan_capture_enabled", False))


def _captured_to_response(captured: Any) -> CapturedEmailResponse:
    return CapturedEmailResponse(
        id=str(captured.id),
        tenant_id=str(captured.tenant_id),
        channel_id=str(captured.channel_id),
        channel_key=captured.channel_key,
        message_id=captured.message_id,
        sender=captured.sender,
        recipients=list(captured.recipients),
        subject=captured.subject,
        captured_at=captured.captured_at.isoformat(),
        captured_by=str(captured.captured_by),
        destination=captured.destination,
        artifact_id=str(captured.artifact_id),
        artifact_kind=captured.artifact_kind,
        correlation_id=captured.correlation_id,
        case_id=str(captured.case_id) if captured.case_id is not None else None,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/capture/email (P04-US6 / FM-028)
# ---------------------------------------------------------------------------


@router.post(
    "/capture/email",
    response_model=CaptureEmailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Capture one inbound email as a case document with provenance",
    responses={
        status.HTTP_200_OK: {"description": "Duplicate Message-Id — prior result replayed"},
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.upload"},
        status.HTTP_404_NOT_FOUND: {"description": "Case or channel not found"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid payload (empty Message-Id, oversized body, etc.)"
        },
    },
)
async def capture_email(
    payload: CaptureEmailRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: CaptureHttpDependencyProvider = Depends(get_capture_dependencies),
    document_store: DocumentStore = Depends(_get_document_store),
) -> CaptureEmailResponse:
    """Capture one inbound email.

    Body shape:

    ```json
    {
      "channel_id": "<uuid>",
      "message_id": "<rfc5322 message id>",
      "sender": "alice@example.test",
      "recipients": ["bob@example.test"],
      "subject": "Hello",
      "case_id": "<uuid>",
      "body_b64": "<base64 of RFC 5322 bytes>"
    }
    ```

    Returns ``201`` on a fresh capture and ``200`` (with ``replayed: true``)
    on a duplicate ``Message-Id``. The ``Idempotency-Key`` header is
    required by the middleware for the 201 path; replays return the
    prior response verbatim.
    """
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_idempotency_key",
                "message": "Idempotency-Key header is required for this endpoint",
            },
        )

    body = decode_body_b64(payload.body_b64)

    try:
        channel_id = UUID(payload.channel_id)
    except ValueError as exc:
        raise ValidationFailed(
            f"channel_id is not a valid UUID: {payload.channel_id!r}",
            field="channel_id",
        ) from exc

    case_id: UUID | None = None
    if payload.case_id:
        try:
            case_id = UUID(payload.case_id)
        except ValueError as exc:
            raise ValidationFailed(
                f"case_id is not a valid UUID: {payload.case_id!r}",
                field="case_id",
            ) from exc

    repos = dependencies.for_session(db)
    channel_repo = repos.channel
    case_repo = repos.case
    document_repo = repos.document
    captured_repo = repos.captured_email
    audit_store = repos.audit
    outbox = repos.outbox

    cmd = email_capture_uc.CaptureEmailCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        channel_id=channel_id,
        message_id=payload.message_id,
        sender=payload.sender,
        recipients=tuple(payload.recipients),
        subject=payload.subject,
        body=body,
        case_id=case_id,
        correlation_id=correlation.correlation_id,
    )

    # The authorization decision lives inside the use case so the
    # DocumentUpload gate is evaluated once, atomically with the
    # case-existence check.
    result = await email_capture_uc.capture_email(
        cmd,
        channel_repo=channel_repo,
        case_repo=case_repo,
        document_repo=document_repo,
        document_store=document_store,
        captured_email_repo=captured_repo,
        audit=audit_store,
        outbox=outbox,
        clock=clock,
    )
    await db.commit()

    document_dict: dict[str, Any] | None = None
    if result.document is not None and result.document_version is not None:
        document_response = document_to_response(result.document, result.document_version)
        document_dict = document_response.model_dump(mode="json")

    response = CaptureEmailResponse(
        captured=_captured_to_response(result.captured),
        replayed=result.replayed,
        document=document_dict,
    )
    # Override the response status when this is an idempotent replay
    # so the client can distinguish "duplicate message" (200) from
    # "fresh capture" (201) without parsing the body.
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return response


# ---------------------------------------------------------------------------
# POST /api/v1/capture/scan (P04-US7 / FM-029, **Gated**)
# ---------------------------------------------------------------------------


@router.post(
    "/capture/scan",
    response_model=CaptureScanResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Capture one scanned page with its scan-result verdict (Gated)",
    responses={
        status.HTTP_200_OK: {"description": "Scan recorded (clean or infected)"},
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {
            "description": "Scan capture is gated off, or actor lacks admin.tenant_config"
        },
        status.HTTP_404_NOT_FOUND: {"description": "Case or channel not found"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid payload (empty bytes, unknown scan_result, etc.)"
        },
    },
)
async def capture_scan(
    payload: CaptureScanRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: CaptureHttpDependencyProvider = Depends(get_capture_dependencies),
    document_store: DocumentStore = Depends(_get_document_store),
) -> CaptureScanResponse:
    """Capture one scanned page + its scan-result verdict.

    Body shape:

    ```json
    {
      "channel_id": "<uuid>",
      "device_id": "scanner-floor-2",
      "source_label": "Intake counter — 2026-09-02",
      "scan_result": "clean" | "infected" | "inconclusive",
      "scan_detail": "<free-text finding>",
      "case_id": "<uuid>",
      "content_b64": "<base64 of scan bytes>"
    }
    ```

    A clean scan releases the quarantine record and produces a
    ``Document`` on the case in one transaction. An infected /
    inconclusive scan keeps the ``QuarantineRecord`` in ``HELD`` and
    returns ``released=false`` with no document — the operator
    resolves it via ``/api/v1/admin/quarantine/{id}/release|reject``.
    """
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_idempotency_key",
                "message": "Idempotency-Key header is required for this endpoint",
            },
        )

    content = decode_body_b64(payload.content_b64)

    try:
        channel_id = UUID(payload.channel_id)
    except ValueError as exc:
        raise ValidationFailed(
            f"channel_id is not a valid UUID: {payload.channel_id!r}",
            field="channel_id",
        ) from exc

    case_id: UUID | None = None
    if payload.case_id:
        try:
            case_id = UUID(payload.case_id)
        except ValueError as exc:
            raise ValidationFailed(
                f"case_id is not a valid UUID: {payload.case_id!r}",
                field="case_id",
            ) from exc

    settings = get_settings()
    repos = dependencies.for_session(db)
    channel_repo = repos.channel
    case_repo = repos.case
    quarantine_repo = repos.quarantine
    document_repo = repos.document
    audit_store = repos.audit
    outbox = repos.outbox

    cmd = scan_capture_uc.ScanCaptureCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        channel_id=channel_id,
        device_id=payload.device_id,
        source_label=payload.source_label,
        scan_result=scan_capture_uc.ScanResult(payload.scan_result),
        scan_detail=payload.scan_detail,
        content=content,
        content_type=payload.content_type,
        file_name=payload.file_name,
        case_id=case_id,
        correlation_id=correlation.correlation_id,
    )
    result = await scan_capture_uc.capture_scan(
        cmd,
        scan_authorization=_SettingsScanAuthorization(settings),
        channel_repo=channel_repo,
        case_repo=case_repo,
        quarantine_repo=quarantine_repo,
        document_repo=document_repo,
        document_store=document_store,
        audit=audit_store,
        outbox=outbox,
        clock=clock,
    )
    await db.commit()

    document_dict: dict[str, Any] | None = None
    if result.document is not None and result.document_version is not None:
        document_response = document_to_response(result.document, result.document_version)
        document_dict = document_response.model_dump(mode="json")

    response = CaptureScanResponse(
        quarantine_id=str(result.quarantine.id),
        channel_id=str(result.quarantine.channel_id),
        channel_key=result.quarantine.channel_key,
        scan_result=payload.scan_result,
        error_code=result.quarantine.error_code,
        released=result.released,
        document=document_dict,
    )
    # Override the response status when the scan is held so the
    # client can distinguish "quarantined" (200) from "released to
    # document" (201) without parsing the body.
    if not result.released:
        response.status_code = status.HTTP_200_OK
    return response


__all__ = ["router"]
