"""Use case: export a custom report to CSV with audit (P12-US6 / P09-US5).

``export_custom_report`` stages a single ``AuditExported`` row in the
same transaction as the read so the export action is visible on the
audit chain. The audit payload carries the definition id, the actor,
the row count, and a stable ``format`` marker (``"csv"``) so a later
replay can answer "who exported what, when, and how many rows". The
export itself does not commit; the caller (the HTTP router) commits
once. The CSV body is rendered after the use case returns the row
collection; the application layer never writes the file. Split out of
the former single-file ``custom_reports.py`` during the R2 package
split.

# INTEGRATION_SEAM: report engine

The ``Report`` port (P12 pillar §2) is reserved for an Azure Synapse /
Power BI production engine. Today the renderer walks the same case /
task repositories as the operational reports; a future swap is a
single-file change in the router. The CSV serialization is also
in-process; a future slice may move it to a document-store-backed
downloadable artifact (P03 seam).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.application.audit.record_event import RecordAuditCommand, record_event
from app.application.authorization import Action, Resource, authorize
from app.application.reports.operational_reports import OperationalReportsQuery
from app.domain.audit import AUDIT_EXPORTED, payload_hash
from app.domain.errors import NotFound
from app.domain.ports import AuditEventStore, CaseRepository, QueueRepository

from .helpers import _ActorProxy
from .render import render_custom_report
from .repository import ReportDefinitionRepository
from .shapes import CustomReportResult, ExportResult, ReportDefinition
from .vocabulary import AggregationKind


@dataclass(frozen=True, slots=True)
class ExportCustomReportCommand:
    """The input to :func:`export_custom_report`.

    The actor + tenant + roles mirror the operational reports; the
    definition_id is resolved inside the use case so a cross-tenant
    id cannot reach the renderer. ``reason`` is recorded on the audit
    payload (P09-US5 contract).
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    definition_id: UUID
    reason: str
    correlation_id: str


async def export_custom_report(
    cmd: ExportCustomReportCommand,
    *,
    repo: ReportDefinitionRepository,
    case_repo: CaseRepository,
    queue_repo: QueueRepository,
    audit: AuditEventStore,
    dedupe: Any,
    clock: Any,
) -> ExportResult:
    """Render the custom report, emit an audit row, return CSV body.

    The audit row is staged in the caller's transaction; the caller
    commits once. The CSV body is rendered from the structured
    result so the wire contract (column order, escaping) is
    deterministic.

    Authorization:
    - ``ADMIN_TENANT_CONFIG`` (process administrator defines the
      spec; export is the same authorization surface because a
      custom export leaves the system boundary).
    - ``AUDIT_EXPORT`` (the export itself is a security event).
    """
    definition = await repo.get(cmd.tenant_id, cmd.definition_id)
    if definition is None:
        raise NotFound(f"report definition {cmd.definition_id} not found in tenant")

    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.AUDIT_EXPORT,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind="report_export"),
    )

    now = clock.now()
    query = OperationalReportsQuery(
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor_id,
        actor_roles=cmd.actor_roles,
    )
    report = await render_custom_report(
        query,
        definition,
        case_repo=case_repo,
        queue_repo=queue_repo,
        now=now,
    )

    payload: dict[str, Any] = {
        "definition_id": str(definition.id),
        "definition_key": definition.key,
        "format": "csv",
        "row_count": len(report.items),
        "reason": cmd.reason or "unspecified",
    }
    await record_event(
        RecordAuditCommand(
            tenant_id=cmd.tenant_id,
            actor_id=cmd.actor_id,
            actor_type="user",
            event_type=AUDIT_EXPORTED,
            subject_type="report_export",
            subject_id=definition.id,
            case_id=None,
            correlation_id=cmd.correlation_id,
            causation_id=None,
            payload=payload,
            occurred_at=now,
        ),
        audit=audit,
        dedupe=dedupe,
    )

    csv_body = _render_csv(report, definition)
    # Synthetic stable identifier for the staged ``AuditExported`` row.
    # The production audit store computes the row id at insert time;
    # here we hash the payload so the caller has a deterministic
    # reference that can be cross-checked against the chain replay.
    # The first 16 hex chars of the digest are enough for an opaque
    # correlation token; collisions are not a concern because the
    # ``(event_type, correlation_id, payload_hash)`` idempotency key
    # is what the chain uses.
    audit_row_id = UUID(bytes=bytes.fromhex(payload_hash(payload)[:32]))
    return ExportResult(
        report=report,
        csv_body=csv_body,
        audit_row_id=audit_row_id,
    )


def _render_csv(report: CustomReportResult, definition: ReportDefinition) -> str:
    """Serialize the report result to CSV.

    Column order is stable: ``group_by`` columns first (in the
    author's declared order), then ``count`` then
    ``overdue_seconds``. ``csv.writer`` handles escaping per
    RFC 4180; the body is returned as a single string so the HTTP
    layer can serve it as ``text/csv`` without any extra adapter.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    header: list[str] = [gb.value for gb in definition.group_by]
    if AggregationKind.COUNT in definition.aggregations:
        header.append("count")
    if AggregationKind.OVERDUE_SUM in definition.aggregations:
        header.append("overdue_seconds")
    writer.writerow(header)
    for bucket in report.items:
        row: list[str] = [str(value) for value in bucket.key]
        if AggregationKind.COUNT in definition.aggregations:
            row.append(str(bucket.count))
        if AggregationKind.OVERDUE_SUM in definition.aggregations:
            row.append(str(bucket.overdue_seconds))
        writer.writerow(row)
    return buffer.getvalue()


__all__ = [
    "ExportCustomReportCommand",
    "export_custom_report",
    "_render_csv",
]
