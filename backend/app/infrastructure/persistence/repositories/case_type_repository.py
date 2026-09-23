"""CaseType and CaseTypeVersion SQL repositories (FM-008 / FR-015, FR-033).

Two SQL adapters live in this module:

- ``SqlCaseTypeRepository`` — the CaseType header row (key, name,
  sensitivity_default, current_version_id).
- ``SqlCaseTypeVersionRepository`` — versioned schema snapshots with the
  ``draft → published → retired`` lifecycle.

Both adapters enforce tenant isolation explicitly: every method takes
``tenant_id`` and every SQL query carries ``WHERE tenant_id = :tenant_id``.
A buggy listener that forgets the filter still cannot leak cross-tenant
data; the test suite asserts this on every repository.

# Lifecycle enforcement

A published ``CaseTypeVersion`` is **immutable**. There is no UPDATE path
through these repositories on a non-draft version; the
``update_draft``/``publish``/``retire`` helpers check the row's status
before issuing the SQL. Database-level triggers (FM-003 hardening) and
ORM listeners arrive in a later phase; this module is the primary
mechanism.

# INTEGRATION_SEAM

Both classes are local adapters. Azure SQL implementations
(``SqlCaseTypeRepositoryAzure`` etc.) plug into the same Protocols in
``app/domain/ports.py`` and are wired by ``app/deps`` based on settings.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.case_type import (
    CaseType,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.errors import InvariantViolation, NotFound
from app.infrastructure.persistence.mapping import (
    case_type_from_domain,
    case_type_to_domain,
    case_type_version_from_domain,
    case_type_version_to_domain,
)
from app.infrastructure.persistence.models import (
    CaseType as CaseTypeRow,
)
from app.infrastructure.persistence.models import (
    CaseTypeVersion as CaseTypeVersionRow,
)


def _now_utc() -> datetime:
    """Return a tz-aware UTC ``datetime`` for ``created_at``/``updated_at``.

    Centralised so every row is stamped consistently and ``naive`` /
    ``aware`` mismatches are impossible.
    """
    return datetime.now(tz=UTC)


# ----- CaseType --------------------------------------------------------------


class SqlCaseTypeRepository:
    """Repository for the CaseType header row."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, tenant_id: UUID, case_type_id: UUID) -> CaseType | None:
        stmt = select(CaseTypeRow).where(
            CaseTypeRow.tenant_id == tenant_id, CaseTypeRow.id == case_type_id
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return case_type_to_domain(row) if row else None

    async def get_by_key(self, tenant_id: UUID, key: str) -> CaseType | None:
        stmt = select(CaseTypeRow).where(CaseTypeRow.tenant_id == tenant_id, CaseTypeRow.key == key)
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return case_type_to_domain(row) if row else None

    async def add(self, case_type: CaseType) -> None:
        self._s.add(CaseTypeRow(**case_type_from_domain(case_type)))

    async def list_for_tenant(self, tenant_id: UUID) -> list[CaseType]:
        """Return case-type headers visible to one tenant, ordered by key."""
        stmt = (
            select(CaseTypeRow)
            .where(CaseTypeRow.tenant_id == tenant_id)
            .order_by(CaseTypeRow.key.asc())
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [case_type_to_domain(row) for row in rows]

    async def set_current_version(
        self, tenant_id: UUID, case_type_id: UUID, version_id: UUID
    ) -> None:
        """Mark a published version as the CaseType's current version.

        Why a dedicated helper rather than a generic UPDATE: the intent
        is explicit (publishing) and the ``updated_at`` stamping lives
        in one place so the audit row can record it.
        """
        stmt = select(CaseTypeRow).where(
            CaseTypeRow.tenant_id == tenant_id, CaseTypeRow.id == case_type_id
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise NotFound("case type not found")
        row.current_version_id = version_id
        row.updated_at = _now_utc()


# ----- CaseTypeVersion -------------------------------------------------------


class SqlCaseTypeVersionRepository:
    """Repository for the versioned CaseType schema.

    Provides:

    - ``add`` / ``get`` / ``get_by_key`` / ``get_published_for_case_type``
      — read/write primitives used by other modules.
    - ``list_for_case_type`` — full publication history (FR-033).
    - ``update_draft`` — edit a draft before publication. Raises on any
      non-draft version.
    - ``mark_status`` — transition ``draft → published`` or
      ``published → retired``. Raises on an invalid transition.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, tenant_id: UUID, case_type_version_id: UUID) -> CaseTypeVersion | None:
        stmt = select(CaseTypeVersionRow).where(
            CaseTypeVersionRow.tenant_id == tenant_id,
            CaseTypeVersionRow.id == case_type_version_id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return case_type_version_to_domain(row) if row else None

    async def get_published_for_case_type(
        self, tenant_id: UUID, case_type_id: UUID
    ) -> CaseTypeVersion | None:
        stmt = (
            select(CaseTypeVersionRow)
            .where(
                CaseTypeVersionRow.tenant_id == tenant_id,
                CaseTypeVersionRow.case_type_id == case_type_id,
                CaseTypeVersionRow.status == CaseTypeVersionStatus.PUBLISHED.value,
            )
            .order_by(CaseTypeVersionRow.version_number.desc())
            .limit(1)
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return case_type_version_to_domain(row) if row else None

    async def get_by_key(self, tenant_id: UUID, case_type_key: str) -> CaseTypeVersion | None:
        """Resolve the **published** version for a case-type key.

        Joins through the CaseType header because the version row carries
        the schema but the key lives on the CaseType. The status filter
        ensures callers do not accidentally pick up a draft.
        """
        stmt = (
            select(CaseTypeVersionRow)
            .join(CaseTypeRow, CaseTypeRow.id == CaseTypeVersionRow.case_type_id)
            .where(
                CaseTypeVersionRow.tenant_id == tenant_id,
                CaseTypeRow.key == case_type_key,
                CaseTypeVersionRow.status == CaseTypeVersionStatus.PUBLISHED.value,
            )
            .order_by(CaseTypeVersionRow.version_number.desc())
            .limit(1)
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return case_type_version_to_domain(row) if row else None

    async def get_latest_for_case_type(
        self, tenant_id: UUID, case_type_id: UUID
    ) -> CaseTypeVersion | None:
        """Return the most-recent version regardless of status.

        Used to compute the next ``version_number`` when drafting a new
        version of an existing CaseType. Not exposed through the domain
        port; only the application layer calls it.
        """
        stmt = (
            select(CaseTypeVersionRow)
            .where(
                CaseTypeVersionRow.tenant_id == tenant_id,
                CaseTypeVersionRow.case_type_id == case_type_id,
            )
            .order_by(CaseTypeVersionRow.version_number.desc())
            .limit(1)
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return case_type_version_to_domain(row) if row else None

    async def list_for_case_type(
        self,
        tenant_id: UUID,
        case_type_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[CaseTypeVersion]:
        """Return every version for a CaseType, newest first.

        Implements FR-033 ("configuration history"): each row carries the
        status, version number, publish actor, and effective time so the
        admin UI can render the publication timeline (P10-US4).
        """
        stmt = (
            select(CaseTypeVersionRow)
            .where(
                CaseTypeVersionRow.tenant_id == tenant_id,
                CaseTypeVersionRow.case_type_id == case_type_id,
            )
            .order_by(CaseTypeVersionRow.version_number.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [case_type_version_to_domain(r) for r in rows]

    async def add(self, version: CaseTypeVersion) -> None:
        created_at = version.created_at or _now_utc()
        self._s.add(
            CaseTypeVersionRow(**case_type_version_from_domain(version, created_at=created_at))
        )

    async def update_draft(self, version: CaseTypeVersion) -> None:
        """Persist schema edits to a draft version.

        Raises ``InvariantViolation`` if the version is not in ``draft``
        status — this is the primary immutability guard for FR-015.
        Database-level triggers will join it in a later phase.
        """
        if version.status is not CaseTypeVersionStatus.DRAFT:
            raise InvariantViolation(f"cannot update a {version.status.value} version")
        stmt = select(CaseTypeVersionRow).where(
            CaseTypeVersionRow.tenant_id == version.tenant_id,
            CaseTypeVersionRow.id == version.id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise NotFound(f"case type version {version.id} not found")
        if row.status != CaseTypeVersionStatus.DRAFT.value:
            raise InvariantViolation(f"case type version {row.id} is {row.status}, not draft")
        # Rewrite the schema fields from the domain entity.
        payload = case_type_version_from_domain(version, created_at=row.created_at)
        row.schema_json = payload["schema_json"]
        row.document_classifications = payload["document_classifications"]
        row.rules = payload["rules"]

    async def mark_status(
        self,
        tenant_id: UUID,
        version_id: UUID,
        *,
        new_status: CaseTypeVersionStatus,
        published_at: datetime | None,
        published_by: UUID | None,
    ) -> None:
        """Transition a version's status atomically.

        Used by the publish and retire use cases. Raises if the current
        row status is incompatible with ``new_status`` — the caller is
        expected to construct the new domain entity via
        ``CaseTypeVersion.publish()`` / ``.retire()`` first, then call
        this helper with the resulting values. The DB status check here
        is the second-line guard against a stale read.
        """
        stmt = select(CaseTypeVersionRow).where(
            CaseTypeVersionRow.tenant_id == tenant_id,
            CaseTypeVersionRow.id == version_id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise NotFound(f"case type version {version_id} not found")
        current = CaseTypeVersionStatus(row.status)
        _assert_valid_transition(current, new_status)
        row.status = new_status.value
        row.published_at = (
            published_at if new_status is CaseTypeVersionStatus.PUBLISHED else row.published_at
        )
        row.published_by = (
            published_by if new_status is CaseTypeVersionStatus.PUBLISHED else row.published_by
        )


def _assert_valid_transition(current: CaseTypeVersionStatus, new: CaseTypeVersionStatus) -> None:
    """Permit only ``draft → published`` and ``published → retired``.

    Why explicit: FR-015 forbids mutation of published versions. The
    domain entity's ``publish`` / ``retire`` raise on the same logic, so
    this is a defense-in-depth check on the persistence boundary.
    """
    if current is CaseTypeVersionStatus.DRAFT and new is CaseTypeVersionStatus.PUBLISHED:
        return
    if current is CaseTypeVersionStatus.PUBLISHED and new is CaseTypeVersionStatus.RETIRED:
        return
    raise InvariantViolation(
        f"invalid case type version transition: {current.value} -> {new.value}"
    )


__all__ = [
    "SqlCaseTypeRepository",
    "SqlCaseTypeVersionRepository",
]
