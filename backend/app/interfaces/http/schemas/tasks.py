"""Pydantic request/response schemas for the tasks API (P02-US1 / FM-014).

``TaskView`` is the canonical read shape for ``GET /api/v1/tasks`` and
``POST /api/v1/tasks/{id}/complete``. ``CompleteTaskRequest`` is the
body for the completion endpoint; it carries the optional
``trigger_transition_key`` that wires a task completion to a workflow
transition in the same transaction (FR-011 / P02-US1 acceptance
scenario 1).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class CompleteTaskRequest(BaseModel):
    """POST body for ``/api/v1/tasks/{id}/complete`` (FR-011).

    ``outcome`` is the structured closure detail (``{"decision":
    "approved", "notes": "..."}``). It is recorded on the audit row
    and on the aggregate, but never embedded in the outbox payload
    (outbox payloads are id-references by policy).

    ``trigger_transition_key`` is the workflow transition the engine
    should fire in the same transaction when the task completes. The
    transition's guard is evaluated against the case context; if the
    guard fails the entire transaction rolls back so the task is not
    committed ``completed`` without its paired transition (Constitution
    III / P02-US1 acceptance scenario 1).
    """

    outcome: dict[str, Any] | None = None
    trigger_transition_key: str | None = Field(default=None, max_length=128)


class TaskView(BaseModel):
    """Canonical task view (read shape for ``GET /api/v1/tasks``).

    Mirrors the ``Task`` aggregate (P02-US1 / FM-014 / FR-011). The
    ``status`` field is the lifecycle value (``open`` / ``completed`` /
    ``cancelled``); ``version_token`` is the optimistic-concurrency
    token (Constitution V — no silent overwrites).
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    type: str
    title: str
    status: str
    priority: str
    assignee_user_id: UUID | None = None
    assignee_queue_id: str | None = None
    due_at: str | None = None
    completed_at: str | None = None
    completed_by: UUID | None = None
    outcome: dict[str, Any] | None = None
    version_token: int = 1


__all__ = ["CompleteTaskRequest", "TaskView"]
