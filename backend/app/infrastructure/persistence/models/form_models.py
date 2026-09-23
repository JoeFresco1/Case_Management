"""Form ORM models: form versions, case-type bindings, worker sessions
(P07 / FM-036 / FM-038). Split verbatim from the pre-split
``persistence/models.py`` (AGENTS.md §4.R2).
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
    UniqueConstraint,
)
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

from .base import TimestampMixin

# ---------------------------------------------------------------------------
# Forms (P07 / FM-036 / P07-US1..US4)
#
# FormDefinition is stored as JSON on FormVersion.definition_json.
# FormBinding links a FormVersion to a CaseTypeVersion.
# Lifecycle mirrors CaseTypeVersion: draft -> published -> retired.
# Published forms/bindings are immutable.
# ---------------------------------------------------------------------------


class FormVersion(Base):
    """An immutable, published snapshot of a form (P07-US2 / FM-036).

    The definition is stored as JSON (sections, fields, visibility rules,
    form-level validations). Status mirrors CaseTypeVersionStatus:
    draft -> published -> retired. Once published, the row is immutable.
    """

    __tablename__ = "form_versions"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "key", "version_number", name="uq_form_versions_tenant_key_version"
        ),
        Index("ix_form_versions_tenant_status", "tenant_id", "status"),
        Index("ix_form_versions_tenant_key", "tenant_id", "key"),
    )


class FormBinding(Base):
    """Immutable link between a FormVersion and a CaseTypeVersion (P07-US4 / FM-036).

    A process administrator publishes a binding each time the bound
    case-type version changes. Published bindings are immutable; the
    lifecycle state is implicit (published_at is None = draft, non-null =
    published).
    """

    __tablename__ = "form_bindings"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    case_type_version_id: Mapped[UUID] = mapped_column(
        SAUuid(),
        ForeignKey("case_type_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    form_version_id: Mapped[UUID] = mapped_column(
        SAUuid(),
        ForeignKey("form_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state_forms: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_by: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "case_type_version_id",
            "form_version_id",
            name="uq_form_bindings_tenant_ctv_fv",
        ),
        Index("ix_form_bindings_tenant_ctv", "tenant_id", "case_type_version_id"),
        Index("ix_form_bindings_tenant_fv", "tenant_id", "form_version_id"),
        Index("ix_form_bindings_tenant_published", "tenant_id", "published_at"),
    )


class FormSession(Base, TimestampMixin):
    """A worker's durable progress through a multi-step form (P07-US6 / FM-038).

    One row per ``(case, form version, actor)`` attempt. ``status`` is
    ``draft`` → ``in_progress`` → ``submitted`` (terminal);
    ``submitted`` rows are immutable (the use case never updates them;
    reopening is a new row). ``completed_steps`` is the ordered set of
    finished step keys (JSON list); ``step_data`` is the accumulated
    partial payload (JSON object). ``version_token`` drives
    compare-and-swap on ``update`` so concurrent step saves conflict
    instead of losing updates (Constitution V).
    """

    __tablename__ = "form_sessions"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[UUID] = mapped_column(
        SAUuid(),
        ForeignKey("cases.id", ondelete="RESTRICT"),
        nullable=False,
    )
    form_version_id: Mapped[UUID] = mapped_column(
        SAUuid(),
        ForeignKey("form_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_id: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    current_step_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_steps: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    step_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="in_progress")
    version_token: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_form_sessions_tenant_case", "tenant_id", "case_id"),
        Index(
            "ix_form_sessions_open_lookup",
            "tenant_id",
            "case_id",
            "form_version_id",
            "actor_id",
            "status",
        ),
    )
