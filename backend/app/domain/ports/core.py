"""Core persistence ports (Case, CaseType, Document, Tenant).

The domain layer depends on these abstractions only. Adapters in
``app.infrastructure.*`` implement them. Swapping a local adapter for an
Azure adapter requires no domain or application changes.

# Tenant isolation contract (FM-002 / P11-US2)

**Every** repository method takes an explicit ``tenant_id: UUID`` as its
first query-scoping argument, and **every** implementation must include
``WHERE tenant_id = :tenant_id`` in its SQL. This is enforced three ways:

1. **Type-level**: a Protocol subclass of ``TenantScoped`` below requires
   the ``tenant_id`` parameter on every method that takes one. Any
   ``Repository`` that does not declare it fails to satisfy the protocol
   at static-check time.
2. **Implementation-level**: ``app/db.py`` ships a SQLAlchemy
   ``before_compile`` listener (``tenant_filter_listener``) that injects
   a ``WHERE tenant_id = :current_tenant_id`` clause into ``SELECT`` /
   ``UPDATE`` / ``DELETE`` statements. The listener is opt-in via
   ``settings.tenant_listener_enabled`` and is **defense-in-depth** —
   it is never the only mechanism.
3. **Test-level**: ``tests/security/test_tenant_isolation.py`` exercises
   two tenants in parallel and asserts that cross-tenant reads and writes
   fail with ``NotFound`` / tenant-mismatch errors, **regardless of
   whether the listener is on or off**.

A single missing filter is a security bug; the three layers make that
hard to ship unnoticed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.domain.case import Case, CaseReference
from app.domain.case_type import CaseType, CaseTypeVersion
from app.domain.document import Document, DocumentRedaction, DocumentVersion

# ---------------------------------------------------------------------------
# Tenant scoping: every query-scoping method on a repository must accept
# `tenant_id` as the first parameter. The Protocol below captures the
# intent at the type level: if a method takes a tenant selector, it must
# be a `UUID` named `tenant_id`.
# ---------------------------------------------------------------------------


@runtime_checkable
class TenantScoped(Protocol):
    """Marker protocol: every method that scopes data MUST take tenant_id."""

    # Protocol bodies cannot list methods, but the contract is enforced by
    # requiring every Repository below to subclass this and by repository
    # tests asserting the parameter signature. Static type-checkers honor
    # the explicit `tenant_id: UUID` first parameter on each method.
    pass


# ---------------------------------------------------------------------------
# Persistence ports
# ---------------------------------------------------------------------------


class CaseRepository(TenantScoped, Protocol):
    """Case aggregate persistence.

    Every method takes ``tenant_id`` first; SQL implementations must
    include ``WHERE tenant_id = :tenant_id``.
    """

    async def add(self, case: Case) -> None:
        """Persist a new case. The tenant_id comes from the case itself."""

    async def update(self, case: Case) -> None:
        """Persist changes to an existing case (P01-US3 / FR-003).

        Implementations MUST enforce optimistic concurrency on
        ``version_token`` (a stale token raises ``Conflict``). The
        caller is expected to have already bumped ``version_token``
        via :meth:`Case.edit`. The repository compares the row's stored
        ``version_token`` against the aggregate's ``version_token`` and
        refuses to overwrite when they differ so a concurrent editor's
        write is never silently lost (Constitution V).
        """

    async def get(self, tenant_id: UUID, case_id: UUID) -> Case | None:
        """Fetch one case by id, scoped to tenant. Returns None for
        missing OR cross-tenant (never raise on cross-tenant: the
        absence is indistinguishable from non-existence)."""

    async def get_by_reference(self, tenant_id: UUID, reference: CaseReference) -> Case | None:
        """Fetch one case by human reference, scoped to tenant."""

    async def list(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Case]:
        """List cases for a tenant, newest first."""

    async def next_reference(
        self, tenant_id: UUID, *, now: datetime, type_key: str | None = None
    ) -> CaseReference:
        """Issue the next monotonic reference for the tenant.

        Renders the tenant's configured ``ReferenceFormat`` template
        (FM-009) when one exists, else the platform default
        (``C-{TENANT}-{YYYY}-{SEQ}``). ``type_key`` supplies the
        ``{TYPE}`` token; templates that require it raise
        ``InvariantViolation`` when it is absent (fail-closed so a
        typeless caller cannot emit a malformed reference).
        """

    async def list_open_for_queues(
        self,
        tenant_id: UUID,
        *,
        queue_keys: tuple[str, ...],
        limit: int = 200,
    ) -> Sequence[Case]:
        """Return open cases with an open task assigned to one of ``queue_keys``.

        P12-US1 / FM-051: the operational reports scope open cases to
        the supervisor's team queues. Implementations MUST include
        ``WHERE tenant_id = :tenant_id`` and MUST restrict to
        open cases with a same-tenant open task in the supplied queue keys.
        Cross-tenant queue keys are silently dropped (the queue is
        invisible from this tenant). Order: ``updated_at DESC`` so
        the supervisor sees the freshest work first.
        """


class CaseTypeRepository(TenantScoped, Protocol):
    """CaseType aggregate persistence."""

    async def get(self, tenant_id: UUID, case_type_id: UUID) -> CaseType | None: ...

    async def get_by_key(self, tenant_id: UUID, key: str) -> CaseType | None: ...

    async def add(self, case_type: CaseType) -> None: ...


class CaseTypeVersionRepository(TenantScoped, Protocol):
    """CaseTypeVersion aggregate persistence."""

    async def get(self, tenant_id: UUID, case_type_version_id: UUID) -> CaseTypeVersion | None: ...

    async def get_published_for_case_type(
        self, tenant_id: UUID, case_type_id: UUID
    ) -> CaseTypeVersion | None: ...

    async def get_by_key(self, tenant_id: UUID, case_type_key: str) -> CaseTypeVersion | None:
        """Resolve the published version for a case type by its stable key."""

    async def add(self, version: CaseTypeVersion) -> None: ...


class DocumentRepository(TenantScoped, Protocol):
    """Document + DocumentVersion aggregate persistence
    (P03-US1 / FM-022, P03-US2 / FM-023).

    The first-slice methods support the upload flow
    (``add_document_and_version``) and the version-by-add flow
    (``add_version``, ``get``, ``list_versions``). Tenant isolation is
    explicit on every read; the SQL adapter carries ``WHERE
    tenant_id = :tenant_id`` on every query (defense-in-depth in
    addition to the per-row tenant_id at write time).
    """

    async def add_document_and_version(
        self,
        *,
        document: Document,
        version: DocumentVersion,
    ) -> None:
        """Stage a new Document + its initial DocumentVersion row.

        Implementations MUST insert both rows so the use case boundary
        commits once and the audit + outbox rows share the same
        transaction.
        """

    async def get(self, tenant_id: UUID, document_id: UUID) -> Document | None:
        """Resolve a Document by id within the actor's tenant.

        Returns ``None`` for a missing id OR for an id that belongs to
        a different tenant — the two cases are intentionally
        indistinguishable so the API never confirms cross-tenant
        existence (Constitution V).
        """

    async def add_version(
        self,
        *,
        document: Document,
        version: DocumentVersion,
    ) -> None:
        """Append a DocumentVersion row AND update the parent
        ``documents.current_version`` pointer to the new version number.

        The metadata row update and the version-row insert must share
        one transaction so a partial commit never leaves the
        ``current_version`` pointer ahead of the missing version row
        or vice versa. Implementations MUST update ``updated_at`` on
        the document row to match ``version.uploaded_at``.
        """

    async def list_versions(self, tenant_id: UUID, document_id: UUID) -> list[DocumentVersion]:
        """Return all DocumentVersion rows for a document in ascending
        ``version_number`` order.

        Returns an empty list for a document id that does not exist in
        the tenant. The caller decides whether to surface the empty
        list as a 404.
        """

    async def save_locked(self, document: Document) -> None:
        """Persist the ``locked`` / ``locked_at`` / ``locked_by`` flags
        on the parent ``documents`` row.

        Tenant-scoped UPDATE: a cross-tenant id cannot match the
        WHERE clause so a leaked id cannot be silently locked in
        another tenant (Constitution V).
        """

    async def add_redaction(
        self,
        *,
        redaction: DocumentRedaction,
        supersede_prior: bool,
    ) -> None:
        """Insert a new ``document_redactions`` row.

        When ``supersede_prior`` is True, any existing ``ACTIVE`` row
        for the same ``(tenant_id, document_id)`` is marked
        ``SUPERSEDED`` in the SAME transaction so the timeline shows
        exactly one active redaction per document at any time
        (P03-US6 — "versioned redactions preserving original"). The
        original document + version rows are untouched (Constitution
        XV — the original bytes remain retrievable for audit replay).
        """

    async def list_redactions(self, tenant_id: UUID, document_id: UUID) -> list[DocumentRedaction]:
        """Return all redaction rows for a document, oldest first.

        Tenant-scoped; a cross-tenant id returns the empty list so the
        caller can surface it as ``NotFound`` without an existence
        oracle (Constitution V, FM-025 rendition reads).
        """

    async def get_active_redaction(
        self, tenant_id: UUID, document_id: UUID
    ) -> DocumentRedaction | None:
        """Return the ACTIVE redaction for a document, if any.

        Tenant-scoped; ``None`` for a missing document, a missing
        redaction, or a cross-tenant id (all indistinguishable).
        """

    async def save_sensitivity(self, document: Document) -> None:
        """Persist a sensitivity change on the parent ``documents`` row.

        Tenant-scoped UPDATE (``WHERE tenant_id = :tenant_id``); the
        caller commits once so the restriction flip + audit + outbox
        land atomically (FM-025 restrict). Locked documents are refused
        in the domain (``Document.with_sensitivity``) before this call.
        """

    async def list_for_case(self, *, tenant_id: UUID, case_id: UUID) -> Sequence[Document]:
        """Return every document attached to ``case_id`` in the actor's tenant.

        Tenant-scoped (``WHERE tenant_id = :tenant_id AND case_id =
        :case_id``); a cross-tenant ``case_id`` returns the empty
        sequence (the application layer treats it as ``NotFound``).

        The application use case filters the result by the case-type's
        ``document_classifications`` allowlist so a leaked id cannot
        surface an unauthorized classification (Constitution V).
        Returns rows ordered by ``created_at`` descending so the
        workspace renders the most recent first.
        """


class TenantRepository(TenantScoped, Protocol):
    """Tenant lookup port.

    The case reference format is `C-{tenant_short}-{YYYY}-{SEQ:06d}`. The
    domain cannot import SQLAlchemy, so the repository that implements
    `CaseRepository.next_reference` resolves the tenant's short key
    through this port. Keeping it as a Protocol keeps `application` and
    `domain` decoupled from the persistence model.
    """

    async def short_key(self, tenant_id: UUID) -> str | None:
        """Return the tenant's short key (uppercase alnum/dash, <=16 chars).

        Returns None for unknown tenants.
        """


__all__ = [
    "CaseRepository",
    "CaseTypeRepository",
    "CaseTypeVersionRepository",
    "DocumentRepository",
    "TenantRepository",
    "TenantScoped",
]
