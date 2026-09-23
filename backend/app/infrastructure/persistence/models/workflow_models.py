"""Workflow and work ORM models.

SLA clocks, queues, workflow catalog/runtime/transitions, case decisions, and
tasks. Split verbatim from the pre-split ``persistence/models.py``
(AGENTS.md §4.R2).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

from .base import TimestampMixin

# ---------------------------------------------------------------------------
# SLA clocks (P02-US6 / FM-018 / FM-039 H0 seam)
#
# Append-friendly table of every deadline the workflow engine started.
# The scanner (app.application.sla.scan_sla) reads ``list_due`` every
# minute, flips each running clock that has passed ``due_at`` to
# ``breached`` (idempotent at the domain layer), and writes an
# ``SlaBreached`` audit row in the same transaction. The case's
# legal-hold flag (FM-039) is *not* stored here — it lives on the
# ``cases`` row; the scanner skips case-owned clocks only when the
# use case asks it to (H1 will wire the seam).
# ---------------------------------------------------------------------------


class SlaClock(Base):
    """A measurable deadline bound to a case (P02-US6 / FM-018).

    The row shape mirrors :class:`app.domain.sla.SlaClock` and
    nothing more. ``breach_action`` is the free-form dict the
    workflow author configured (typically
    ``{"type": "notify", "queue": "<queue_key>"}``); the
    application layer interprets it when the scanner fires. The
    column is JSON, not a stringified blob, so the SQL adapter
    can read it without a parse step.
    """

    __tablename__ = "sla_clocks"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[UUID] = mapped_column(
        SAUuid(),
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
    )
    clock_key: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    breach_action: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_sla_clocks_tenant_case", "tenant_id", "case_id"),
        Index("ix_sla_clocks_tenant_due", "tenant_id", "due_at"),
        Index("ix_sla_clocks_tenant_status_due", "tenant_id", "status", "due_at"),
    )


# ---------------------------------------------------------------------------
# Queues (P02-US3, US9 / FM-016).
#
# A queue is a tenant-scoped named work container with an assignment
# policy (``manual`` or ``round_robin``). The workflow engine references
# queues by ``key`` (stable identifier); the human-facing ``name`` is a
# label and may change without breaking history.
#
# Why one table per queue (not one row per queue+assignment): queue
# *configuration* (policy, capacity, members) is configuration data —
# P10 owns the publish/retire lifecycle for it. Task assignment to a
# queue is a fact on the ``tasks`` row (``assignee_queue_id``) and
# changes per-task; it does not live here. The round-robin pointer
# (``rr_cursor``) is per-queue and updates on each auto-claim, so it
# stays on the queue row rather than being inferred from a scan.
# ---------------------------------------------------------------------------


class Queue(Base, TimestampMixin):
    """A named work container with an assignment policy (P02-US3 / FM-016).

    ``key`` is the stable identifier the workflow engine references
    (``actions[].queue``). ``name`` is the human label and may rename
    without breaking audit history. ``assignment_policy`` is one of
    ``"manual"`` (a worker must explicitly claim) or ``"round_robin"``
    (auto-assigned in declared-member order).

    ``rr_cursor`` is the round-robin index into ``member_user_ids``;
    it advances after every auto-assignment so successive
    round-robin claims cycle through members deterministically. The
    cursor is meaningless under ``manual`` and is left at ``0``.

    ``capacity`` is the soft cap on open tasks the queue will accept
    (a workflow transition that targets a saturated queue is rejected
    by the engine before it lands — out of scope for the first slice,
    but the column lives here so the validator does not need a
    migration when v2 wires it).

    ``member_user_ids`` is the ordered set of users that may work
    the queue. Under ``manual`` it is informational; under
    ``round_robin`` it is the cycling order. Stored as JSON (not a
    child table) so the round-robin order is read in one query and
    the column is portable (AGENTS.md §4.R1 — no JSONB / arrays).
    """

    __tablename__ = "queues"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    assignment_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    member_user_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    rr_cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # FM-016: optimistic-concurrency token. Bumped by the domain aggregate
    # on every mutation (rename / policy / members / cursor advance); the
    # repository enforces compare-and-swap so concurrent edits conflict.
    version_token: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_queues_tenant_key"),
        Index("ix_queues_tenant", "tenant_id"),
    )


class WorkflowDefinition(Base, TimestampMixin):
    """Tenant-scoped workflow catalog header."""

    __tablename__ = "workflow_definitions"
    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    current_version_id: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_workflow_definitions_tenant_key"),
        Index("ix_workflow_definitions_tenant_id", "tenant_id"),
    )


class WorkflowVersion(Base):
    """Immutable versioned workflow definition."""

    __tablename__ = "workflow_versions"
    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    workflow_definition_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("workflow_definitions.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "workflow_definition_id",
            "version_number",
            name="uq_workflow_versions_definition_version",
        ),
        Index("ix_workflow_versions_tenant_id", "tenant_id"),
        Index("ix_workflow_versions_definition_status", "workflow_definition_id", "status"),
    )


class WorkflowInstance(Base):
    """Runtime workflow pointer pinned to a published version."""

    __tablename__ = "workflow_instances"
    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False
    )
    workflow_definition_id: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    workflow_version_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("workflow_versions.id", ondelete="RESTRICT"), nullable=False
    )
    current_state: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version_token: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("case_id", name="uq_workflow_instances_case_id"),
        Index("ix_workflow_instances_tenant_id", "tenant_id"),
        Index("ix_workflow_instances_workflow_version_id", "workflow_version_id"),
    )


class WorkflowTransition(Base):
    """One executed transition (T069 runtime log row).

    Append-only evidence that a declared edge fired for an instance.
    No UPDATE/DELETE surface exists; corrections are compensating
    audit rows. Mirrors ``domain.workflow.WorkflowTransitionRecord``.
    """

    __tablename__ = "workflow_transitions"
    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    workflow_instance_id: Mapped[UUID] = mapped_column(
        SAUuid(),
        ForeignKey("workflow_instances.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_state: Mapped[str] = mapped_column(String(64), nullable=False)
    to_state: Mapped[str] = mapped_column(String(64), nullable=False)
    transition_key: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    causation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (
        Index("ix_workflow_transitions_tenant_id", "tenant_id"),
        Index(
            "ix_workflow_transitions_instance_occurred",
            "workflow_instance_id",
            "occurred_at",
        ),
    )


class Decision(Base, TimestampMixin):
    """A case-scoped decision record (T069 schema; lifecycle is Phase 4).

    The table exists from migration 0005 so the Phase 4 decision
    lifecycle (record/supersede/retire) lands without DDL. Rows are
    append-only: revisions chain via ``supersedes_id``; there is no
    UPDATE/DELETE path. No application writer exists in this slice.
    """

    __tablename__ = "decisions"
    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False
    )
    decision_type: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    made_by: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    made_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applicable_workflow_version_id: Mapped[UUID | None] = mapped_column(
        SAUuid(),
        ForeignKey("workflow_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    supersedes_id: Mapped[UUID | None] = mapped_column(
        SAUuid(),
        ForeignKey("decisions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    __table_args__ = (
        Index("ix_decisions_tenant_id", "tenant_id"),
        Index("ix_decisions_case_id", "case_id"),
        Index("ix_decisions_supersedes_id", "supersedes_id"),
    )


class Task(Base, TimestampMixin):
    """A tenant-scoped unit of case work persisted for task APIs."""

    __tablename__ = "tasks"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    assignee_user_id: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)
    # Queue keys are stable business identifiers, not queue row ids.
    assignee_queue_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)
    completion_outcome: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    version_token: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by_transition: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_tasks_tenant_idempotency_key"),
        Index("ix_tasks_tenant", "tenant_id"),
        Index("ix_tasks_case", "case_id"),
        Index("ix_tasks_tenant_status", "tenant_id", "status"),
        Index("ix_tasks_tenant_assignee", "tenant_id", "assignee_user_id"),
    )
