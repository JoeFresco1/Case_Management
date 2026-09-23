"""Fakes and a small fixed Clock used by the create_case tests.

Why in-memory fakes: AGENTS.md §4.R5 explicitly prefers fakes over a
live container. The use case is a pure orchestration function; the only
side effect it has is "stage a row on a session" + "append audit + outbox
in the same transaction". Fakes capture the same shape (a list of staged
rows; a list of audit/outbox appends) and let the test assert exactly
what the use case committed.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from app.domain.case import Case, CaseReference
from app.domain.case_relationship import CaseRelationship, RelationshipKind
from app.domain.case_type import (
    CaseType,
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.document import Document, DocumentVersion
from app.domain.errors import Conflict, InvariantViolation


class FixedClock:
    """A `Clock` Protocol implementation that returns a fixed UTC instant.

    Using a fixed clock means the `CaseReference` sequence and the
    `created_at`/`updated_at` timestamps are deterministic, which makes
    the success test reproducible.
    """

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now


@dataclass
class StagedCase:
    """A case row staged on the in-memory repository."""

    case: Case


class InMemoryCaseRepository:
    """In-memory `CaseRepository`.

    `next_reference` is per-tenant monotonic: the next seq for the
    (tenant_id, year) prefix is the highest stored seq + 1. Tenant short
    codes are derived from `tenant_id` (`ACME` for the default test
    tenant, `OTHER` for a second one).
    """

    def __init__(self, *, tenant_short_keys: dict[UUID, str] | None = None) -> None:
        self._cases: list[Case] = []
        self._tenant_short = tenant_short_keys or {}

    def set_tenant_short(self, tenant_id: UUID, short: str) -> None:
        self._tenant_short[tenant_id] = short

    async def add(self, case: Case) -> None:
        self._cases.append(case)

    async def update(self, case: Case) -> None:
        """Persist an edited case (P01-US3).

        Mirrors the SQL adapter's optimistic concurrency: when the
        incoming ``version_token`` is not exactly ``stored + 1`` we
        raise :class:`Conflict` so the test exercises the same code
        path as production.
        """
        for idx, existing in enumerate(self._cases):
            if existing.tenant_id == case.tenant_id and existing.id == case.id:
                if existing.version_token != case.version_token - 1:
                    raise Conflict(f"stale concurrency token for case {case.id}")
                self._cases[idx] = case
                return
        raise Conflict(f"case {case.id} not found in tenant {case.tenant_id}")

    async def get(self, tenant_id: UUID, case_id: UUID) -> Case | None:
        for c in self._cases:
            if c.tenant_id == tenant_id and c.id == case_id:
                return c
        return None

    async def get_by_reference(self, tenant_id: UUID, reference: CaseReference) -> Case | None:
        for c in self._cases:
            if c.tenant_id == tenant_id and c.reference.value == reference.value:
                return c
        return None

    async def list(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Case]:
        return [c for c in self._cases if c.tenant_id == tenant_id]

    async def next_reference(
        self, tenant_id: UUID, *, now: datetime, type_key: str | None = None
    ) -> CaseReference:
        _ = type_key  # the fake honors the platform default template only
        short = self._tenant_short.get(tenant_id, "TEST")
        prefix = f"C-{short}-{now.year}-"
        seq = 0
        for c in self._cases:
            if c.tenant_id == tenant_id and c.reference.value.startswith(prefix):
                tail = c.reference.value[len(prefix) :]
                with contextlib.suppress(ValueError):
                    seq = max(seq, int(tail))
        return CaseReference(f"{prefix}{seq + 1:06d}")


class InMemoryCaseTypeRepository:
    """In-memory `CaseTypeRepository`."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, CaseType] = {}
        self._by_key: dict[tuple[UUID, str], CaseType] = {}

    def add(self, case_type: CaseType) -> None:
        self._by_id[case_type.id] = case_type
        self._by_key[(case_type.tenant_id, case_type.key)] = case_type

    async def get(self, tenant_id: UUID, case_type_id: UUID) -> CaseType | None:
        ct = self._by_id.get(case_type_id)
        if ct is None or ct.tenant_id != tenant_id:
            return None
        return ct

    async def get_by_key(self, tenant_id: UUID, key: str) -> CaseType | None:
        return self._by_key.get((tenant_id, key))


class InMemoryCaseTypeVersionRepository:
    """In-memory `CaseTypeVersionRepository`."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, CaseTypeVersion] = {}

    def add(self, version: CaseTypeVersion) -> None:
        self._by_id[version.id] = version

    async def get(self, tenant_id: UUID, case_type_version_id: UUID) -> CaseTypeVersion | None:
        v = self._by_id.get(case_type_version_id)
        if v is None or v.tenant_id != tenant_id:
            return None
        return v

    async def get_published_for_case_type(
        self, tenant_id: UUID, case_type_id: UUID
    ) -> CaseTypeVersion | None:
        for v in self._by_id.values():
            if (
                v.tenant_id == tenant_id
                and v.case_type_id == case_type_id
                and v.status is CaseTypeVersionStatus.PUBLISHED
            ):
                return v
        return None

    async def add_to_store(self, version: CaseTypeVersion) -> None:
        self.add(version)


@dataclass
class StagedAudit:
    tenant_id: UUID
    event_type: str
    actor_id: UUID
    actor_type: str
    subject_type: str
    subject_id: UUID
    case_id: UUID | None
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any]
    occurred_at: datetime


class InMemoryAuditStore:
    """In-memory `AuditEventStore`.

    Captures every `append` call so the test can assert the audit row
    landed in the same transaction as the case.
    """

    def __init__(self) -> None:
        self.events: list[StagedAudit] = []

    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID,
        actor_type: str,
        subject_type: str,
        subject_id: UUID,
        case_id: UUID | None,
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        self.events.append(
            StagedAudit(
                tenant_id=tenant_id,
                event_type=event_type,
                actor_id=actor_id,
                actor_type=actor_type,
                subject_type=subject_type,
                subject_id=subject_id,
                case_id=case_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                payload=payload,
                occurred_at=occurred_at,
            )
        )

    async def list_for_case(
        self,
        *,
        tenant_id: UUID,
        case_id: UUID,
        event_types: Sequence[str] | None = None,
        actor_id: UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[StagedAudit]:
        """Return audit rows for ``case_id`` in chronological order.

        Mirrors the ``AuditEventStore.list_for_case`` port contract
        used by the FM-042 evidence-report use case to pull the
        disposition-related audit rows for a single case. Tenant-
        scoped, optional filters on ``event_type`` / ``actor_id`` /
        ``occurred_at`` range, ``limit`` caps the result.
        """
        del cursor  # cursor is a no-op in the in-memory fake
        rows: list[StagedAudit] = []
        for event in self.events:
            if event.tenant_id != tenant_id:
                continue
            if event.case_id != case_id:
                continue
            if event_types is not None and event.event_type not in event_types:
                continue
            if actor_id is not None and event.actor_id != actor_id:
                continue
            if since is not None and event.occurred_at < since:
                continue
            if until is not None and event.occurred_at > until:
                continue
            rows.append(event)
        rows.sort(key=lambda e: e.occurred_at)
        return rows[:limit]


@dataclass
class StagedOutbox:
    tenant_id: UUID
    event_type: str
    event_id: UUID
    payload: dict[str, Any]


class InMemoryOutbox:
    """In-memory `OutboxWriter`."""

    def __init__(self) -> None:
        self.events: list[StagedOutbox] = []

    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        event_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        self.events.append(
            StagedOutbox(
                tenant_id=tenant_id,
                event_type=event_type,
                event_id=event_id,
                payload=payload,
            )
        )


# ----- Idempotency ----------------------------------------------------------


@dataclass
class StoredIdempotency:
    tenant_id: UUID
    key: str
    request_hash: str
    response_status: int
    response_body: dict[str, Any]
    correlation_id: str


class InMemoryIdempotencyStore:
    """In-memory `IdempotencyStore`.

    Tenant-scoped: ``get(tenant_A, key)`` returns None even if the same
    key was stored under tenant_B. This is what the SQL adapter does
    via `WHERE tenant_id = :tenant_id`.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, str], StoredIdempotency] = {}

    async def get(self, *, tenant_id: UUID, key: str) -> dict[str, Any] | None:
        row = self._rows.get((tenant_id, key))
        if row is None:
            return None
        return {
            "request_hash": row.request_hash,
            "response_status": row.response_status,
            "response_body": row.response_body,
            "correlation_id": row.correlation_id,
        }

    async def put(
        self,
        *,
        tenant_id: UUID,
        key: str,
        request_hash: str,
        response_status: int,
        response_body: dict[str, Any],
        correlation_id: str,
    ) -> None:
        self._rows[(tenant_id, key)] = StoredIdempotency(
            tenant_id=tenant_id,
            key=key,
            request_hash=request_hash,
            response_status=response_status,
            response_body=response_body,
            correlation_id=correlation_id,
        )


# ----- CaseRelationshipRepository -------------------------------------------


class InMemoryCaseRelationshipRepository:
    """In-memory ``CaseRelationshipRepository``.

    Stores every edge by id; tenant isolation is enforced by the
    explicit ``tenant_id`` filter on every read so a cross-tenant id
    returns an empty sequence (Constitution V).
    """

    def __init__(self) -> None:
        self._edges: list[CaseRelationship] = []

    @property
    def edges(self) -> list[CaseRelationship]:
        return list(self._edges)

    async def add_pair(self, edges: tuple[CaseRelationship, CaseRelationship]) -> None:
        if len(edges) != 2:
            raise InvariantViolation("add_pair requires exactly two edges")
        self._edges.append(edges[0])
        self._edges.append(edges[1])

    async def add(self, edge: CaseRelationship) -> None:
        self._edges.append(edge)

    async def list_for_case(
        self,
        tenant_id: UUID,
        case_id: UUID,
        *,
        kinds: Sequence[RelationshipKind] | None = None,
    ) -> Sequence[CaseRelationship]:
        out: list[CaseRelationship] = []
        for edge in self._edges:
            if edge.tenant_id != tenant_id:
                continue
            if edge.source_case_id != case_id:
                continue
            if kinds and edge.kind not in kinds:
                continue
            out.append(edge)
        return out

    async def get_pair(self, tenant_id: UUID, relationship_id: UUID) -> Sequence[CaseRelationship]:
        return [
            edge
            for edge in self._edges
            if edge.tenant_id == tenant_id and edge.relationship_id == relationship_id
        ]


# ----- DocumentRepository (subset used by split_case) -----------------------


class InMemoryDocumentRepository:
    """Minimal ``DocumentRepository`` for split-case tests.

    Implements only the methods the relationship use cases call:
    ``add_document_and_version`` and ``list_for_case``. The first-slice
    SQL adapter does not yet expose ``list_for_case``; the use case
    falls back gracefully when the method is missing.
    """

    def __init__(self) -> None:
        self._documents: list[Document] = []
        self._versions: list[DocumentVersion] = []

    @property
    def documents(self) -> list[Document]:
        return list(self._documents)

    @property
    def versions(self) -> list[DocumentVersion]:
        return list(self._versions)

    async def add_document_and_version(
        self,
        *,
        document: Document,
        version: DocumentVersion,
    ) -> None:
        self._documents.append(document)
        self._versions.append(version)

    async def list_for_case(self, *, tenant_id: UUID, case_id: UUID) -> list[Document]:
        return [d for d in self._documents if d.tenant_id == tenant_id and d.case_id == case_id]


# ----- Fixtures -------------------------------------------------------------


def make_case_type(
    *,
    tenant_id: UUID,
    key: str = "ServiceRequest",
    name: str = "Service Request",
    required_keys: tuple[str, ...] = ("title", "priority"),
) -> tuple[CaseType, CaseTypeVersion]:
    """Build a published `CaseType` + `CaseTypeVersion` with the given fields.

    The fields use `type=text` so any non-empty string passes the schema
    validator; required keys are the only ones that can cause
    `ValidationFailed`.
    """
    fields = tuple(
        CaseTypeField(
            key=required,
            label=required.title(),
            type="text",
            required=True,
        )
        for required in required_keys
    )
    version = CaseTypeVersion(
        id=uuid4(),
        tenant_id=tenant_id,
        case_type_id=uuid4(),
        version_number=1,
        schema=CaseTypeSchema(fields=fields),
        status=CaseTypeVersionStatus.PUBLISHED,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        published_by=uuid4(),
    )
    case_type = CaseType(
        id=version.case_type_id,
        tenant_id=tenant_id,
        key=key,
        name=name,
        description="",
        current_version_id=version.id,
    )
    return case_type, version


__all__ = [
    "FixedClock",
    "InMemoryAuditStore",
    "InMemoryCaseRelationshipRepository",
    "InMemoryCaseRepository",
    "InMemoryCaseTypeRepository",
    "InMemoryCaseTypeVersionRepository",
    "InMemoryDocumentRepository",
    "InMemoryIdempotencyStore",
    "InMemoryOutbox",
    "StagedAudit",
    "StagedCase",
    "StagedOutbox",
    "StoredIdempotency",
    "make_case_type",
]
