"""SQLAlchemy ORM models.

All column types are SQLAlchemy generic types (Uuid, JSON, DateTime(timezone=True))
to keep Azure SQL portability. Every table carries `tenant_id`; explicit
tenant scoping lives in the repository methods, not in magic event listeners.

This package is the structural split of the original single-file module
(1906 lines > 1000-line hard cap, AGENTS.md §4.R2). Every public name the
pre-split module exposed is re-exported here — classes, ``Base``,
``TimestampMixin``, and the imported helpers — so every consumer of
``app.infrastructure.persistence.models`` and the alembic ``env.py`` target
keep working unchanged, and ``Base.metadata`` registers all tables exactly
once.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

# ---------------------------------------------------------------------------
# Viewer pipeline tables (T5 / FM-023 ext) live in ``models_viewer``.
#
# ``models.py`` was already past the 1000-line hard cap (AGENTS.md
# §4.R2), so the three viewer tables ship in the sibling module. The
# import here registers them on ``Base.metadata`` so the alembic
# ``env.py`` target (which imports this module) and the
# ``create_all``-based test fixtures see the full schema.
# ---------------------------------------------------------------------------
from app.infrastructure.persistence.models_viewer import (
    DocumentCheckoutLock,
    DocumentRendition,
    ViewerAnnotation,
)

from .base import Base, TimestampMixin
from .case_models import (
    Case,
    CaseType,
    CaseTypeVersion,
    LegalHold,
    Participant,
    ReferenceFormat,
    Tenant,
    User,
)
from .document_models import (
    Document,
    DocumentRedaction,
    DocumentVersion,
)
from .form_models import (
    FormBinding,
    FormSession,
    FormVersion,
)
from .ingestion_models import (
    CapturedEmail,
    CapturedWebhook,
    Channel,
    ImportJob,
    ImportMapping,
    QuarantineRecord,
)
from .ops_models import (
    AuditEvent,
    ConfigurationPublication,
    IdempotencyKey,
    Outbox,
    SignedAuditBatch,
)
from .retention_models import (
    DispositionRequest,
    RetentionPolicy,
)
from .search_models import (
    SavedSearch,
    SearchAudit,
)
from .security_models import (
    BreakGlassGrant,
    Role,
    RoleAssignment,
)
from .webhook_models import (
    OutboundDelivery,
    WebhookSubscription,
)
from .workflow_models import (
    Decision,
    Queue,
    SlaClock,
    Task,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowTransition,
    WorkflowVersion,
)

__all__ = [
    "Any",
    "AuditEvent",
    "Base",
    "Boolean",
    "BreakGlassGrant",
    "CapturedEmail",
    "CapturedWebhook",
    "Case",
    "CaseType",
    "CaseTypeVersion",
    "Channel",
    "ConfigurationPublication",
    "DateTime",
    "Decision",
    "DispositionRequest",
    "Document",
    "DocumentCheckoutLock",
    "DocumentRedaction",
    "DocumentRendition",
    "DocumentVersion",
    "ForeignKey",
    "FormBinding",
    "FormSession",
    "FormVersion",
    "IdempotencyKey",
    "ImportJob",
    "ImportMapping",
    "Index",
    "Integer",
    "JSON",
    "LegalHold",
    "Mapped",
    "OutboundDelivery",
    "Outbox",
    "Participant",
    "PrimaryKeyConstraint",
    "QuarantineRecord",
    "Queue",
    "ReferenceFormat",
    "RetentionPolicy",
    "Role",
    "RoleAssignment",
    "SAUuid",
    "SavedSearch",
    "SearchAudit",
    "SignedAuditBatch",
    "SlaClock",
    "String",
    "Task",
    "Tenant",
    "Text",
    "TimestampMixin",
    "UUID",
    "UniqueConstraint",
    "User",
    "ViewerAnnotation",
    "WebhookSubscription",
    "WorkflowDefinition",
    "WorkflowInstance",
    "WorkflowTransition",
    "WorkflowVersion",
    "annotations",
    "datetime",
    "mapped_column",
    "relationship",
    "uuid4",
]

# why: loading the submodules above sets them as attributes on this package
# (a side effect of the import system), but the pre-split single-file module
# exposed no submodule attributes. Pop them so ``dir(models)`` stays
# byte-for-byte equivalent for introspection; the modules remain reachable
# via their fully-qualified names (they stay in ``sys.modules``), and no
# consumer ever imported a submodule path (verified by AST scan).
for _submodule in (
    "base",
    "case_models",
    "document_models",
    "form_models",
    "ingestion_models",
    "ops_models",
    "retention_models",
    "search_models",
    "security_models",
    "webhook_models",
    "workflow_models",
):
    globals().pop(_submodule, None)
del _submodule
