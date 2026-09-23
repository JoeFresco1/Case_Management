"""Convert between ORM rows and domain entities.

Kept tiny and explicit. No business logic. Field renames and additions
between the domain (``CaseType`` / ``CaseTypeSchema``) and the ORM rows
live here so the domain stays free of SQLAlchemy imports.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.domain.case import Case, CaseReference, CaseStatus
from app.domain.case_type import (
    CaseType,
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.legal_hold import LegalHold
from app.domain.participant import Participant
from app.domain.queue import AssignmentPolicy, Queue
from app.domain.task import Task, TaskStatus
from app.domain.workflow import (
    WorkflowDefinition,
    WorkflowState,
    WorkflowTransition,
    WorkflowVersion,
    WorkflowVersionStatus,
)
from app.infrastructure.persistence.models import (
    Case as CaseRow,
)
from app.infrastructure.persistence.models import (
    CaseType as CaseTypeRow,
)
from app.infrastructure.persistence.models import (
    CaseTypeVersion as CaseTypeVersionRow,
)
from app.infrastructure.persistence.models import (
    LegalHold as LegalHoldRow,
)
from app.infrastructure.persistence.models import (
    Participant as ParticipantRow,
)
from app.infrastructure.persistence.models import Queue as QueueRow
from app.infrastructure.persistence.models import Task as TaskRow
from app.infrastructure.persistence.models import WorkflowDefinition as WorkflowDefinitionRow
from app.infrastructure.persistence.models import WorkflowVersion as WorkflowVersionRow


def workflow_definition_to_domain(row: WorkflowDefinitionRow) -> WorkflowDefinition:
    """Reconstruct a workflow catalog header from SQL."""
    return WorkflowDefinition(
        id=row.id,
        tenant_id=row.tenant_id,
        key=row.key,
        name=row.name,
        description=row.description,
        current_version_id=row.current_version_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def workflow_definition_from_domain(value: WorkflowDefinition) -> dict[str, Any]:
    """Flatten a workflow catalog header for SQL persistence."""
    created = value.created_at or datetime.now(UTC)
    return {
        "id": value.id,
        "tenant_id": value.tenant_id,
        "key": value.key,
        "name": value.name,
        "description": value.description,
        "current_version_id": value.current_version_id,
        "created_at": created,
        "updated_at": value.updated_at or created,
    }


def workflow_version_to_domain(row: WorkflowVersionRow) -> WorkflowVersion:
    """Reconstruct a workflow version from its declarative JSON contract."""
    raw = row.definition_json or {}
    states = tuple(WorkflowState(**state) for state in raw.get("states", ()))
    transitions = tuple(
        WorkflowTransition(**transition) for transition in raw.get("transitions", ())
    )
    return WorkflowVersion(
        id=row.id,
        tenant_id=row.tenant_id,
        workflow_definition_id=row.workflow_definition_id,
        version_number=row.version_number,
        states=states,
        transitions=transitions,
        status=WorkflowVersionStatus(row.status),
        published_at=row.published_at,
        published_by=row.published_by,
        created_at=row.created_at,
    )


def workflow_version_from_domain(value: WorkflowVersion) -> dict[str, Any]:
    """Flatten workflow states and transitions into portable JSON."""
    created = value.created_at or datetime.now(UTC)
    return {
        "id": value.id,
        "tenant_id": value.tenant_id,
        "workflow_definition_id": value.workflow_definition_id,
        "version_number": value.version_number,
        "definition_json": {
            "states": [
                {
                    "key": s.key,
                    "label": s.label,
                    "is_initial": s.is_initial,
                    "is_terminal": s.is_terminal,
                }
                for s in value.states
            ],
            "transitions": [
                {
                    "key": t.key,
                    "from_state": t.from_state,
                    "to_state": t.to_state,
                    "human_driven": t.human_driven,
                    "guard": t.guard,
                    "actions": [dict(a) for a in t.actions],
                }
                for t in value.transitions
            ],
        },
        "status": value.status.value,
        "published_at": value.published_at,
        "published_by": value.published_by,
        "created_at": created,
    }


# ----- Case ------------------------------------------------------------------


def case_to_domain(row: CaseRow) -> Case:
    return Case(
        id=row.id,
        tenant_id=row.tenant_id,
        reference=CaseReference(row.reference),
        case_type_version_id=row.case_type_version_id,
        status=CaseStatus(row.status),
        priority=row.priority,
        title=row.title,
        description=row.description,
        data=row.data,
        current_workflow_state=row.current_workflow_state,
        version_token=row.version_token,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        closed_at=row.closed_at,
    )


def case_from_domain(c: Case) -> dict[str, Any]:
    return {
        "id": c.id,
        "tenant_id": c.tenant_id,
        "reference": c.reference.value,
        "case_type_version_id": c.case_type_version_id,
        "status": c.status.value,
        "priority": c.priority,
        "title": c.title,
        "description": c.description,
        "data": c.data,
        "current_workflow_state": c.current_workflow_state,
        "version_token": c.version_token,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
        "created_by": c.created_by,
        "closed_at": c.closed_at,
    }


# ----- CaseType / CaseTypeVersion -------------------------------------------


def case_type_to_domain(row: CaseTypeRow) -> CaseType:
    return CaseType(
        id=row.id,
        tenant_id=row.tenant_id,
        key=row.key,
        name=row.name,
        description=row.description,
        sensitivity_default=row.sensitivity_default,
        current_version_id=row.current_version_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def case_type_from_domain(c: CaseType) -> dict[str, Any]:
    created = c.created_at or datetime.now(UTC)
    return {
        "id": c.id,
        "tenant_id": c.tenant_id,
        "key": c.key,
        "name": c.name,
        "description": c.description,
        "sensitivity_default": c.sensitivity_default,
        "current_version_id": c.current_version_id,
        "created_at": created,
        "updated_at": c.updated_at or created,
    }


def case_type_version_to_domain(row: CaseTypeVersionRow) -> CaseTypeVersion:
    """Reconstruct the domain ``CaseTypeVersion`` from its ORM row.

    The schema JSON is the authoritative source for fields. The
    ``document_classifications`` and ``rules`` JSON columns are
    reconstructed as tuples so the domain entity remains hashable / frozen.
    Empty JSON arrays map to empty tuples to satisfy the type contract.
    """
    raw = row.schema_json or {}
    fields = tuple(
        CaseTypeField(
            key=f["key"],
            label=f.get("label", f["key"]),
            type=f["type"],
            required=f.get("required", False),
            enum_values=tuple(f.get("enum_values", ())),
            sensitivity=f.get("sensitivity", "internal"),
            searchable=f.get("searchable", False),
        )
        for f in raw.get("fields", ())
    )
    classifications = tuple(row.document_classifications or ())
    rules: tuple[dict[str, Any], ...] = tuple(dict(r) for r in (row.rules or []))
    return CaseTypeVersion(
        id=row.id,
        tenant_id=row.tenant_id,
        case_type_id=row.case_type_id,
        version_number=row.version_number,
        schema=CaseTypeSchema(
            fields=fields,
            document_classifications=classifications,
            rules=rules,
            document_max_size_bytes=raw.get("document_max_size_bytes"),
            document_allowed_content_types=tuple(raw.get("document_allowed_content_types", ())),
            document_reject_duplicates=bool(raw.get("document_reject_duplicates", False)),
        ),
        status=CaseTypeVersionStatus(row.status),
        published_at=row.published_at,
        published_by=row.published_by,
        created_at=row.created_at,
    )


def case_type_version_from_domain(
    v: CaseTypeVersion, *, created_at: datetime | None = None
) -> dict[str, Any]:
    """Flatten a domain ``CaseTypeVersion`` into an ORM-row dict.

    The schema's ``document_classifications`` and ``rules`` are split into
    dedicated JSON columns so the publication-history reader can answer
    "what classifications were allowed on this version" without parsing
    the fields blob (FR-033, P10-US4).
    """
    created = created_at or v.created_at or datetime.now(UTC)
    return {
        "id": v.id,
        "tenant_id": v.tenant_id,
        "case_type_id": v.case_type_id,
        "version_number": v.version_number,
        "schema_json": {
            "fields": [
                {
                    "key": f.key,
                    "label": f.label,
                    "type": f.type,
                    "required": f.required,
                    "enum_values": list(f.enum_values),
                    "sensitivity": f.sensitivity,
                    "searchable": f.searchable,
                }
                for f in v.schema.fields
            ],
            "document_max_size_bytes": v.schema.document_max_size_bytes,
            "document_allowed_content_types": list(v.schema.document_allowed_content_types),
            "document_reject_duplicates": v.schema.document_reject_duplicates,
        },
        "document_classifications": list(v.schema.document_classifications),
        "rules": [dict(r) for r in v.schema.rules],
        "status": v.status.value,
        "published_at": v.published_at,
        "published_by": v.published_by,
        "created_at": created,
    }


def ensure_uuid(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def task_to_domain(row: TaskRow) -> Task:
    """Convert a persisted task row into the immutable task aggregate."""
    return Task(
        id=row.id,
        tenant_id=row.tenant_id,
        case_id=row.case_id,
        type=row.type,
        title=row.title,
        status=TaskStatus(row.status),
        priority=row.priority,
        assignee_user_id=row.assignee_user_id,
        assignee_queue_id=row.assignee_queue_id,
        due_at=row.due_at,
        created_at=row.created_at,
        completed_at=row.completed_at,
        completed_by=row.completed_by,
        outcome=row.completion_outcome,
        version_token=row.version_token,
        created_by_transition=row.created_by_transition,
    )


def task_from_domain(task: Task) -> dict[str, Any]:
    """Flatten a task aggregate into portable ORM column values."""
    created = task.created_at or datetime.now(UTC)
    return {
        "id": task.id,
        "tenant_id": task.tenant_id,
        "case_id": task.case_id,
        "type": task.type,
        "title": task.title,
        "status": task.status.value,
        "priority": task.priority,
        "assignee_user_id": task.assignee_user_id,
        "assignee_queue_id": task.assignee_queue_id,
        "due_at": task.due_at,
        "completed_at": task.completed_at,
        "completed_by": task.completed_by,
        "completion_outcome": task.outcome,
        "version_token": task.version_token,
        "created_by_transition": task.created_by_transition,
        "created_at": created,
        "updated_at": created,
    }


def participant_to_domain(row: ParticipantRow) -> Participant:
    """Convert a participant ORM row to the framework-free domain object."""
    return Participant(
        id=row.id,
        tenant_id=row.tenant_id,
        case_id=row.case_id,
        role=row.role,
        display_name=row.display_name,
        external_ref=row.external_ref,
        source=row.source,
        is_primary=row.is_primary,
        added_at=row.added_at,
        removed_at=row.removed_at,
    )


def participant_from_domain(participant: Participant) -> dict[str, Any]:
    """Flatten a participant for a SQLAlchemy row."""
    return {
        "id": participant.id,
        "tenant_id": participant.tenant_id,
        "case_id": participant.case_id,
        "role": participant.role,
        "display_name": participant.display_name,
        "external_ref": participant.external_ref,
        "source": participant.source,
        "is_primary": participant.is_primary,
        "added_at": participant.added_at,
        "removed_at": participant.removed_at,
        "created_at": participant.added_at,
        "updated_at": participant.removed_at or participant.added_at,
    }


def queue_to_domain(row: QueueRow) -> Queue:
    """Reconstruct the domain ``Queue`` aggregate from its ORM row.

    Why ``ensure_uuid`` on members: the JSON column stores UUID strings
    (portable across SQLite/Postgres); the aggregate carries real UUIDs.

    # INTEGRATION_SEAM: visibility_scope column (B-5 queue-admin API)

    ``Queue.visibility_scope`` (FM-016, P02-US3) has no column yet —
    this task ships no tables — so rows read here default to
    ``VisibilityScope.TENANT`` (fail-closed: pre-scope queues stay
    tenant-visible, scope can only narrow). The B-5 migration adds
    the ``visibility_scope`` column and extends both mappers; until
    then the SQL adapter round-trips every other field exactly.
    """
    return Queue(
        id=row.id,
        tenant_id=row.tenant_id,
        key=row.key,
        name=row.name,
        description=row.description or "",
        assignment_policy=AssignmentPolicy(row.assignment_policy),
        capacity=row.capacity,
        member_user_ids=tuple(ensure_uuid(v) for v in (row.member_user_ids or [])),
        rr_cursor=row.rr_cursor,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version_token=row.version_token,
    )


def queue_from_domain(queue: Queue) -> dict[str, Any]:
    """Flatten a ``Queue`` aggregate into portable ORM column values."""
    created = queue.created_at or datetime.now(UTC)
    return {
        "id": queue.id,
        "tenant_id": queue.tenant_id,
        "key": queue.key,
        "name": queue.name,
        "description": queue.description or "",
        "assignment_policy": queue.assignment_policy.value,
        "capacity": queue.capacity,
        "member_user_ids": [str(uid) for uid in queue.member_user_ids],
        "rr_cursor": queue.rr_cursor,
        "version_token": queue.version_token,
        "created_at": created,
        "updated_at": queue.updated_at or created,
    }


# ----- LegalHold -----------------------------------------------------------


def legal_hold_to_domain(row: LegalHoldRow) -> LegalHold:
    """Reconstruct the domain ``LegalHold`` aggregate from its ORM row."""
    return LegalHold(
        id=row.id,
        tenant_id=row.tenant_id,
        case_id=row.case_id,
        placed_by=row.placed_by,
        placed_reason=row.placed_reason,
        placed_at=_normalize_dt(row.placed_at),
        released_by=row.released_by,
        released_reason=row.released_reason,
        released_at=_normalize_dt(row.released_at),
    )


def legal_hold_from_domain(h: LegalHold) -> dict[str, Any]:
    """Flatten a domain ``LegalHold`` into an ORM-row dict."""
    return {
        "id": h.id,
        "tenant_id": h.tenant_id,
        "case_id": h.case_id,
        "placed_by": h.placed_by,
        "placed_reason": h.placed_reason,
        "placed_at": h.placed_at,
        "released_by": h.released_by,
        "released_reason": h.released_reason,
        "released_at": h.released_at,
        "is_active": h.is_active,
        "created_at": h.placed_at,
        "updated_at": h.released_at or h.placed_at,
    }


def _normalize_dt(value: datetime | None) -> datetime | None:
    """Ensure domain datetimes are tz-aware (SQLite returns naive).

    SQLite (test profile) round-trips datetime columns as naive; the
    domain clock is aware. Normalizing at the mapping boundary keeps
    the aggregate consistent without dialect-specific DDL.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
