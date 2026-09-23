"""Defensible-deletion evidence report router (FM-042 / P08-US6 / P12-US9).

The chain-of-custody report is surfaced as a per-case read on
``GET /api/v1/retention/evidence/{case_id}``. The endpoint is
mounted under ``/retention`` (not ``/admin/retention``) because the
audience is the compliance officer / records auditor who is not
necessarily the records administrator who publishes the policy.

# Authorization

The endpoint gates on ``RETENTION_POLICY_PUBLISH`` — the same action
the records-admin surfaces (publish / eligible / route) already
require. The defensible-deletion report is a read-only view of the
same data; splitting the action would add vocabulary without adding
a real guard. Future slices may split the action (FM-042 follow-up)
if the compliance role diverges from the records-admin role.

# Tenant isolation

The use case resolves the case via the tenant-scoped
``CaseRepository.get`` so a cross-tenant id surfaces as ``NotFound``
(Constitution V). The use case never confirms cross-tenant
existence. The disposition, policy, and audit repositories are all
tenant-scoped (AGENTS.md §3 R1, §12).

# INTEGRATION_SEAM

The repos are the same ones the disposition router already wires —
the compliance view reuses the in-memory seam placeholders. The SQL
swap is a one-line change in ``app.state`` wiring.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.retention.evidence_report import build_evidence_report
from app.application.retention.http_dependencies import RetentionHttpDependencyProvider
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.domain.ports import DispositionRequestRepository, RetentionPolicyRepository
from app.interfaces.http.dependencies import get_retention_dependencies
from app.interfaces.http.schemas.retention import (
    AuditChainEntryResponse,
    DispositionEvidenceEntry,
    EvidenceReportResponse,
    PolicyEvidenceEntry,
)

router = APIRouter(prefix="/retention", tags=["retention", "compliance"])


# ---------------------------------------------------------------------------
# Provider + repository factories
# ---------------------------------------------------------------------------


def _policy_repository(request: Request) -> RetentionPolicyRepository:
    """Return the process-scoped policy repository from ``app.state``.

    Mirrors the disposition router's factory so the compliance view
    sees the same data the records-admin view mutates. Depends only on
    the ``RetentionPolicyRepository`` domain port.

    Raises:
        RuntimeError: if an app was created without the shared repository.
    """
    repo = getattr(request.app.state, "retention_policy_repository", None)
    if repo is None:
        raise RuntimeError("retention policy repository is not configured")
    return cast(RetentionPolicyRepository, repo)


def _disposition_repository(request: Request) -> DispositionRequestRepository:
    """Return the process-scoped disposition repository from ``app.state``.

    Raises:
        RuntimeError: if an app was created without the shared repository.
    """
    repo = getattr(request.app.state, "disposition_repository", None)
    if repo is None:
        raise RuntimeError("disposition repository is not configured")
    return cast(DispositionRequestRepository, repo)


# ---------------------------------------------------------------------------
# GET /retention/evidence/{case_id} — chain-of-custody report
# ---------------------------------------------------------------------------


@router.get(
    "/evidence/{case_id}",
    response_model=EvidenceReportResponse,
    summary="Defensible-deletion evidence report for a case",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks retention.policy_publish",
        },
        status.HTTP_404_NOT_FOUND: {"description": "Case not found"},
    },
)
async def get_evidence_report_endpoint(
    case_id: UUID = Path(..., description="Target case id (tenant-scoped)"),
    request: Request = ...,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: RetentionHttpDependencyProvider = Depends(get_retention_dependencies),
) -> EvidenceReportResponse:
    """Assemble the per-case chain-of-custody evidence packet.

    The report is a snapshot of the system of record at
    ``generated_at`` (the request's commit clock). It carries:

    - The case identity (id, human reference, created_at).
    - The published retention policy that authorised the
      disposition decision (id + version_number + published_at +
      published_by).
    - Every ``DispositionRequest`` for the case in chronological
      order (pending / approved / rejected all surface so the
      auditor sees the full decision history).
    - The bounded audit chain of the disposition-related event
      types so the chain of custody is re-walkable without
      external joins.

    A case with no approved disposition is returned with
    ``disposed=false`` and an empty ``dispositions`` list — the
    absence is the proof of "never silent deletion" (P08 §2).
    """
    repositories = dependencies.for_session(db)
    report = await build_evidence_report(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        case_id=case_id,
        case_repo=repositories.case,
        policy_repo=_policy_repository(request),
        disposition_repo=_disposition_repository(request),
        audit=repositories.audit,
        clock=clock,
    )
    return EvidenceReportResponse(
        case_id=report.case_id,
        case_reference=report.case_reference,
        case_created_at=report.case_created_at,
        tenant_id=report.tenant_id,
        kind=report.kind,  # type: ignore[arg-type]
        generated_at=report.generated_at,
        actor_id=report.actor_id,
        disposed=report.disposed,
        policy=(
            PolicyEvidenceEntry(
                policy_id=report.policy.policy_id,
                category=report.policy.category,  # type: ignore[arg-type]
                target_key=report.policy.target_key,
                period_days=report.policy.period_days,
                version_number=report.policy.version_number,
                status=report.policy.status,  # type: ignore[arg-type]
                published_at=report.policy.published_at,
                published_by=report.policy.published_by,
            )
            if report.policy is not None
            else None
        ),
        dispositions=[
            DispositionEvidenceEntry(
                request_id=d.request_id,
                category=d.category,  # type: ignore[arg-type]
                status=d.status,  # type: ignore[arg-type]
                surfaced_at=d.surfaced_at,
                surfaced_by=d.surfaced_by,
                decided_at=d.decided_at,
                decided_by=d.decided_by,
                rationale=d.rationale,
            )
            for d in report.dispositions
        ],
        audit_chain=[
            AuditChainEntryResponse(
                event_id=entry.event_id,
                event_type=entry.event_type,
                actor_id=entry.actor_id,
                actor_type=entry.actor_type,
                subject_type=entry.subject_type,
                subject_id=entry.subject_id,
                occurred_at=entry.occurred_at,
                correlation_id=entry.correlation_id,
                summary=entry.summary,
            )
            for entry in report.audit_chain
        ],
    )


__all__ = ["router"]
