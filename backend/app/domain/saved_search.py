"""Saved-search aggregate (P06-US3, US5, US6 / FM-032).

A saved search is a named, reusable query shape an authorized case worker
can re-execute later. The query shape is **declarative**: the same
``text_query`` + ``filters`` + ``sort`` tuple the use case consumes. The
saved row retains executable values for its owner, while ``query_shape``
is a privacy-safe projection used in audit records.

# Why a domain aggregate, not just a JSON blob

Saving a search is a small but security-relevant operation: the saved
row carries the actor's chosen filter shape, and re-executing it must
respect the **current** tenant's searchable-field contract (P05 / P06).
A JSON blob without invariants would let a caller persist a non-
searchable filter (rejected at execute time, but the row itself
becomes a footgun). The domain entity validates the shape at
construction so the row that lands in the relational store is always
executable.

# Tenant + ownership scope

Each saved search belongs to a single ``(tenant_id, owner_user_id)``
pair. The repository's ``list`` method always takes ``tenant_id`` first
and the SQL adapter carries ``WHERE tenant_id = :tenant_id`` on every
read so cross-tenant reads return ``None`` (Constitution V). A user
who owns a saved search can re-execute it; a security administrator
who holds ``AUDIT_READ`` can see the audit trail (P06-US5 / P09-US6).

# Sort

The sort spec is a bounded tuple of ``(field, direction)`` pairs.
Only fields the application already understands are admitted
(``reference``, ``title``, ``priority``, ``status``, ``created_at``,
``updated_at``). Anything else is rejected at construction so a saved
search cannot smuggle a column the SQL adapter does not project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation, ValidationFailed

# Controlled sort-field vocabulary. The application layer maps each
# key to a SQL column on the ``cases`` table; the domain refuses
# anything else at construction so a saved search cannot smuggle a
# column the SQL adapter does not project.
SORTABLE_FIELDS: Final[frozenset[str]] = frozenset(
    {"reference", "title", "priority", "status", "created_at", "updated_at"}
)
SORT_DIRECTIONS: Final[frozenset[str]] = frozenset({"asc", "desc"})


@dataclass(frozen=True, slots=True)
class SortSpec:
    """A single ``(field, direction)`` entry in the saved-search sort.

    ``field`` must be one of :data:`SORTABLE_FIELDS`; ``direction`` is
    ``asc`` or ``desc``. The dataclass is frozen so the repository can
    safely hash and serialize it as part of the query shape.
    """

    field: str
    direction: str = "asc"

    def __post_init__(self) -> None:
        if self.field not in SORTABLE_FIELDS:
            raise InvariantViolation(
                f"unsupported sort field {self.field!r}; supported: {sorted(SORTABLE_FIELDS)}"
            )
        if self.direction not in SORT_DIRECTIONS:
            raise InvariantViolation(
                f"unsupported sort direction {self.direction!r}; "
                f"supported: {sorted(SORT_DIRECTIONS)}"
            )

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "direction": self.direction}

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> SortSpec:
        return SortSpec(
            field=str(payload.get("field", "")),
            direction=str(payload.get("direction", "asc")),
        )


@dataclass(frozen=True, slots=True)
class SavedSearch:
    """A named, reusable case-search query (P06-US3 / FM-032).

    Attributes:
        tenant_id: The tenant the saved search is scoped to. The
            repository filters by tenant_id on every read so a row
            from tenant A is never visible in tenant B.
        owner_user_id: The actor who saved the search. Only the owner
            may re-execute it via the run path; the audit layer
            records both the owner and the executing actor (which can
            be the same person).
        name: A human-readable label, unique per ``(tenant_id,
            owner_user_id)``. The uniqueness constraint is enforced at
            the repository layer (it depends on the storage engine);
            the domain refuses empty or whitespace-only names.
        text_query: The free-text portion; ``None`` is permitted.
        filters: A tuple of ``(field, op, value)`` triples; same shape
            as :class:`app.application.search.case_search.CaseSearchQuery.filters`.
        sort: A tuple of :class:`SortSpec` entries; empty tuple means
            "no explicit sort".
        query_shape: The canonical, serialised representation of the
            query (``text_query`` + ``filters`` + ``sort``). The
            application layer sets it on construction so the audit row
            captures exactly the shape that was saved. We keep it as a
            plain dict so the JSON serialization is deterministic.
        id: Stable identifier; assigned by the factory.
        created_at: UTC timestamp; set on construction.
    """

    tenant_id: UUID
    owner_user_id: UUID
    name: str
    text_query: str | None
    filters: tuple[tuple[str, str, object], ...]
    sort: tuple[SortSpec, ...]
    query_shape: dict[str, Any]
    id: UUID = field(default_factory=uuid4)
    created_at: Any | None = None  # datetime; typed at the boundary

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValidationFailed("saved search name is required", field="name")
        if len(self.name) > 200:
            raise ValidationFailed("saved search name must be at most 200 characters", field="name")

    # ----- factories --------------------------------------------------------

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        name: str,
        text_query: str | None,
        filters: tuple[tuple[str, str, object], ...],
        sort: tuple[SortSpec, ...] = (),
        now: Any | None = None,
    ) -> SavedSearch:
        """Build a saved-search aggregate with a privacy-safe ``query_shape``.

        The executable query is retained in ``text_query`` and
        ``filters``. The persisted audit projection records fields and
        operators but redacts free text and every filter operand, which
        may contain sensitive content even when the field classification
        is incomplete.
        """
        shape = {
            "text_query": (
                {"redacted": True, "reason": "free_text"} if text_query is not None else None
            ),
            "filters": [
                {
                    "field": f[0],
                    "op": f[1],
                    "value": {"redacted": True, "reason": "query_value"},
                }
                for f in filters
            ],
            "sort": [s.to_dict() for s in sort],
        }
        return SavedSearch(
            id=uuid4(),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            name=name.strip(),
            text_query=text_query,
            filters=filters,
            sort=sort,
            query_shape=shape,
            created_at=now,
        )

    def to_wire(self) -> dict[str, Any]:
        """Project the entity onto the HTTP wire shape.

        The router uses this projection so the OpenAPI contract is
        defined in one place (the dataclass) and the test fixtures
        can build it from the same source.
        """
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "owner_user_id": self.owner_user_id,
            "name": self.name,
            "text_query": self.text_query,
            "filters": [{"field": f[0], "op": f[1], "value": f[2]} for f in self.filters],
            "sort": [s.to_dict() for s in self.sort],
            "query_shape": self.query_shape,
            "created_at": (self.created_at.isoformat() if self.created_at is not None else None),
        }


__all__ = [
    "SORT_DIRECTIONS",
    "SORTABLE_FIELDS",
    "SavedSearch",
    "SortSpec",
]
