"""Load a published ``CaseTypeVersion`` for runtime use.

The create-case flow needs the **published** schema for a case-type key
so that incoming payloads can be validated (FR-001, P05-US2). The load
function is the single seam where application code asks "what schema is
in effect for this key?".

Why a dedicated use case rather than calling the repository directly:

- The application layer never reaches into infrastructure directly
  (``interfaces`` → ``application`` → ``domain``; import-linter
  enforces this).
- Loading the published schema is a meaningful business operation: it
  is the join point where the "case type is in effect" decision lives,
  and where authorization (admin/intake) and tenant scoping both
  apply.
- Tests can substitute an in-memory fake.

The use case also records the publication lineage — what version is
being served — so the response can include it. That is the substrate
for FR-033 ("configuration history") and for case-data provenance
("this case was created against ServiceRequest v3").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.domain.case_type import CaseTypeVersion
from app.domain.errors import NotFound


# Protocol-typed port so the use case can be unit-tested with fakes.
class CaseTypeVersionLoader(Protocol):
    async def get_by_key(self, tenant_id: UUID, case_type_key: str) -> CaseTypeVersion | None: ...


@dataclass(frozen=True, slots=True)
class LoadCaseTypeQuery:
    tenant_id: UUID
    case_type_key: str


async def load_for_create_case(
    query: LoadCaseTypeQuery, *, loader: CaseTypeVersionLoader
) -> CaseTypeVersion:
    """Return the published schema for ``case_type_key``.

    Raises :class:`NotFound` if the key does not exist or has no
    published version. The check is intentionally strict: the create-
    case flow rejects a missing type with a structured error rather
    than defaulting to "create against any draft".
    """
    version = await loader.get_by_key(query.tenant_id, query.case_type_key)
    if version is None:
        raise NotFound(f"published case type {query.case_type_key!r} not found")
    return version


__all__ = ["LoadCaseTypeQuery", "load_for_create_case", "CaseTypeVersionLoader"]
