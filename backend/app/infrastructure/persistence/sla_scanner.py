"""SQL composition adapter for the application SLA scanner."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.sla.scan_sla import scan_once, scan_warning_once
from app.domain.ports import LegalHoldRepository
from app.domain.task import Task
from app.infrastructure.persistence.audit_outbox import SqlAuditEventStore, SqlOutboxWriter
from app.infrastructure.persistence.models import Tenant
from app.infrastructure.persistence.repositories import SqlSlaClockRepository, SqlTaskRepository


class _SystemClock:
    """Production clock supplied at the infrastructure boundary."""

    def now(self) -> datetime:
        """Return the current UTC timestamp."""
        return datetime.now(tz=UTC)


class SqlSlaScanner:
    """Run both SLA tiers in one tenant-scoped SQL transaction.

    The local legal-hold adapter is injected at composition time.  A future
    SQL/Azure adapter can implement the same ``is_under_legal_hold`` seam
    without changing the scanner or scheduler.
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        legal_holds: LegalHoldRepository,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._legal_holds = legal_holds

    async def tenant_ids(self) -> list[UUID]:
        """Return all tenant ids using a portable, read-only query."""
        async with self._sessionmaker() as session:
            result = await session.execute(select(Tenant.id).order_by(Tenant.id.asc()))
            return list(result.scalars().all())

    async def scan_tenant(self, tenant_id: UUID) -> None:
        """Scan one tenant and commit both SLA transitions atomically."""
        async with self._sessionmaker() as session:
            try:
                clock = _SystemClock()
                now = clock.now()
                clock_repo = SqlSlaClockRepository(session)
                task_repo = SqlTaskRepository(session)
                audit = SqlAuditEventStore(session)
                outbox = SqlOutboxWriter(session)

                async def create_task(
                    *,
                    tenant_id: UUID,
                    case_id: UUID,
                    task_type: str,
                    title: str,
                    priority: str,
                    queue_key: str | None,
                ) -> Task:
                    """Construct an escalation task at the transaction boundary."""
                    return Task.create(
                        tenant_id=tenant_id,
                        case_id=case_id,
                        type=task_type,
                        title=title,
                        priority=priority,
                        assignee_queue_id=queue_key,
                        created_at=now,
                        created_by_transition="sla_scanner",
                    )

                await scan_warning_once(
                    tenant_id=tenant_id,
                    clock_repo=clock_repo,
                    audit=audit,
                    outbox=outbox,
                    case_lookup=self._legal_holds,
                    task_creator=create_task,
                    task_repo=task_repo,
                    clock=clock,
                )
                await scan_once(
                    tenant_id=tenant_id,
                    clock_repo=clock_repo,
                    audit=audit,
                    outbox=outbox,
                    case_lookup=self._legal_holds,
                    task_creator=create_task,
                    task_repo=task_repo,
                    clock=clock,
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise


__all__ = ["SqlSlaScanner"]
