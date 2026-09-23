"""Pydantic request/response schemas for the queue admin + worker API.

These are wire projections of the domain ``Queue`` and ``Task``
aggregates (see ``app/domain/queue.py`` and
``app/domain/task.py``). The HTTP layer does not invent fields; the
schema mirrors the canonical aggregate so the OpenAPI types match
the long-lived declarative contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class QueueSummary(BaseModel):
    """A queue as returned to admins / workers."""

    id: UUID
    tenant_id: UUID
    key: str
    name: str
    description: str
    assignment_policy: Literal["manual", "round_robin"]
    capacity: int
    member_user_ids: list[UUID]
    rr_cursor: int
    created_at: datetime
    updated_at: datetime


class DefineQueueRequest(BaseModel):
    """Body for ``POST /api/v1/admin/queues``."""

    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    assignment_policy: Literal["manual", "round_robin"] = "manual"
    capacity: int = Field(default=0, ge=0)
    member_user_ids: list[UUID] = Field(default_factory=list)


class ReconfigureQueueRequest(BaseModel):
    """Body for ``PUT /api/v1/admin/queues/{queue_id}``.

    Every field is optional; ``None`` means "leave unchanged". An
    empty list for ``member_user_ids`` means "clear the membership
    list" (only legal under ``manual`` — the aggregate rejects it
    under ``round_robin``).
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    assignment_policy: Literal["manual", "round_robin"] | None = None
    capacity: int | None = Field(default=None, ge=0)
    member_user_ids: list[UUID] | None = None


class ClaimNextRequest(BaseModel):
    """Body for ``POST /api/v1/queues/{key}/claim``.

    The actor is taken from the auth context; the body is empty by
    design. The model exists so future knobs (``max_due_at``,
    ``type_filter``) land on the same shape without a contract
    break.
    """

    pass


class ClaimNextResponse(BaseModel):
    """Response for a successful claim.

    ``task_id`` is the canonical handle; the full task view is on
    ``GET /api/v1/tasks/{id}``. The response deliberately omits
    the task body so the claim endpoint stays small and the worker
    UI fetches the detail on click.
    """

    task_id: UUID
    queue_key: str
    claimed_at: datetime


class ReleaseTaskRequest(BaseModel):
    """Body for ``POST /api/v1/tasks/{id}/release``."""

    reason: str | None = Field(default=None, max_length=500)
    queue_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description=(
            "Return queue. Required when the task was claimed through "
            "``POST /queues/{key}/claim`` (a claim clears the queue "
            "pointer); ignored when the task still carries one."
        ),
    )


class BulkHandleRequest(BaseModel):
    """Body for ``POST /api/v1/queues/{key}/bulk-handle``."""

    action: Literal["claim", "release"]
    task_ids: list[UUID] = Field(min_length=1, max_length=200)


class BulkHandleResponse(BaseModel):
    handled: list[UUID]
    skipped: list[UUID]


class DelegateRequest(BaseModel):
    """Body for ``POST /api/v1/tasks/delegate`` (P02-US9 / FM-016).

    ``until`` is the exclusive bound on the delegation window: the
    delegate holds each task while ``now <= until``; once ``now >
    until`` the ``expire_delegations`` sweep restores the prior owner.
    A past ``until`` is rejected with 409 so a worker cannot hand off
    work they no longer own.
    """

    delegate_user_id: UUID
    task_ids: list[UUID] = Field(min_length=1, max_length=200)
    until: datetime


class ReleaseTaskResponse(BaseModel):
    """Response for ``POST /api/v1/tasks/{id}/release``."""

    task_id: UUID
    status: str = "released"


class DelegateResponse(BaseModel):
    """Response for ``POST /api/v1/tasks/delegate`` (P02-US9 / FM-016).

    Echoes the bounded window (``until``) so the worker UI can render
    the cover period without re-reading the audit row.
    """

    delegated: list[UUID]
    delegate_user_id: UUID
    until: datetime


class QueueTaskAging(BaseModel):
    """Worker-facing due-date / aging readout for one queue task."""

    age_seconds: float
    is_overdue: bool
    overdue_by_seconds: float | None = None
    due_in_seconds: float | None = None


class QueueTaskView(BaseModel):
    """One open task on a queue with its aging context (P02-US3 / FM-016)."""

    task_id: UUID
    case_id: UUID
    type: str
    title: str
    status: str
    due_at: datetime | None = None
    created_at: datetime | None = None
    aging: QueueTaskAging


__all__ = [
    "BulkHandleRequest",
    "BulkHandleResponse",
    "ClaimNextRequest",
    "ClaimNextResponse",
    "DefineQueueRequest",
    "DelegateRequest",
    "DelegateResponse",
    "QueueSummary",
    "QueueTaskAging",
    "QueueTaskView",
    "ReconfigureQueueRequest",
    "ReleaseTaskRequest",
    "ReleaseTaskResponse",
]
