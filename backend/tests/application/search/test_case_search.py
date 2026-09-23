"""Tests for `app.application.search.case_search.handle` (P06 / FM-031).

Acceptance scenarios (per task spec):

1. **Tenant isolation**: a search filter (priority) returns only the
   actor's own tenant's cases — a case in another tenant with the same
   priority must not appear in the result set.
2. **Sensitive redaction**: when the actor lacks the sensitive-read
   grant, the snippet's restricted/phi field values are replaced with
   the ``Redacted`` marker. Non-sensitive fields pass through.
3. **Non-searchable rejection**: a filter that references a key not
   declared ``searchable`` in the published CaseTypeVersion raises
   ``ValidationFailed`` so the router maps it to 400.
4. **Pagination**: ``limit`` / ``offset`` are honored and out-of-range
   values are rejected.
5. **LIKE not JSONB**: covered separately in the SQL adapter tests
   (`test_sql_search_provider.py`); this module exercises the
   application-layer contract only.

The fakes are intentionally hermetic: the use case depends on a
``SearchProvider`` instance, so an in-memory implementation stands in
for the SQL adapter. Tests run without Postgres / SQLite.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.search import case_search
from app.domain.case_type import CaseTypeField, CaseTypeSchema
from app.domain.errors import Forbidden, ValidationFailed
from app.domain.ports import (
    SearchHit,
    SearchProvider,
    SearchResultPage,
    SearchSpec,
)

# ---------------------------------------------------------------------------
# Fixtures + constants
# ---------------------------------------------------------------------------


ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
INTAKE_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
REVIEWER_USER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredCase:
    tenant_id: UUID
    case_id: UUID
    reference: str
    priority: str
    status: str
    title: str
    data: dict[str, Any]


class InMemorySearchProvider(SearchProvider):
    """Portable-shaped in-memory search provider.

    Mirrors the SQL adapter's contract: tenant scoping, filter
    validation, snippet projection, per-field redaction. It implements
    the LIKE-via-substring shape directly because the in-memory model
    already holds structured data; the SQL adapter instead uses
    portable LIKE on the JSON TEXT representation.
    """

    def __init__(self) -> None:
        self.cases: list[StoredCase] = []

    async def search(self, spec: SearchSpec) -> SearchResultPage:
        items: list[StoredCase] = []
        for c in self.cases:
            if c.tenant_id != spec.tenant_id:
                continue
            if spec.text_query and spec.text_query.lower() not in c.title.lower():
                continue
            ok = True
            for f in spec.filters:
                if not _match_filter(c, f):
                    ok = False
                    break
            if ok:
                items.append(c)
        total = len(items)
        # Stable order: tenant_id then reference for determinism.
        items.sort(key=lambda c: (c.reference,))
        page = items[spec.offset : spec.offset + spec.limit]
        snippet_keys = spec.searchable_field_keys
        sensitive = set(spec.sensitive_field_keys)
        hits = [
            SearchHit(
                case_id=c.case_id,
                reference=c.reference,
                priority=c.priority,
                status=c.status,
                title=c.title,
                data_snippet=_build_snippet(c, snippet_keys, sensitive, spec.can_see_sensitive),
            )
            for c in page
        ]
        next_cursor: str | None = None
        if len(page) == spec.limit and spec.offset + spec.limit < total:
            next_cursor = f"{spec.offset + spec.limit}:{spec.limit}"
        return SearchResultPage(
            items=hits,
            total_considered=total,
            limit=spec.limit,
            offset=spec.offset,
            next_cursor=next_cursor,
        )


def _core_column(c: StoredCase, name: str) -> Any:
    return {
        "reference": c.reference,
        "status": c.status,
        "priority": c.priority,
        "title": c.title,
    }.get(name)


def _match_op(value: Any, op: str, target: Any) -> bool:
    if value is None:
        return False
    if op == "eq":
        return str(value).lower() == str(target).lower()
    if op == "like":
        return str(target).lower() in str(value).lower()
    if op == "in":
        lowered = [str(t).lower() for t in target]
        return str(value).lower() in lowered
    return False


def _match_filter(c: StoredCase, f: Any) -> bool:
    if f.field.startswith("data."):
        key = f.field[len("data.") :]
        return _match_op(c.data.get(key), f.op, f.value)
    column = _core_column(c, f.field)
    if column is None:
        return False
    return _match_op(column, f.op, f.value)


def _build_snippet(
    c: StoredCase,
    searchable: Sequence[str],
    sensitive: set[str],
    can_see_sensitive: bool,
) -> dict[str, Any]:
    snippet: dict[str, Any] = {}
    for key in searchable:
        if key in c.data:
            v = c.data[key]
            if not can_see_sensitive and key in sensitive:
                v = {"redacted": True, "reason": "sensitive"}
            snippet[key] = v
    return snippet


def _case(
    *,
    tenant_id: UUID,
    priority: str,
    title: str,
    status: str = "open",
    data: dict[str, Any] | None = None,
) -> StoredCase:
    return StoredCase(
        tenant_id=tenant_id,
        case_id=uuid4(),
        reference=f"C-{tenant_id.hex[:6].upper()}-2026-{uuid4().int % 1000000:06d}",
        priority=priority,
        status=status,
        title=title,
        data=data or {},
    )


@pytest.fixture
def provider() -> InMemorySearchProvider:
    return InMemorySearchProvider()


# ---------------------------------------------------------------------------
# 1. Tenant isolation
# ---------------------------------------------------------------------------


async def test_case_search__priority_filter__returns_only_actor_tenant(
    provider: InMemorySearchProvider,
) -> None:
    """Filtering by priority must return only the actor's own tenant's
    cases. A case with the same priority in another tenant must not
    leak into the result set.
    """
    acme_urgent = _case(tenant_id=ACME_TENANT, priority="urgent", title="A")
    acme_normal = _case(tenant_id=ACME_TENANT, priority="normal", title="B")
    other_urgent = _case(tenant_id=OTHER_TENANT, priority="urgent", title="C")
    provider.cases.extend([acme_urgent, acme_normal, other_urgent])

    result = await case_search.handle(
        case_search.CaseSearchQuery(
            tenant_id=ACME_TENANT,
            actor_id=INTAKE_USER,
            actor_roles=("intake",),
            filters=(("priority", "eq", "urgent"),),
        ),
        search_provider=provider,
    )

    assert [h.case_id for h in result.items] == [acme_urgent.case_id]
    assert result.total_considered == 1


async def test_case_search__unauthorized_actor__denies_with_forbidden(
    provider: InMemorySearchProvider,
) -> None:
    """An actor without any role grant for ``CASE_READ`` is denied.

    The application-layer policy evaluates deny-by-default: a request
    carrying no role at all (or a role that does not include
    ``CASE_READ``) raises ``Forbidden``. The use case never touches
    the SQL adapter.
    """
    provider.cases.append(_case(tenant_id=ACME_TENANT, priority="urgent", title="A"))
    with pytest.raises(Forbidden):
        await case_search.handle(
            case_search.CaseSearchQuery(
                tenant_id=ACME_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=(),  # empty role set -> deny
                filters=(("priority", "eq", "urgent"),),
            ),
            search_provider=provider,
        )


# ---------------------------------------------------------------------------
# 2. Sensitive redaction
# ---------------------------------------------------------------------------


async def test_case_search__sensitive_field__redacted_when_actor_lacks_grant(
    provider: InMemorySearchProvider,
) -> None:
    """When the actor's role does NOT grant ``CASE_READ_SENSITIVE``,
    the snippet's restricted/phi field values are replaced with the
    ``Redacted`` marker. Non-sensitive fields pass through untouched.
    """
    case = _case(
        tenant_id=ACME_TENANT,
        priority="normal",
        title="A",
        data={
            "participantName": "Jane Doe",  # searchable, internal
            "ssn": "111-22-3333",  # searchable, phi
            "notes": "free text",  # searchable, internal
        },
    )
    provider.cases.append(case)

    # Intake role does NOT carry ``CASE_READ_SENSITIVE`` -> redaction.
    result = await case_search.handle(
        case_search.CaseSearchQuery(
            tenant_id=ACME_TENANT,
            actor_id=INTAKE_USER,
            actor_roles=("intake",),
            searchable_field_keys=("participantName", "ssn", "notes"),
            sensitive_field_keys=("ssn",),
        ),
        search_provider=provider,
    )

    assert len(result.items) == 1
    snippet = result.items[0].data_snippet
    assert snippet["participantName"] == "Jane Doe"
    assert snippet["notes"] == "free text"
    assert snippet["ssn"] == {"redacted": True, "reason": "sensitive"}


async def test_case_search__sensitive_field__visible_to_supervisor(
    provider: InMemorySearchProvider,
) -> None:
    """Supervisors carry ``CASE_READ_SENSITIVE``; sensitive fields
    pass through unchanged in the snippet.
    """
    case = _case(
        tenant_id=ACME_TENANT,
        priority="normal",
        title="A",
        data={"ssn": "111-22-3333"},
    )
    provider.cases.append(case)

    result = await case_search.handle(
        case_search.CaseSearchQuery(
            tenant_id=ACME_TENANT,
            actor_id=REVIEWER_USER,
            actor_roles=("supervisor",),
            searchable_field_keys=("ssn",),
            sensitive_field_keys=("ssn",),
        ),
        search_provider=provider,
    )

    assert result.items[0].data_snippet["ssn"] == "111-22-3333"


# ---------------------------------------------------------------------------
# 3. Non-searchable rejection
# ---------------------------------------------------------------------------


async def test_case_search__non_searchable_field_filter__raises_validation_failed(
    provider: InMemorySearchProvider,
) -> None:
    """A filter on a field that is NOT declared searchable must be
    rejected as ``ValidationFailed`` (router maps to 400).
    """
    with pytest.raises(ValidationFailed) as excinfo:
        await case_search.handle(
            case_search.CaseSearchQuery(
                tenant_id=ACME_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=("intake",),
                filters=(("data.secret", "eq", "x"),),
                searchable_field_keys=("participantName",),
            ),
            search_provider=provider,
        )
    assert "data.secret" in str(excinfo.value)


async def test_case_search__unknown_core_field_filter__raises_validation_failed(
    provider: InMemorySearchProvider,
) -> None:
    """A filter on a core column that the application does not admit
    (e.g. ``data.created_by``) is rejected as ``ValidationFailed``.
    """
    with pytest.raises(ValidationFailed):
        await case_search.handle(
            case_search.CaseSearchQuery(
                tenant_id=ACME_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=("intake",),
                filters=(("nonexistent_column", "eq", "x"),),
            ),
            search_provider=provider,
        )


async def test_case_search__unsupported_operator__raises_validation_failed(
    provider: InMemorySearchProvider,
) -> None:
    """Operators outside the controlled vocabulary are rejected."""
    with pytest.raises(ValidationFailed) as excinfo:
        await case_search.handle(
            case_search.CaseSearchQuery(
                tenant_id=ACME_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=("intake",),
                filters=(("priority", "regex", "x"),),
            ),
            search_provider=provider,
        )
    assert "regex" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 4. Pagination
# ---------------------------------------------------------------------------


async def test_case_search__pagination__honors_limit_and_offset(
    provider: InMemorySearchProvider,
) -> None:
    """``limit`` clamps the page; ``offset`` advances it. Out-of-range
    values are rejected as ``ValidationFailed``.
    """
    provider.cases.extend(
        _case(tenant_id=ACME_TENANT, priority="urgent", title=f"T{i}") for i in range(5)
    )

    page1 = await case_search.handle(
        case_search.CaseSearchQuery(
            tenant_id=ACME_TENANT,
            actor_id=INTAKE_USER,
            actor_roles=("intake",),
            filters=(("priority", "eq", "urgent"),),
            limit=2,
            offset=0,
        ),
        search_provider=provider,
    )
    page2 = await case_search.handle(
        case_search.CaseSearchQuery(
            tenant_id=ACME_TENANT,
            actor_id=INTAKE_USER,
            actor_roles=("intake",),
            filters=(("priority", "eq", "urgent"),),
            limit=2,
            offset=2,
        ),
        search_provider=provider,
    )

    assert len(page1.items) == 2
    assert page1.next_cursor == "2:2"
    assert len(page2.items) == 2
    assert page2.next_cursor == "4:2"
    # Pages do not overlap.
    ids1 = {h.case_id for h in page1.items}
    ids2 = {h.case_id for h in page2.items}
    assert ids1.isdisjoint(ids2)


async def test_case_search__limit_too_high__raises_validation_failed(
    provider: InMemorySearchProvider,
) -> None:
    """Limit > 200 is rejected; the router layer mirrors the same
    guard via Pydantic but the application use case is the final
    authority.
    """
    with pytest.raises(ValidationFailed):
        await case_search.handle(
            case_search.CaseSearchQuery(
                tenant_id=ACME_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=("intake",),
                limit=201,
            ),
            search_provider=provider,
        )


async def test_case_search__negative_offset__raises_validation_failed(
    provider: InMemorySearchProvider,
) -> None:
    with pytest.raises(ValidationFailed):
        await case_search.handle(
            case_search.CaseSearchQuery(
                tenant_id=ACME_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=("intake",),
                offset=-1,
            ),
            search_provider=provider,
        )


# ---------------------------------------------------------------------------
# 5. Free-text query
# ---------------------------------------------------------------------------


async def test_case_search__free_text_query__matches_title(
    provider: InMemorySearchProvider,
) -> None:
    """The free-text portion matches ``title`` case-insensitively."""
    provider.cases.append(_case(tenant_id=ACME_TENANT, priority="normal", title="Onboarding help"))
    provider.cases.append(_case(tenant_id=ACME_TENANT, priority="normal", title="Password reset"))

    result = await case_search.handle(
        case_search.CaseSearchQuery(
            tenant_id=ACME_TENANT,
            actor_id=INTAKE_USER,
            actor_roles=("intake",),
            text_query="onboard",
        ),
        search_provider=provider,
    )

    assert len(result.items) == 1
    assert result.items[0].title == "Onboarding help"


# ---------------------------------------------------------------------------
# Helpers (unused; ensures schema imports are wired)
# ---------------------------------------------------------------------------


def _schema_field_unused() -> CaseTypeSchema:
    """Sanity-check the CaseTypeSchema import path for CI."""

    return CaseTypeSchema(
        fields=(
            CaseTypeField(
                key="participantName",
                label="Participant Name",
                type="text",
                searchable=True,
            ),
        )
    )


_ = _schema_field_unused
