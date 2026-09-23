"""Separation plans (validate-only; byte surgery is a later slice)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.ports import AuditEventStore, DocumentRepository, OutboxWriter
from app.domain.separation import (
    PageRange,
    SeparationOperation,
    SeparationPlan,
    SeparationSource,
    plan_join_merge,
    plan_split,
)

from ._common import (
    SEPARATION_PLANNED,
    SUBJECT_TYPE_DOCUMENT,
    append_audit_and_outbox,
    proxy,
    resolve_document,
)


@dataclass(frozen=True, slots=True)
class PlanSplitCommand:
    """Input to :func:`plan_separation_split`."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    source_document_id: UUID
    page_count: int
    ranges: tuple[tuple[int, int], ...]
    reason: str


async def plan_separation_split(
    cmd: PlanSplitCommand,
    *,
    document_repo: DocumentRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> SeparationPlan:
    """Validate a split plan and record the audit trail (no byte writes).

    The source document must exist in-tenant and must not be filed
    (locked); ``page_count`` is the operator-declared batch size the
    plan partitions. The ``SeparationSource`` descriptor projects the
    stored aggregate (tenant, case, current version, lock) so the
    domain rule stays pure.
    """
    await authorize(
        Action.DOCUMENT_READ,
        proxy(tenant_id=cmd.tenant_id, actor_id=cmd.actor_id, actor_roles=cmd.actor_roles),
        Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.source_document_id),
    )
    source = await resolve_document(
        tenant_id=cmd.tenant_id,
        document_id=cmd.source_document_id,
        document_repo=document_repo,
    )
    now = clock.now()
    plan = plan_split(
        tenant_id=cmd.tenant_id,
        case_id=cmd.case_id,
        source=SeparationSource(
            document_id=source.id,
            tenant_id=source.tenant_id,
            case_id=source.case_id,
            version_number=source.current_version,
            page_count=cmd.page_count,
            locked=source.locked,
        ),
        ranges=tuple(PageRange(start_page=a, end_page=b) for a, b in cmd.ranges),
        actor_id=cmd.actor_id,
        reason=cmd.reason,
        now=now,
    )
    await append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=cmd.tenant_id,
        event_type=SEPARATION_PLANNED,
        actor_id=cmd.actor_id,
        subject_type=SUBJECT_TYPE_DOCUMENT,
        subject_id=cmd.source_document_id,
        case_id=cmd.case_id,
        payload=plan.record.describe(),
        now=now,
    )
    return plan


@dataclass(frozen=True, slots=True)
class PlanJoinMergeCommand:
    """Input to :func:`plan_separation_join_merge`."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    operation: str
    source_document_ids: tuple[UUID, ...]
    page_counts: tuple[int, ...]
    reason: str


async def plan_separation_join_merge(
    cmd: PlanJoinMergeCommand,
    *,
    document_repo: DocumentRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> SeparationPlan:
    """Validate a join/merge plan and record the audit trail."""
    await authorize(
        Action.DOCUMENT_READ,
        proxy(tenant_id=cmd.tenant_id, actor_id=cmd.actor_id, actor_roles=cmd.actor_roles),
        Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.source_document_ids[0]),
    )
    try:
        operation = SeparationOperation(cmd.operation)
    except ValueError:
        from app.domain.errors import ValidationFailed  # noqa: PLC0415

        raise ValidationFailed(
            f"unknown separation operation: {cmd.operation!r}",
            field="operation",
        ) from None
    resolved = [
        await resolve_document(
            tenant_id=cmd.tenant_id, document_id=doc_id, document_repo=document_repo
        )
        for doc_id in cmd.source_document_ids
    ]
    now = clock.now()
    plan = plan_join_merge(
        operation=operation,
        tenant_id=cmd.tenant_id,
        case_id=cmd.case_id,
        sources=tuple(
            SeparationSource(
                document_id=doc.id,
                tenant_id=doc.tenant_id,
                case_id=doc.case_id,
                version_number=doc.current_version,
                page_count=pages,
                locked=doc.locked,
            )
            for doc, pages in zip(resolved, cmd.page_counts, strict=True)
        ),
        actor_id=cmd.actor_id,
        reason=cmd.reason,
        now=now,
    )
    await append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=cmd.tenant_id,
        event_type=SEPARATION_PLANNED,
        actor_id=cmd.actor_id,
        subject_type=SUBJECT_TYPE_DOCUMENT,
        subject_id=cmd.source_document_ids[0],
        case_id=cmd.case_id,
        payload=plan.record.describe(),
        now=now,
    )
    return plan
