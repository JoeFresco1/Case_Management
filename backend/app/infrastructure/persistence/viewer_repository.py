"""SQL adapters for the viewer pipeline (T5 / FM-023 ext).

Implements the ``AnnotationRepository``, ``CheckoutLockRepository``,
and ``RenditionRepository`` Protocols defined in
``app.domain.ports.viewer``. Tenant isolation is explicit on every
method (`WHERE tenant_id = :tenant_id`); the listener-based
defense-in-depth is never the sole mechanism.

The annotation path never writes ``documents`` / ``document_versions``:
the overlay rows plus audit + outbox share the caller's transaction,
and the original bytes stay untouched (T5 acceptance).

# INTEGRATION_SEAM

These are the local SQLAlchemy-backed adapters. An Azure SQL adapter
would implement the same Protocols and live under
``app/infrastructure/persistence/azure_sql_viewer.py``; no domain or
application code changes are required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.annotations import (
    Annotation,
    AnnotationKind,
    CheckoutLock,
    Rendition,
    RenditionKind,
)
from app.infrastructure.persistence.models_viewer import (
    DocumentCheckoutLock as CheckoutLockRow,
)
from app.infrastructure.persistence.models_viewer import (
    DocumentRendition as RenditionRow,
)
from app.infrastructure.persistence.models_viewer import (
    ViewerAnnotation as AnnotationRow,
)


def _aware(value: datetime) -> datetime:
    """Coerce a stored timestamp to tz-aware UTC.

    Why: sqlite round-trips ``DateTime(timezone=True)`` as naive while
    postgres preserves the offset. The domain checkout rule subtracts
    ``now`` (always aware) from these fields, so a naive value raises
    ``TypeError`` on sqlite only — a backend difference the adapter
    absorbs, never the domain (AGENTS.md R1: portable types, portable
    behavior). Mirrors ``mapping._normalize_dt``.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _aware_opt(value: datetime | None) -> datetime | None:
    """Nullable variant of :func:`_aware` for ``released_at``."""
    return None if value is None else _aware(value)


def _annotation_from_row(row: AnnotationRow) -> Annotation:
    """Translate an ORM row into the domain aggregate."""
    return Annotation(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        document_version=row.document_version,
        page=row.page,
        kind=AnnotationKind(row.kind),
        geometry=dict(row.geometry),
        content=row.content,
        created_by=row.created_by,
        created_at=_aware(row.created_at),
    )


def _lock_from_row(row: CheckoutLockRow) -> CheckoutLock:
    """Translate an ORM row into the domain aggregate."""
    return CheckoutLock(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        holder_user_id=row.holder_user_id,
        acquired_at=_aware(row.acquired_at),
        expires_at=_aware(row.expires_at),
        released_at=_aware_opt(row.released_at),
    )


def _rendition_from_row(row: RenditionRow) -> Rendition:
    """Translate an ORM row into the domain aggregate."""
    return Rendition(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        source_version=row.source_version,
        kind=RenditionKind(row.kind),
        sha256=row.sha256,
        size=row.size,
        storage_ref=row.storage_ref,
        content_type=row.content_type,
        redaction_id=row.redaction_id,
        created_at=_aware(row.created_at),
    )


class SqlAnnotationRepository:
    """SQL-backed ``AnnotationRepository`` (overlay CRUD, bytes untouched)."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, annotation: Annotation) -> None:
        """Stage one annotation row on the active session.

        The session is not committed here; the use case boundary
        commits once so the overlay + audit + outbox land atomically.
        """
        self._s.add(
            AnnotationRow(
                id=annotation.id,
                tenant_id=annotation.tenant_id,
                document_id=annotation.document_id,
                document_version=annotation.document_version,
                page=annotation.page,
                kind=annotation.kind.value,
                geometry=dict(annotation.geometry),
                content=annotation.content,
                created_by=annotation.created_by,
                created_at=annotation.created_at,
                updated_at=annotation.created_at,
            )
        )

    async def get(self, tenant_id: UUID, annotation_id: UUID) -> Annotation | None:
        """Fetch one annotation by id within the actor's tenant.

        ``WHERE tenant_id = :tenant_id`` is the isolation boundary; a
        cross-tenant id is treated as missing.
        """
        stmt = select(AnnotationRow).where(
            AnnotationRow.id == annotation_id,
            AnnotationRow.tenant_id == tenant_id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return None if row is None else _annotation_from_row(row)

    async def list_for_document(
        self,
        tenant_id: UUID,
        document_id: UUID,
        *,
        document_version: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Annotation]:
        """Return overlay rows for a document, oldest first."""
        stmt = (
            select(AnnotationRow)
            .where(
                AnnotationRow.tenant_id == tenant_id,
                AnnotationRow.document_id == document_id,
                *(
                    [AnnotationRow.document_version == document_version]
                    if document_version is not None
                    else []
                ),
            )
            .order_by(AnnotationRow.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_annotation_from_row(r) for r in rows]

    async def save(self, annotation: Annotation) -> None:
        """Persist overlay edits (geometry/content) on an existing row.

        Only the overlay columns change; the pinned identity and the
        provenance columns are immutable on this path.
        """
        stmt = (
            update(AnnotationRow)
            .where(
                AnnotationRow.id == annotation.id,
                AnnotationRow.tenant_id == annotation.tenant_id,
            )
            .values(
                geometry=dict(annotation.geometry),
                content=annotation.content,
            )
        )
        await self._s.execute(stmt)

    async def delete(self, tenant_id: UUID, annotation_id: UUID) -> bool:
        """Delete one annotation row; ``True`` iff a row was removed."""
        stmt = (
            delete(AnnotationRow)
            .where(
                AnnotationRow.id == annotation_id,
                AnnotationRow.tenant_id == tenant_id,
            )
            .returning(AnnotationRow.id)
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none() is not None


class SqlCheckoutLockRepository:
    """SQL-backed ``CheckoutLockRepository`` (acquire/renew/release/history)."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, lock: CheckoutLock) -> None:
        """Stage a fresh checkout row (acquire or post-expiry takeover)."""
        self._s.add(
            CheckoutLockRow(
                id=lock.id,
                tenant_id=lock.tenant_id,
                document_id=lock.document_id,
                holder_user_id=lock.holder_user_id,
                acquired_at=lock.acquired_at,
                expires_at=lock.expires_at,
                released_at=None,
                created_at=lock.acquired_at,
                updated_at=lock.acquired_at,
            )
        )

    async def get_active(self, tenant_id: UUID, document_id: UUID) -> CheckoutLock | None:
        """Return the latest unreleased checkout row for a document.

        Liveness (expiry) is evaluated by the caller with the Clock
        port — the repository never reads the clock (AGENTS.md R10).
        """
        stmt = (
            select(CheckoutLockRow)
            .where(
                CheckoutLockRow.tenant_id == tenant_id,
                CheckoutLockRow.document_id == document_id,
                CheckoutLockRow.released_at.is_(None),
            )
            .order_by(CheckoutLockRow.acquired_at.desc())
            .limit(1)
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return None if row is None else _lock_from_row(row)

    async def save(self, lock: CheckoutLock) -> None:
        """Persist a renew (``expires_at``) or release (``released_at``)."""
        stmt = (
            update(CheckoutLockRow)
            .where(
                CheckoutLockRow.id == lock.id,
                CheckoutLockRow.tenant_id == lock.tenant_id,
            )
            .values(
                expires_at=lock.expires_at,
                released_at=lock.released_at,
            )
        )
        await self._s.execute(stmt)

    async def list_history(
        self,
        tenant_id: UUID,
        document_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CheckoutLock]:
        """Return the checkout trail for a document, newest first."""
        stmt = (
            select(CheckoutLockRow)
            .where(
                CheckoutLockRow.tenant_id == tenant_id,
                CheckoutLockRow.document_id == document_id,
            )
            .order_by(CheckoutLockRow.acquired_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_lock_from_row(r) for r in rows]


class SqlRenditionRepository:
    """SQL-backed ``RenditionRepository`` (burned rendition rows)."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, rendition: Rendition) -> None:
        """Stage one burned rendition row on the active session.

        The bytes were already stored via ``DocumentStore.put`` by the
        caller, so the row always points at durable content.
        """
        self._s.add(
            RenditionRow(
                id=rendition.id,
                tenant_id=rendition.tenant_id,
                document_id=rendition.document_id,
                source_version=rendition.source_version,
                kind=rendition.kind.value,
                sha256=rendition.sha256,
                size=rendition.size,
                storage_ref=rendition.storage_ref,
                content_type=rendition.content_type,
                redaction_id=rendition.redaction_id,
                created_at=rendition.created_at,
                updated_at=rendition.created_at,
            )
        )

    async def list_for_document(
        self,
        tenant_id: UUID,
        document_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Rendition]:
        """Return rendition rows for a document, oldest first."""
        stmt = (
            select(RenditionRow)
            .where(
                RenditionRow.tenant_id == tenant_id,
                RenditionRow.document_id == document_id,
            )
            .order_by(RenditionRow.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_rendition_from_row(r) for r in rows]


__all__ = [
    "SqlAnnotationRepository",
    "SqlCheckoutLockRepository",
    "SqlRenditionRepository",
]
