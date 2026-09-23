"""SQL composition provider for the case HTTP boundary.

This adapter is deliberately outside ``app.interfaces``. It binds the local
SQLAlchemy repositories and transaction-bound stores to the infra-free case
HTTP provider contract; an Azure adapter can implement the same contract later.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cases.http_dependencies import CaseHttpRepositories
from app.infrastructure.persistence.audit_outbox import (
    SqlAuditEventStore,
    SqlOutboxWriter,
)
from app.infrastructure.persistence.repositories import (
    SqlCaseRepository,
    SqlCaseTypeRepository,
    SqlCaseTypeVersionRepository,
    SqlDocumentRepository,
    SqlSlaClockRepository,
    SqlTaskRepository,
    SqlWorkflowDefinitionRepository,
    SqlWorkflowVersionRepository,
)
from app.infrastructure.persistence.repositories.workflow_instance_repository import (
    SqlWorkflowInstanceRepository,
    SqlWorkflowTransitionLog,
)


class SqlCaseHttpDependencyProvider:
    """Bind all case HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> CaseHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return CaseHttpRepositories(
            case=SqlCaseRepository(db),
            case_type=SqlCaseTypeRepository(db),
            case_type_version=SqlCaseTypeVersionRepository(db),
            sla_clock=SqlSlaClockRepository(db),
            audit=SqlAuditEventStore(db),
            outbox=SqlOutboxWriter(db),
            workflow_definition=SqlWorkflowDefinitionRepository(db),
            workflow_transition_log=SqlWorkflowTransitionLog(db),
            workflow_instance=SqlWorkflowInstanceRepository(db),
            workflow_version=SqlWorkflowVersionRepository(db),
            task=SqlTaskRepository(db),
            document=SqlDocumentRepository(db),
        )


__all__ = ["SqlCaseHttpDependencyProvider"]
