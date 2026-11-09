"""Case aggregate SQL repository.

The Case aggregate is the primary domain object (Constitution I). This
adapter implements the ``CaseRepository`` protocol with explicit tenant
isolation on every query (FM-002 / P11-US2).

Monotonic reference issuance is per-tenant (FR-001, plan §16.3,
AGENTS.md §16.3 invariants). The implementation is portable: a single
``SELECT ... ORDER BY reference DESC LIMIT 1`` over the per-tenant
unique index, parsed with a deterministic prefix. SQLite (tests) and
PostgreSQL (local) both honor the unique index.

# INTEGRATION_SEAM: relational DB

This is the local adapter. An ``AzureSqlCaseRepository`` implementing the
same protocol would replace this module's body and live behind the
``CaseRepository`` port defined in ``app/domain/ports.py``.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.case import Case, CaseReference
from app.domain.errors import Conflict, DependencyUnavailable, InvariantViolation
from app.domain.reference_format import (
    PLATFORM_DEFAULT_TEMPLATE,
    ReferenceFormat,
    validate_template,
)
from app.infrastructure.persistence.mapping import (
    case_from_domain,
    case_to_domain,
)
from app.infrastructure.persistence.models import Case as CaseRow
from app.infrastructure.persistence.models import ReferenceFormat as ReferenceFormatRow
from app.infrastructure.persistence.models import Task as TaskRow
from app.infrastructure.persistence.models import Tenant as TenantRow

# Template token -> regex fragment used to recover the highest issued
# sequence from existing references. ``{SEQ}`` (any pad width) is the
# single capturing group; every other token is non-capturing. A template
# with two ``{SEQ}`` tokens captures twice — the max across groups wins.
_SEQ_CAPTURE = r"(\d{4,12})"
_TOKEN_REGEX: dict[str, str] = {
    "TENANT": r"[A-Z0-9-]{1,16}",
    "TYPE": r"[A-Z0-9_]{1,64}",
    "YYYY": r"\d{4}",
    "YY": r"\d{2}",
}
_TOKEN_PATTERN = re.compile(r"\{(SEQ(?::\d{1,2})?|[A-Z_]+)\}")

# Bounded collision-retry: after rendering, the candidate is checked for
# existence (a concurrent intake may have won the race). Each retry bumps
# the sequence; exhausting the budget surfaces ``Conflict`` instead of
# looping forever.
_MAX_SEQ_PROBE_ATTEMPTS = 1000


class SqlCaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, case: Case) -> None:
        self._s.add(CaseRow(**case_from_domain(case)))

    async def update(self, case: Case) -> None:
        """Persist a mutated case (P01-US3 / FR-003).

        Optimistic concurrency: the UPDATE is gated on the row's
        current ``version_token`` matching ``case.version_token - 1``
        (the token the caller observed in the prior GET). If the row's
        stored token differs, the UPDATE matches zero rows and we
        raise :class:`Conflict` so the application layer can surface
        the 409 with the current token in the body.

        Why a compare-and-swap UPDATE rather than a select-then-update:
        the two-statement form has a TOCTOU race the database-level
        WHERE clause eliminates. The application layer never silently
        overwrites a concurrent editor's write (Constitution V).
        """
        stmt = (
            sql_update(CaseRow)
            .where(
                CaseRow.tenant_id == case.tenant_id,
                CaseRow.id == case.id,
                CaseRow.version_token == case.version_token - 1,
            )
            .values(
                # why: ``case_from_domain`` already carries the new
                # ``version_token``; passing it again as an explicit
                # keyword raises ``TypeError: got multiple values`` and
                # breaks every SQL case update (submit, edit, session
                # commit). The CAS gate lives in the WHERE clause above.
                **case_from_domain(case),
            )
        )
        result = await self._s.execute(stmt)
        if result.rowcount == 0:
            raise Conflict(f"stale concurrency token for case {case.id}")

    async def get(self, tenant_id: UUID, case_id: UUID) -> Case | None:
        stmt = select(CaseRow).where(CaseRow.tenant_id == tenant_id, CaseRow.id == case_id)
        result = (await self._s.execute(stmt)).scalar_one_or_none()
        return case_to_domain(result) if result else None

    async def get_by_reference(self, tenant_id: UUID, reference: CaseReference) -> Case | None:
        stmt = select(CaseRow).where(
            CaseRow.tenant_id == tenant_id, CaseRow.reference == reference.value
        )
        result = (await self._s.execute(stmt)).scalar_one_or_none()
        return case_to_domain(result) if result else None

    async def list(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Case]:
        stmt = (
            select(CaseRow)
            .where(CaseRow.tenant_id == tenant_id)
            .order_by(CaseRow.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [case_to_domain(r) for r in rows]

    async def next_reference(
        self, tenant_id: UUID, *, now: datetime, type_key: str | None = None
    ) -> CaseReference:
        """Per-tenant monotonic reference honoring the tenant template.

        Renders the tenant's configured ``ReferenceFormat`` (FM-009)
        when one exists, else the platform default
        (``C-{TENANT}-{YYYY}-{SEQ}``). The sequence is one past the
        highest sequence recovered from the tenant's existing
        references (matched against a regex derived from the active
        template, so a template change starts its own counter without
        re-numbering history). A final existence check with bounded
        retry covers the concurrent-intake race; the unique index on
        ``(tenant_id, reference)`` remains the integrity guarantee.

        Why a full reference scan instead of a prefix-ordered max: the
        sequence token may sit anywhere in a custom template
        (``PR-{SEQ}-{YY}``), so no single ordered-prefix query covers
        every shape. Tenant case counts are small at this scale; a
        counter table lands if this ever shows up in profiles.
        """
        tenant = (
            await self._s.execute(select(TenantRow).where(TenantRow.id == tenant_id))
        ).scalar_one_or_none()
        if tenant is None:
            raise DependencyUnavailable(f"tenant {tenant_id} not found")
        template = await self._template_for_tenant(tenant_id)
        if "{TYPE}" in template and not type_key:
            raise InvariantViolation(
                "the tenant's reference template requires {TYPE} but no case type key was supplied"
            )
        seq = await self._highest_issued_sequence(tenant_id, template) + 1
        for _ in range(_MAX_SEQ_PROBE_ATTEMPTS):
            rendered = ReferenceFormat(tenant_id=tenant_id, template=template).render(
                tenant_short=tenant.key,
                # CaseType keys are stable business slugs (for example,
                # ServiceRequest or review-case); reference tokens require
                # uppercase ASCII with underscores. Normalize at this port
                # so seed, intake, split, forms, and imports agree.
                type_key=(type_key or "UNKNOWN").upper().replace("-", "_"),
                now=now,
                sequence=seq,
            )
            exists = (
                await self._s.execute(
                    select(CaseRow.id).where(
                        CaseRow.tenant_id == tenant_id,
                        CaseRow.reference == rendered,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                return CaseReference(rendered)
            seq += 1
        raise Conflict(f"could not issue a unique reference for tenant {tenant_id}")

    async def _template_for_tenant(self, tenant_id: UUID) -> str:
        """Return the tenant's configured template or the platform default."""
        row = (
            await self._s.execute(
                select(ReferenceFormatRow).where(ReferenceFormatRow.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return PLATFORM_DEFAULT_TEMPLATE
        # Belt-and-suspenders: the admin boundary validates on write,
        # but a corrupt row must not emit malformed references silently.
        validate_template(row.template)
        return row.template

    async def _highest_issued_sequence(self, tenant_id: UUID, template: str) -> int:
        """Recover the highest sequence issued under ``template``.

        Derives a full-match regex from the template (``{SEQ}`` is the
        capturing group) and scans the tenant's existing references.
        References issued under a *different* template simply do not
        match, so each template shape owns an independent counter and
        history is never re-numbered.
        """
        pattern = self._template_to_regex(template)
        rows = (
            (await self._s.execute(select(CaseRow.reference).where(CaseRow.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        highest = 0
        for reference in rows:
            match = pattern.fullmatch(reference)
            if match is None:
                continue
            for group in match.groups():
                if group is None:
                    continue
                with contextlib.suppress(ValueError):
                    highest = max(highest, int(group))
        return highest

    @staticmethod
    def _template_to_regex(template: str) -> re.Pattern[str]:
        """Compile a template into a full-match reference regex."""
        parts: list[str] = []
        pos = 0
        for match in _TOKEN_PATTERN.finditer(template):
            parts.append(re.escape(template[pos : match.start()]))
            name = match.group(1)
            if name == "SEQ" or name.startswith("SEQ:"):
                parts.append(_SEQ_CAPTURE)
            else:
                parts.append(_TOKEN_REGEX[name])
            pos = match.end()
        parts.append(re.escape(template[pos:]))
        return re.compile("".join(parts))

    async def list_open_for_queues(
        self,
        tenant_id: UUID,
        *,
        queue_keys: tuple[str, ...],
        limit: int = 200,
    ) -> Sequence[Case]:
        """Return the freshest open cases with open work in these tenant queues.

        An empty queue set returns no rows. Multiple matching tasks still represent
        one case; the correlated EXISTS applies membership before the result limit.
        """
        if not queue_keys:
            return []
        assigned_task = (
            select(TaskRow.id)
            .where(
                TaskRow.tenant_id == tenant_id,
                TaskRow.case_id == CaseRow.id,
                TaskRow.status == "open",
                TaskRow.assignee_queue_id.in_(queue_keys),
            )
            .exists()
        )
        stmt = (
            select(CaseRow)
            .where(CaseRow.tenant_id == tenant_id, CaseRow.status == "open", assigned_task)
            .order_by(CaseRow.updated_at.desc(), CaseRow.id)
            .limit(limit)
        )
        return [case_to_domain(row) for row in (await self._s.scalars(stmt)).all()]


__all__ = ["SqlCaseRepository"]
