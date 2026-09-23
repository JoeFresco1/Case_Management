"""Dependency contracts for the case HTTP composition boundary.

The HTTP router needs several ports bound to one request transaction. This
module describes that bundle without importing FastAPI, SQLAlchemy, or any
infrastructure adapter. The composition root supplies the concrete provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    CaseTypeRepository,
    CaseTypeVersionRepository,
    DocumentRepository,
    OutboxWriter,
    SlaClockRepository,
    TaskRepository,
    WorkflowDefinitionRepository,
    WorkflowInstanceRepository,
    WorkflowTransitionLog,
    WorkflowVersionRepository,
)


@dataclass(frozen=True, slots=True)
class CaseHttpRepositories:
    """Ports required by the case HTTP endpoints for one session."""

    case: CaseRepository
    case_type: CaseTypeRepository
    case_type_version: CaseTypeVersionRepository
    sla_clock: SlaClockRepository
    audit: AuditEventStore
    outbox: OutboxWriter
    workflow_definition: WorkflowDefinitionRepository
    workflow_instance: WorkflowInstanceRepository
    workflow_transition_log: WorkflowTransitionLog
    workflow_version: WorkflowVersionRepository
    task: TaskRepository
    document: DocumentRepository


class CaseHttpDependencyProvider(Protocol):
    """Create a request-scoped case port bundle from a persistence session."""

    def for_session(self, session: object) -> CaseHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = ["CaseHttpDependencyProvider", "CaseHttpRepositories"]
