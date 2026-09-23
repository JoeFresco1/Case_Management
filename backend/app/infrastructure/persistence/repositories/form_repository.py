"""FormVersion, FormBinding, and FormSession SQL repositories (P07 / FM-036, FM-038).

Three SQL adapters live in this module:

- ``SqlFormVersionRepository`` — the versioned form definition with the
  ``draft → published → retired`` lifecycle.
- ``SqlFormBindingRepository`` — the immutable link between a
  ``FormVersion`` and a ``CaseTypeVersion``.
- ``SqlFormSessionRepository`` — the durable multi-step progress record
  (P07-US6 / FM-038). Compare-and-swap on ``version_token`` so
  concurrent step saves conflict instead of losing updates.

All adapters enforce tenant isolation explicitly: every method takes
``tenant_id`` and every SQL query carries ``WHERE tenant_id = :tenant_id``.
A buggy listener that forgets the filter still cannot leak cross-tenant
data; the test suite asserts this on every repository.

# Lifecycle enforcement

A published ``FormVersion`` is **immutable**. There is no UPDATE path
through these repositories on a non-draft version; the
``update_draft``/``publish``/``retire`` helpers check the row's status
before issuing the SQL. Database-level triggers (FM-003 hardening) and
ORM listeners arrive in a later phase; this module is the primary
mechanism.

A ``FormBinding`` has no explicit status enum — ``published_at`` is
None for a draft binding, non-null for a published binding. Publishing
a binding flips ``published_at`` / ``published_by`` to non-null values
and is irreversible.

# INTEGRATION_SEAM

Both classes are local adapters. Azure SQL implementations
(``SqlFormVersionRepositoryAzure`` etc.) plug into the same Protocols in
``app/domain/ports.py`` and are wired by ``app/deps`` based on settings.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.forms.advanced_execution import FormSession
from app.domain.errors import Conflict, InvariantViolation, NotFound
from app.domain.form import (
    FormBinding,
    FormVersion,
    FormVersionStatus,
)
from app.infrastructure.persistence.form_mapping import (
    form_binding_from_domain,
    form_binding_to_domain,
    form_version_from_domain,
    form_version_to_domain,
)
from app.infrastructure.persistence.models import (
    FormBinding as FormBindingRow,
)
from app.infrastructure.persistence.models import (
    FormSession as FormSessionRow,
)
from app.infrastructure.persistence.models import (
    FormVersion as FormVersionRow,
)


def _now_utc() -> datetime:
    """Return a tz-aware UTC ``datetime`` for ``created_at``/``updated_at``."""
    return datetime.now(tz=UTC)


# ----- FormVersion -------------------------------------------------------------


class SqlFormVersionRepository:
    """Repository for the versioned Form definition.

    Provides:

    - ``add`` / ``get`` / ``get_by_id`` / ``get_latest_published_for_key``
      — read/write primitives used by other modules.
    - ``list_published`` — list all published form versions for a tenant.
    - ``update_draft`` — edit a draft before publication. Raises on any
      non-draft version.
    - ``mark_status`` — transition ``draft → published`` or
      ``published → retired``. Raises on an invalid transition.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, tenant_id: UUID, form_version_id: UUID) -> FormVersion | None:
        stmt = select(FormVersionRow).where(
            FormVersionRow.tenant_id == tenant_id,
            FormVersionRow.id == form_version_id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return form_version_to_domain(row) if row else None

    async def get_by_id(self, tenant_id: UUID, form_version_id: UUID) -> FormVersion | None:
        return await self.get(tenant_id, form_version_id)

    async def get_latest_published_for_key(
        self, tenant_id: UUID, form_key: str
    ) -> FormVersion | None:
        """Return the latest published form version for a form key."""
        stmt = (
            select(FormVersionRow)
            .where(
                FormVersionRow.tenant_id == tenant_id,
                FormVersionRow.key == form_key,
                FormVersionRow.status == FormVersionStatus.PUBLISHED.value,
            )
            .order_by(FormVersionRow.version_number.desc())
            .limit(1)
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return form_version_to_domain(row) if row else None

    async def list_published(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[FormVersion]:
        """Return published form versions for a tenant, newest first."""
        stmt = (
            select(FormVersionRow)
            .where(
                FormVersionRow.tenant_id == tenant_id,
                FormVersionRow.status == FormVersionStatus.PUBLISHED.value,
            )
            .order_by(FormVersionRow.version_number.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [form_version_to_domain(r) for r in rows]

    async def add(self, version: FormVersion) -> None:
        created_at = version.created_at or _now_utc()
        self._s.add(FormVersionRow(**form_version_from_domain(version, created_at=created_at)))

    async def update_draft(self, version: FormVersion) -> None:
        """Persist schema edits to a draft version.

        Raises ``InvariantViolation`` if the version is not in ``draft``
        status — this is the primary immutability guard.
        """
        if version.status is not FormVersionStatus.DRAFT:
            raise InvariantViolation(f"cannot update a {version.status.value} version")
        stmt = select(FormVersionRow).where(
            FormVersionRow.tenant_id == version.tenant_id,
            FormVersionRow.id == version.id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise NotFound(f"form version {version.id} not found")
        if row.status != FormVersionStatus.DRAFT.value:
            raise InvariantViolation(f"form version {row.id} is {row.status}, not draft")
        payload = form_version_from_domain(version, created_at=row.created_at)
        row.definition_json = payload["definition_json"]

    async def mark_status(
        self,
        tenant_id: UUID,
        version_id: UUID,
        *,
        new_status: FormVersionStatus,
        published_at: datetime | None,
        published_by: UUID | None,
    ) -> None:
        """Transition a version's status atomically.

        Used by the publish and retire use cases. Raises if the current
        row status is incompatible with ``new_status``.
        """
        stmt = select(FormVersionRow).where(
            FormVersionRow.tenant_id == tenant_id,
            FormVersionRow.id == version_id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise NotFound(f"form version {version_id} not found")
        current = FormVersionStatus(row.status)
        _assert_valid_form_version_transition(current, new_status)
        row.status = new_status.value
        if new_status is FormVersionStatus.PUBLISHED:
            row.published_at = published_at
            row.published_by = published_by
        elif new_status is FormVersionStatus.RETIRED:
            # Retired versions keep their published_at / published_by
            pass


# ----- FormBinding -------------------------------------------------------------


class SqlFormBindingRepository:
    """Repository for the immutable FormBinding link.

    A binding has no explicit status enum; ``published_at`` is the
    lifecycle marker. Draft bindings have ``published_at = None``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_published_for_key(self, tenant_id: UUID, form_key: str) -> FormBinding | None:
        """Return the published binding for a form key.

        Joins through FormVersion to filter by the form key. Only returns
        bindings with ``published_at IS NOT NULL``.
        """
        stmt = (
            select(FormBindingRow)
            .join(FormVersionRow, FormVersionRow.id == FormBindingRow.form_version_id)
            .where(
                FormBindingRow.tenant_id == tenant_id,
                FormVersionRow.key == form_key,
                FormBindingRow.published_at.is_not(None),
            )
            .order_by(FormBindingRow.published_at.desc())
            .limit(1)
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return form_binding_to_domain(row) if row else None

    async def list_published_for_form_version(
        self,
        tenant_id: UUID,
        form_version_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[FormBinding]:
        """Return published bindings for a form version."""
        stmt = (
            select(FormBindingRow)
            .where(
                FormBindingRow.tenant_id == tenant_id,
                FormBindingRow.form_version_id == form_version_id,
                FormBindingRow.published_at.is_not(None),
            )
            .order_by(FormBindingRow.published_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [form_binding_to_domain(r) for r in rows]

    async def add(self, binding: FormBinding) -> None:
        self._s.add(FormBindingRow(**form_binding_from_domain(binding)))

    async def get_by_id(self, tenant_id: UUID, binding_id: UUID) -> FormBinding | None:
        """Return one binding by id (any status), scoped to tenant."""
        stmt = select(FormBindingRow).where(
            FormBindingRow.tenant_id == tenant_id,
            FormBindingRow.id == binding_id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return form_binding_to_domain(row) if row else None

    async def list_for_form_version(
        self,
        tenant_id: UUID,
        form_version_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[FormBinding]:
        """List all bindings (any status) for a form version."""
        stmt = (
            select(FormBindingRow)
            .where(
                FormBindingRow.tenant_id == tenant_id,
                FormBindingRow.form_version_id == form_version_id,
            )
            .order_by(FormBindingRow.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [form_binding_to_domain(r) for r in rows]

    async def mark_published(
        self,
        tenant_id: UUID,
        binding_id: UUID,
        *,
        published_at: datetime,
        published_by: UUID,
    ) -> None:
        """Mark a binding as published. Irreversible."""
        stmt = select(FormBindingRow).where(
            FormBindingRow.tenant_id == tenant_id,
            FormBindingRow.id == binding_id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise NotFound(f"form binding {binding_id} not found")
        if row.published_at is not None:
            raise InvariantViolation("binding already published")
        row.published_at = published_at
        row.published_by = published_by

    async def mark_retired(
        self,
        tenant_id: UUID,
        binding_id: UUID,
        *,
        retired_at: datetime,
        retired_by: UUID,
    ) -> None:
        """Transition a published binding to ``retired``. Irreversible.

        Raises :class:`InvariantViolation` if the binding is not yet
        published or is already retired.
        """
        stmt = select(FormBindingRow).where(
            FormBindingRow.tenant_id == tenant_id,
            FormBindingRow.id == binding_id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise NotFound(f"form binding {binding_id} not found")
        if row.published_at is None:
            raise InvariantViolation("cannot retire an unpublished binding")
        if row.retired_at is not None:
            raise InvariantViolation("binding already retired")
        row.retired_at = retired_at
        row.retired_by = retired_by


def _assert_valid_form_version_transition(
    current: FormVersionStatus, new: FormVersionStatus
) -> None:
    """Permit only ``draft → published`` and ``published → retired``."""
    if current is FormVersionStatus.DRAFT and new is FormVersionStatus.PUBLISHED:
        return
    if current is FormVersionStatus.PUBLISHED and new is FormVersionStatus.RETIRED:
        return
    raise InvariantViolation(f"invalid form version transition: {current.value} -> {new.value}")


# ----- FormSession -----------------------------------------------------------


def _form_session_to_domain(row: FormSessionRow) -> FormSession:
    """Reconstruct the immutable session aggregate from its ORM row."""
    return FormSession(
        id=row.id,
        tenant_id=row.tenant_id,
        case_id=row.case_id,
        form_version_id=row.form_version_id,
        actor_id=row.actor_id,
        current_step_index=row.current_step_index,
        completed_steps=frozenset(row.completed_steps or []),
        step_data=dict(row.step_data or {}),
        status=row.status,
        version_token=row.version_token,
        created_at=row.created_at,
        updated_at=row.updated_at,
        submitted_at=row.submitted_at,
    )


class SqlFormSessionRepository:
    """Durable multi-step form progress (P07-US6 / FM-038).

    Implements the ``FormSessionRepository`` Protocol from
    ``app.application.forms.advanced_execution``. ``update`` enforces
    compare-and-swap on ``version_token`` so a stale ``If-Match``
    surfaces as ``Conflict`` (HTTP 409) instead of silently losing a
    concurrent step save.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, tenant_id: UUID, session_id: UUID) -> FormSession | None:
        """Fetch one session; ``None`` for missing/cross-tenant ids."""
        row = (
            await self._s.execute(
                select(FormSessionRow).where(
                    FormSessionRow.tenant_id == tenant_id,
                    FormSessionRow.id == session_id,
                )
            )
        ).scalar_one_or_none()
        return _form_session_to_domain(row) if row else None

    async def get_open_for_case_and_form(
        self, tenant_id: UUID, case_id: UUID, form_version_id: UUID, actor_id: UUID
    ) -> FormSession | None:
        """Return the actor's open session for the triple, if any.

        Only ``in_progress`` rows resume — ``submitted`` rows are
        terminal (reopening is a new session) and ``draft`` rows are
        not yet started.
        """
        row = (
            await self._s.execute(
                select(FormSessionRow)
                .where(
                    FormSessionRow.tenant_id == tenant_id,
                    FormSessionRow.case_id == case_id,
                    FormSessionRow.form_version_id == form_version_id,
                    FormSessionRow.actor_id == actor_id,
                    FormSessionRow.status == "in_progress",
                )
                .order_by(FormSessionRow.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return _form_session_to_domain(row) if row else None

    async def add(self, session: FormSession) -> None:
        """Stage a new session in the caller's transaction."""
        created = session.created_at or _now_utc()
        self._s.add(
            FormSessionRow(
                id=session.id,
                tenant_id=session.tenant_id,
                case_id=session.case_id,
                form_version_id=session.form_version_id,
                actor_id=session.actor_id,
                current_step_index=session.current_step_index,
                completed_steps=sorted(session.completed_steps),
                step_data=dict(session.step_data),
                status=session.status,
                version_token=session.version_token,
                created_at=created,
                updated_at=session.updated_at or created,
                submitted_at=session.submitted_at,
            )
        )
        await self._s.flush()

    async def update(self, session: FormSession) -> None:
        """Persist step progress with compare-and-swap.

        Raises:
            Conflict: when the stored ``version_token`` differs from
                ``session.version_token - 1`` (a concurrent save won).
        """
        stmt = (
            sql_update(FormSessionRow)
            .where(
                FormSessionRow.tenant_id == session.tenant_id,
                FormSessionRow.id == session.id,
                FormSessionRow.version_token == session.version_token - 1,
            )
            .values(
                current_step_index=session.current_step_index,
                completed_steps=sorted(session.completed_steps),
                step_data=dict(session.step_data),
                status=session.status,
                version_token=session.version_token,
                updated_at=session.updated_at,
                submitted_at=session.submitted_at,
            )
        )
        result = await self._s.execute(stmt)
        if not isinstance(result, CursorResult) or result.rowcount == 0:
            raise Conflict(f"stale concurrency token for form session {session.id}")


__all__ = [
    "SqlFormVersionRepository",
    "SqlFormBindingRepository",
    "SqlFormSessionRepository",
]
