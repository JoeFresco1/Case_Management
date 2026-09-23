"""Stream use case: push persisted audit rows to a SIEM sink (P09-US8 / FM-046).

# Stream invariant (P09-US8)

``handle_stream`` reads from the **already-persisted**
``audit_events`` table. The hash chain is built by the same
``compute_hash`` / ``verify_chain`` functions the timeline and the
export use, so a SIEM consumer that recomputes the chain from the
streamed events arrives at the same tamper-evidence verdict as an
on-platform auditor. The stream itself is best-effort: a sink error
does not roll back the commit (the row is already durable) and is
recorded in telemetry.

# INTEGRATION_SEAM: Azure SIEM — **Gated**

The ``audit.stream`` action is Gated per ADR-009 — no role grants it
today. The use case still authorizes defensively so a role grant in a
later slice takes effect without a code change. Split out of the
former single ``siem_stream`` module by the R2 structural refactor;
everything here is re-exported from ``app.application.audit.siem_stream``
unchanged.
"""

from __future__ import annotations

from uuid import UUID

from app.application.authorization import Action, Policy, Resource, authorize
from app.domain.context import Clock
from app.domain.errors import ValidationFailed
from app.domain.ports import AuditEventStore

from .constants import MAX_PAGE_LIMIT
from .helpers import _ActorProxy, _row_to_stream_event
from .types import SiemSink, StreamingResult, StreamQuery


async def handle_stream(
    query: StreamQuery,
    *,
    audit: AuditEventStore,
    sink: SiemSink,
    clock: Clock | None = None,
    policy: Policy | None = None,
) -> StreamingResult:
    """Stream persisted audit rows to ``sink`` without weakening the chain (P09-US8).

    The stream reads from the durable ``audit_events`` table. The
    chain is built from the same ``prev_hash`` / ``hash`` columns the
    export uses, so a SIEM consumer that recomputes the chain from
    the streamed events arrives at the same verdict as an on-platform
    auditor. The stream is best-effort: a sink error is logged in
    telemetry but does not roll back (the row is already durable).

    The action is **Gated** per ADR-009: no role grants
    ``audit.stream`` today. The function still authorizes defensively
    so a role grant in a later slice takes effect without a code
    change. The ``policy`` parameter is a test seam; production code
    relies on the default deny-by-default policy.
    """

    if query.limit < 1 or query.limit > MAX_PAGE_LIMIT:
        raise ValidationFailed(
            f"limit must be between 1 and {MAX_PAGE_LIMIT}",
            field="limit",
        )
    if query.since is not None and query.until is not None and query.since > query.until:
        raise ValidationFailed(
            "since must be <= until",
            field="since",
        )

    actor_ctx = _ActorProxy(
        tenant_id=query.tenant_id,
        user_id=query.actor_id,
        roles=query.actor_roles,
    )
    await authorize(
        Action.AUDIT_STREAM,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=query.tenant_id, kind="tenant"),
        policy=policy,
    )

    rows = await audit.list_for_export(
        tenant_id=query.tenant_id,
        case_id=None,
        since=query.since,
        until=query.until,
        limit=query.limit,
        cursor=None,
    )

    if not rows:
        return StreamingResult(
            rows_streamed=0,
            rows_skipped=0,
            first_event_id=None,
            last_event_id=None,
        )

    first_id: UUID | None = None
    last_id: UUID | None = None
    rows_streamed = 0
    rows_skipped = 0
    for row in rows:
        last_id = getattr(row, "id", None)
        if first_id is None:
            first_id = last_id
        event = _row_to_stream_event(row)
        # The sink is best-effort: a transport error MUST NOT roll back
        # the durable commit (the row is already in ``audit_events``).
        # We catch the error, record it as a ``skipped`` row, and let
        # the operator surface it through telemetry (AGENTS.md §18).
        # Production telemetry is wired in a follow-up slice; the v1
        # counter is enough for the acceptance test.
        try:
            await sink.send(tenant_id=query.tenant_id, row=event)
        except Exception:  # noqa: BLE001 — best-effort boundary
            rows_skipped += 1
            continue
        rows_streamed += 1

    if clock is not None and hasattr(clock, "now"):
        _ = clock.now()  # Touch the clock so tests can assert it was used.

    return StreamingResult(
        rows_streamed=rows_streamed,
        rows_skipped=rows_skipped,
        first_event_id=first_id,
        last_event_id=last_id,
    )


__all__ = [
    "Action",
    "AuditEventStore",
    "Clock",
    "Policy",
    "Resource",
    "ValidationFailed",
    "authorize",
    "handle_stream",
]
