"""SQL coverage for tenant-configurable reference formats (FM-009 / P01-US9).

Exercises the production adapters end-to-end against SQLite:
``SqlReferenceFormatRepository`` (configure / read / reset) plus
``SqlCaseRepository.next_reference`` rendering the active template.

Acceptance mapping (Pillar P01 §6 Q1 + domain docstring):

- Admin sets ``C-{TYPE}-{YYYY}-{SEQ}`` → a *new* case uses that shape.
- An *existing* case keeps its original reference (no re-numbering).
- No override → platform default (``C-{TENANT}-{YYYY}-{SEQ}``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.references import configure_references
from app.deps import UserContext
from app.domain.case import Case, CaseReference, CaseStatus
from app.domain.errors import InvariantViolation
from app.domain.reference_format import ReferenceFormat
from app.infrastructure.persistence.models import Tenant as TenantRow
from app.infrastructure.persistence.repositories import (
    SqlCaseRepository,
    SqlReferenceFormatRepository,
)

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ADMIN_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _run(coro: Any) -> Any:
    """Run an async seeding step from a sync test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    key: str,
) -> None:
    """Insert the tenant row ``next_reference`` resolves the short key from."""
    async with session_factory() as session:
        session.add(TenantRow(id=tenant_id, key=key, name=key, created_at=NOW))
        await session.commit()


def _admin() -> UserContext:
    return UserContext(
        user_id=ADMIN_ID,
        tenant_id=TENANT_A,
        display_name="Synthetic Admin",
        roles=("process_admin",),
    )


def _persist_case(
    session_factory: async_sessionmaker[AsyncSession],
    reference: CaseReference,
) -> None:
    """Stage a case row carrying ``reference`` (history simulation)."""

    async def _add() -> None:
        async with session_factory() as session:
            case = Case(
                id=uuid4(),
                tenant_id=TENANT_A,
                reference=reference,
                case_type_version_id=uuid4(),
                status=CaseStatus.OPEN,
                priority="normal",
                title="Synthetic",
                description="",
                data={},
                current_workflow_state=None,
                version_token=1,
                created_at=NOW,
                updated_at=NOW,
                created_by=ADMIN_ID,
            )
            await SqlCaseRepository(session).add(case)
            await session.commit()

    _run(_add())


def test_next_reference__default_template__matches_legacy_shape(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No override → ``C-{TENANT}-{YYYY}-{SEQ:06d}`` (first-slice parity)."""

    async def _issue() -> str:
        async with sql_session_factory() as session:
            return (await SqlCaseRepository(session).next_reference(TENANT_A, now=NOW)).value

    _run(_seed_tenant(sql_session_factory, TENANT_A, "ACME"))
    assert _run(_issue()) == "C-ACME-2026-000001"
    assert _run(_issue()) == "C-ACME-2026-000001"  # no row staged → same candidate


def test_next_reference__custom_template__new_cases_use_it_history_kept(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FM-009 acceptance: new cases render the template; history is kept."""
    _run(_seed_tenant(sql_session_factory, TENANT_A, "ACME"))

    async def _configure() -> None:
        async with sql_session_factory() as session:
            await configure_references.configure_reference_format(
                configure_references.ConfigureReferenceFormatCommand(
                    tenant_id=TENANT_A,
                    actor=_admin(),
                    correlation_id="cid-1",
                    template="C-{TYPE}-{YYYY}-{SEQ}",
                ),
                repo=SqlReferenceFormatRepository(session),
                audit=_NoOpAudit(),
                clock=_FixedClock(),
            )
            await session.commit()

    _run(_configure())

    async def _issue() -> str:
        async with sql_session_factory() as session:
            ref = await SqlCaseRepository(session).next_reference(
                TENANT_A, now=NOW, type_key="SERVICEREQUEST"
            )
            return ref.value

    # given a legacy case issued before the format change
    _persist_case(sql_session_factory, CaseReference("C-ACME-2026-000001"))

    # when a new case is issued after the change
    first = _run(_issue())

    # then the new reference uses the template shape with the next
    # per-tenant sequence (the counter is per tenant, not per template:
    # the legacy ``...-000001`` parses under the new shape, so the new
    # case takes ``...-000002``), and the legacy reference still
    # resolves untouched.
    assert first == "C-SERVICEREQUEST-2026-000002", first

    async def _resolve() -> Case | None:
        async with sql_session_factory() as session:
            return await SqlCaseRepository(session).get_by_reference(
                TENANT_A, CaseReference("C-ACME-2026-000001")
            )

    assert _run(_resolve()) is not None


def test_next_reference__type_template_without_key__fails_closed(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A ``{TYPE}`` template with no type key raises, never half-renders."""
    _run(_seed_tenant(sql_session_factory, TENANT_A, "ACME"))

    async def _configure_and_issue() -> None:
        async with sql_session_factory() as session:
            repo = SqlReferenceFormatRepository(session)
            await repo.upsert(
                ReferenceFormat(tenant_id=TENANT_A, template="C-{TYPE}-{SEQ}"),
                now=NOW,
            )
            await session.commit()
        async with sql_session_factory() as session:
            await SqlCaseRepository(session).next_reference(TENANT_A, now=NOW)

    with pytest.raises(InvariantViolation, match="TYPE"):
        _run(_configure_and_issue())


def test_next_reference__template_is_tenant_scoped(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Tenant B keeps the default while tenant A customizes."""
    _run(_seed_tenant(sql_session_factory, TENANT_A, "ACME"))
    _run(_seed_tenant(sql_session_factory, TENANT_B, "BETA"))

    async def _configure_a() -> None:
        async with sql_session_factory() as session:
            await SqlReferenceFormatRepository(session).upsert(
                ReferenceFormat(tenant_id=TENANT_A, template="PR-{SEQ}"),
                now=NOW,
            )
            await session.commit()

    _run(_configure_a())

    async def _issue(tenant_id: UUID) -> str:
        async with sql_session_factory() as session:
            return (await SqlCaseRepository(session).next_reference(tenant_id, now=NOW)).value

    assert _run(_issue(TENANT_A)) == "PR-000001"
    assert _run(_issue(TENANT_B)) == "C-BETA-2026-000001"


def test_reset__restores_platform_default(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reset writes the default back; issuance matches the legacy shape."""
    _run(_seed_tenant(sql_session_factory, TENANT_A, "ACME"))

    async def _configure_then_reset() -> str:
        async with sql_session_factory() as session:
            repo = SqlReferenceFormatRepository(session)
            await configure_references.configure_reference_format(
                configure_references.ConfigureReferenceFormatCommand(
                    tenant_id=TENANT_A,
                    actor=_admin(),
                    correlation_id="cid-1",
                    template="PR-{SEQ}",
                ),
                repo=repo,
                audit=_NoOpAudit(),
                clock=_FixedClock(),
            )
            result = await configure_references.reset_reference_format(
                configure_references.ResetReferenceFormatCommand(
                    tenant_id=TENANT_A,
                    actor=_admin(),
                    correlation_id="cid-2",
                ),
                repo=repo,
                audit=_NoOpAudit(),
                clock=_FixedClock(),
            )
            await session.commit()
            return result.template

    template = _run(_configure_then_reset())
    assert template == "C-{TENANT}-{YYYY}-{SEQ}"

    async def _issue() -> str:
        async with sql_session_factory() as session:
            return (await SqlCaseRepository(session).next_reference(TENANT_A, now=NOW)).value

    assert _run(_issue()) == "C-ACME-2026-000001"


class _NoOpAudit:
    """Test-only audit sink: issuance paths are asserted, not audit rows."""

    async def append(self, **kwargs: object) -> None:
        return None


class _FixedClock:
    """Deterministic clock pinned to the module ``NOW``."""

    def now(self) -> datetime:
        return NOW
