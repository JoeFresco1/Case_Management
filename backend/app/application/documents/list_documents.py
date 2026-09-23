"""List documents attached to a case (P03-US1 / FM-022 + P13-US11 / FM-057).

This use case is the read-side companion to ``upload_document`` and
``protect_document``. It is intentionally narrow: the only entry point
the GraphQL governed surface needs to satisfy ``case.documents``.

# Authorization (P03-US4 / P11 / FM-029)

The use case evaluates ``Action.DOCUMENT_READ`` against the case —
not against each document — because the policy resource is the
case-attached set, and because every document in the result is
returned at the same authorization level. A denied case read raises
``Forbidden``; the HTTP / GraphQL layer maps that to 403. There is
no per-document document-read action in the policy today; the case
scope is sufficient because all documents on a case share the same
classification policy.

# Document classification policy (P03-US4 / FM-022)

Each row's ``classification`` is checked against the
``CaseTypeVersion.schema.document_classifications`` allowlist before
it is emitted. A document whose classification is not on the
allowlist is **omitted** (not raised, not nulled) — the document
exists, but the case type does not permit exposing it through this
read path. Omission is indistinguishable from "no document of that
classification exists" so a leaked id cannot be detected by a side
channel (Constitution V).

# Locked documents

Locked documents remain readable. The ``locked`` flag is surfaced so
the client renders the lock state correctly. Mutations (new version
/ redaction) are deliberately NOT exposed through this use case —
``upload_document`` and ``protect_document`` are the only writers
and the GraphQL surface does not call them.

# Failure semantics
- ``Forbidden`` on deny.
- ``NotFound`` when the case does not exist in the actor's tenant
  (the cross-tenant case is handled the same way: the repository
  filters by tenant_id, so a cross-tenant id is also NotFound).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.document import Document
from app.domain.errors import NotFound
from app.domain.ports import (
    CaseRepository,
    CaseTypeVersionRepository,
    DocumentRepository,
)


@dataclass(frozen=True, slots=True)
class ListDocumentsForCaseQuery:
    """The input to ``list_documents_for_case.handle``.

    ``tenant_id`` and ``actor_id`` / ``actor_roles`` mirror the
    :class:`UserContext` shape used elsewhere; the use case derives
    them from the request scope, never from the caller-supplied
    arguments.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy (see create_case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


async def handle(
    query: ListDocumentsForCaseQuery,
    *,
    case_repo: CaseRepository,
    case_type_version_repo: CaseTypeVersionRepository,
    document_repo: DocumentRepository,
) -> list[Document]:
    """Return every document attached to the case, classification-filtered.

    Failure semantics:
    - Auth denial -> ``Forbidden`` (raised by ``authorize``).
    - Missing case -> ``NotFound`` (also covers cross-tenant ids).
    - Repository failure -> propagated unchanged.

    Returned documents are ordered by ``uploaded_at`` descending so
    the client renders the most recent first. Documents whose
    classification is not on the case-type's allowlist are filtered
    out before the return so a leaked id cannot surface an
    unauthorized classification through the GraphQL surface.
    """
    # 1. Authorization. The resource is the case; the policy evaluates
    # ``Action.DOCUMENT_READ`` against the actor's role set. A denied
    # read raises ``Forbidden``; the HTTP layer maps that to 403.
    actor_ctx = _ActorProxy(
        tenant_id=query.tenant_id,
        user_id=query.actor_id,
        roles=query.actor_roles,
    )
    await authorize(
        Action.DOCUMENT_READ,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=query.tenant_id, kind="case", id=query.case_id),
    )

    # 2. Resolve the case within the actor's tenant. Cross-tenant ids
    # resolve to ``None`` here (the repository filters by tenant_id)
    # and the use case raises ``NotFound`` so the GraphQL surface
    # returns ``null`` for the parent Case (Constitution V).
    case = await case_repo.get(query.tenant_id, query.case_id)
    if case is None:
        raise NotFound(f"case {query.case_id} not found")

    # 3. Resolve the case-type version to read the document
    # classification allowlist. The version was pinned at case
    # creation; the use case is the right place to consult the
    # allowlist because the policy is part of the case-type contract
    # (FM-008).
    version = await case_type_version_repo.get(query.tenant_id, case.case_type_version_id)
    if version is None:
        # The case row references a version that has since been
        # deleted; the read cannot proceed without the allowlist.
        raise NotFound(f"case type version {case.case_type_version_id} not found")

    # 4. Fetch all documents on the case. The repository today does
    # not expose a per-case method, so we filter the full tenant set.
    # Tenant isolation is still enforced: the filter below only ever
    # sees rows the repository returned for ``query.tenant_id``.
    # Implementations must include ``WHERE tenant_id = :tenant_id``
    # in the underlying query (AGENTS.md §12). The repository
    # contract is deliberately narrow for the first slice; a
    # dedicated ``list_for_case`` method lands when document counts
    # make a full-scan impractical.
    all_documents: list[Document] = list(
        await _list_documents_for_case(document_repo, query.tenant_id, case.id)
    )

    # 5. Classification filter. A document whose classification is
    # not on the allowlist is dropped silently (Constitution V — the
    # omission is indistinguishable from a non-existent document).
    allowed = set(version.schema.document_classifications)
    filtered = [d for d in all_documents if d.classification in allowed]

    # 6. Newest first.
    filtered.sort(key=lambda d: d.created_at, reverse=True)
    return filtered


async def _list_documents_for_case(
    repo: DocumentRepository, tenant_id: UUID, case_id: UUID
) -> list[Document]:
    """Return every document the repository knows for ``(tenant, case)``.

    A thin adapter that walks ``list_versions`` per document id is
    intentionally avoided (it would require knowing the document ids
    up front). Instead, we rely on the repository's full tenant
    listing. The future dedicated ``list_for_case`` port replaces
    this with a single SQL query.

    The repository here is duck-typed: today the
    ``DocumentRepository`` Protocol exposes upload / version /
    redaction / lock paths only; the GraphQL surface reads via the
    application use case and the implementation provides a tenant
    listing via a small extension. The interface below uses
    ``getattr`` so a missing helper surfaces as ``AttributeError``
    (caught by the test layer) rather than a static type-check
    failure.

    For the first slice the tests use an in-memory fake with
    ``list_for_case``; the SQL adapter will implement the same
    method. See ``tests/graphql/_fakes.py`` for the contract.
    """
    method = getattr(repo, "list_for_case", None)
    if method is None:
        raise NotImplementedError(
            "DocumentRepository must implement list_for_case for the "
            "GraphQL governed surface (FM-057)"
        )
    result = await method(tenant_id=tenant_id, case_id=case_id)
    return list(result)


__all__ = [
    "ListDocumentsForCaseQuery",
    "handle",
]
