"""Pydantic request/response schemas for the cases API (P01-US2 / FM-006 / FR-002).

The ``CaseSummary`` schema is the canonical read shape used by
``GET /api/v1/cases/{id}`` and ``GET /api/v1/cases``. Fields the
client must always see are typed; workspace slots that land in later
pillars are typed as a union of ``Redacted`` shape and the eventual
list shape so the OpenAPI document is honest about what the server
returns today vs later.

``Page[T]`` is the AGENTS.md §17.1 envelope for paginated list
endpoints. Cursor-based: ``next_cursor`` is an opaque string the
server encodes from the current page's tail; clients send it back
unchanged on the next request.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

Priority = Literal["low", "normal", "high", "urgent"]


class CreateCaseRequest(BaseModel):
    case_type_key: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=10_000)
    data: dict[str, Any] = Field(default_factory=dict)
    priority: Priority = "normal"


# ---------------------------------------------------------------------------
# Edit case (P01-US3 / FM-007 / FR-003)
# ---------------------------------------------------------------------------


class EditCaseRequest(BaseModel):
    """PATCH body for ``/api/v1/cases/{id}`` (P01-US3).

    Every field is optional — only the fields the caller intends to
    change are sent. The router pairs this with the required
    ``If-Match: <version_token>`` header (FR-003 acceptance scenario 3).
    A stale ``If-Match`` returns 409 with the current ``version_token``
    in the body so the client can refetch and retry.
    """

    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=10_000)
    priority: Priority | None = None
    data: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Redaction marker (AGENTS.md §4.R12, P01-US4, FR-002)
# ---------------------------------------------------------------------------


class RedactedField(BaseModel):
    """A field whose content the actor is not authorized to see.

    The schema is deliberately minimal: ``redacted: true`` plus a
    human-readable ``reason``. The client renders ``(restricted)`` or
    similar and never sees the underlying content. This is the only
    shape a redacted field may take on the wire.
    """

    redacted: Literal[True]
    reason: str


# ---------------------------------------------------------------------------
# Case summary (P01-US2 / FR-002)
# ---------------------------------------------------------------------------


class CaseSummary(BaseModel):
    """The canonical case-workspace read shape.

    Workspace slots that land in later pillars (assignee, queue,
    participants, open tasks, recent documents, recent decisions,
    timeline, SLA state) are typed as either a list (``[]`` for
    ``participants``/``open_tasks``/etc.) or a ``RedactedField`` so the
    OpenAPI document reflects what the server actually returns today.

    The ``sensitivity`` field carries the case's effective sensitivity
    level (inherited from the case type's ``sensitivity_default`` at
    creation time). The client uses it to render a case-level
    indicator; it never gates access — server-side redaction is the
    authoritative mechanism.
    """

    id: UUID
    tenant_id: UUID
    reference: str
    case_type_key: str
    case_type_version_id: UUID
    status: str
    priority: str
    title: str
    description: str
    data: dict[str, Any]
    current_workflow_state: str | None
    version_token: int
    created_at: str
    updated_at: str
    created_by: UUID
    closed_at: str | None
    sensitivity: str = "internal"

    assignee: RedactedField | None = None
    queue: RedactedField | None = None
    # P02-US6 / FM-018: SLA state projection. The server emits either
    # a populated ``SlaStateView`` (workspace view), a ``RedactedField``
    # marker when the projection is deferred or restricted, or ``None``
    # for the list view (the list-view serializer strips this slot).
    # The field is typed as a union so the OpenAPI document advertises
    # all shapes honestly.
    sla_state: SlaStateView | RedactedField | None = None
    participants: list[Any] = Field(default_factory=list)
    open_tasks: list[Any] = Field(default_factory=list)
    recent_documents: list[Any] = Field(default_factory=list)
    recent_decisions: list[Any] = Field(default_factory=list)
    timeline: list[Any] = Field(default_factory=list)
    # P08-US1 / FM-039: the case's legal-hold flag surfaced from the
    # aggregate. Read-only; place/release via /cases/{id}/holds.
    legal_hold: bool = False


class SlaStateView(BaseModel):
    """The wire shape of the case-summary ``sla_state`` slot.

    Mirrors :class:`app.application.cases.sla_state.SlaStateSummary`.
    ``state`` is one of ``"breached"`` / ``"warning"`` / ``"active"``
    / ``"paused"`` / ``"completed"`` / ``"none"``. ``next_due_at`` is
    the soonest ``due_at`` across the case's clocks that drove the
    worst tier (``breached`` clocks show the past deadline so the
    operator sees when the case went overdue). The counts break
    down the case's clock set so the workspace UI can render
    "1 breached, 2 active" without re-fetching.
    """

    state: Literal["breached", "warning", "active", "paused", "completed", "none"]
    next_due_at: str | None = None
    breached_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    active_count: int = Field(ge=0)
    paused_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    clock_keys: list[str] = Field(default_factory=list)


class CaseListItem(BaseModel):
    """The list-view row shape (subset of ``CaseSummary``).

    The list endpoint intentionally omits workspace-only slots. The
    client fetches the full ``CaseSummary`` by id when the operator
    drills into a case.
    """

    id: UUID
    tenant_id: UUID
    reference: str
    case_type_key: str
    status: str
    priority: str
    title: str
    current_workflow_state: str | None
    version_token: int
    created_at: str
    updated_at: str
    created_by: UUID
    closed_at: str | None


# ---------------------------------------------------------------------------
# Pagination envelope (AGENTS.md §17.1)
# ---------------------------------------------------------------------------


class Page[T](BaseModel):
    """Cursor-paginated list envelope.

    ``limit`` echoes the requested limit (after server-side clamping).
    ``next_cursor`` is ``None`` when the page is the last page.
    """

    items: list[T]
    next_cursor: str | None
    limit: int = Field(default=50, ge=1, le=200)


# ---------------------------------------------------------------------------
# Workflow transition (P02-US1 / FM-014 / FR-009 / T076)
# ---------------------------------------------------------------------------


class ExecuteTransitionRequest(BaseModel):
    """POST body for ``/api/v1/cases/{id}/transitions`` (FR-009).

    ``transition_key`` is the stable key declared on the workflow
    transition (``"submit_for_review"``, ``"record_decision"``).
    ``payload`` is the caller's structured transition payload — its
    keys are surfaced as ``payload.*`` to guard expressions and
    recorded on the audit row.
    """

    transition_key: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] | None = None


class TaskSummary(BaseModel):
    """Lightweight task summary embedded in transition responses.

    The full task view (``TaskView``) carries completion timestamps
    and audit hooks; this shape only exposes the fields a client
    needs to render a follow-on-task list (P02-US1 acceptance
    scenario 1).
    """

    id: UUID
    type: str
    title: str
    assignee_queue_id: str | None = None
    assignee_user_id: UUID | None = None


class ExecuteTransitionResponse(BaseModel):
    """POST response for ``/api/v1/cases/{id}/transitions``.

    Reports the post-transition state (``from_state`` → ``to_state``)
    and the list of follow-on tasks created by the transition's
    ``create_task`` actions.
    """

    case_id: UUID
    from_state: str
    to_state: str
    transition_key: str
    created_tasks: list[TaskSummary] = Field(default_factory=list)


class TransitionPreflight(BaseModel):
    """Safe availability and guard verdict for one current transition."""

    key: str
    from_state: str
    to_state: str
    allowed: bool
    reason: str | None = None
    details: dict[str, Any] | None = None
    approval_required: bool = False
    approval_eligible: bool = True
    approval_reason: str | None = None


class TransitionPreflightResponse(BaseModel):
    """Current workflow state and all executable transition options."""

    case_id: UUID
    workflow_instance_id: UUID
    workflow_version_id: UUID
    current_state: str
    transitions: list[TransitionPreflight] = Field(default_factory=list)
