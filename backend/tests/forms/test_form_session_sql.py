"""SQL coverage for form sessions + step persistence (FM-038 / P07-US6).

Proves two things the fake-based suite cannot:

- Explicit multi-step declarations survive the
  ``FormVersion`` SQL round-trip (the ``steps`` array is part of
  ``definition_json``).
- ``SqlFormSessionRepository`` enforces tenant isolation and
  compare-and-swap on ``version_token`` (stale saves conflict).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.forms.advanced_execution import FormSession
from app.domain.errors import Conflict, InvariantViolation
from app.domain.form import (
    FormDefinition,
    FormFieldSpec,
    FormSectionSpec,
    FormVersion,
)
from app.infrastructure.persistence.repositories.form_repository import (
    SqlFormSessionRepository,
    SqlFormVersionRepository,
)

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ACTOR_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _run(coro: Any) -> Any:
    """Run an async step from a sync test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _two_step_version(tenant_id: UUID) -> FormVersion:
    """A published two-step form version."""
    definition = FormDefinition(
        sections=(
            FormSectionSpec(
                key="basics",
                label="Basics",
                description="Initial details",
                fields=(
                    FormFieldSpec(
                        key="title",
                        label="Title",
                        required=True,
                        tab_order=1,
                        min_length=2,
                        max_length=200,
                        validation_message="Provide a title",
                    ),
                ),
            ),
            FormSectionSpec(
                key="review",
                label="Review",
                fields=(
                    FormFieldSpec(
                        key="decision",
                        label="Decision",
                        required=True,
                        field_type="enum",
                        options=("approve", "reject"),
                        tab_order=2,
                    ),
                    FormFieldSpec(
                        key="signature",
                        label="Signature",
                        field_type="signature",
                        signature_kind="external",
                        signature_connector="test-signature",
                        tab_order=3,
                    ),
                ),
            ),
        ),
        validations=(),
        steps=(
            {"step_key": "basics", "label": "Basics", "section_keys": ["basics"]},
            {"step_key": "review", "label": "Review", "section_keys": ["review"]},
        ),
    )
    return FormVersion.draft(
        tenant_id=tenant_id,
        key="MultiStep",
        version_number=1,
        definition=definition,
        now=NOW,
    ).publish(actor_id=ACTOR_ID, at=NOW)


def _session(
    tenant_id: UUID, case_id: UUID, form_version_id: UUID, **overrides: Any
) -> FormSession:
    """A fresh in-progress session with test defaults."""
    base: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": tenant_id,
        "case_id": case_id,
        "form_version_id": form_version_id,
        "actor_id": ACTOR_ID,
        "current_step_index": 0,
        "completed_steps": frozenset(),
        "step_data": {},
        "status": "in_progress",
        "version_token": 1,
        "created_at": NOW,
        "updated_at": NOW,
        "submitted_at": None,
    }
    base.update(overrides)
    return FormSession(**base)


def test_form_version__explicit_steps__survive_sql_roundtrip(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Multi-step declarations persist (no silent single-step fallback)."""

    async def _roundtrip() -> FormVersion | None:
        async with sql_session_factory() as session:
            version = _two_step_version(TENANT_A)
            await SqlFormVersionRepository(session).add(version)
            await session.commit()
            return await SqlFormVersionRepository(session).get_by_id(TENANT_A, version.id)

    fetched = _run(_roundtrip())
    assert fetched is not None
    assert [s["step_key"] for s in fetched.definition.steps] == ["basics", "review"]
    title = fetched.definition.sections[0].fields[0]
    assert title.tab_order == 1
    assert title.validation_message == "Provide a title"
    decision = fetched.definition.sections[1].fields[0]
    assert decision.field_type == "enum"
    assert decision.options == ("approve", "reject")
    signature = fetched.definition.sections[1].fields[1]
    assert signature.signature_connector == "test-signature"
    assert fetched.definition.sections[0].description == "Initial details"


def test_form_version_repo__reads_are_tenant_scoped(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A form-version id from another tenant resolves as missing."""

    async def _roundtrip() -> tuple[UUID, FormVersion | None, FormVersion | None]:
        async with sql_session_factory() as session:
            version = _two_step_version(TENANT_A)
            repo = SqlFormVersionRepository(session)
            await repo.add(version)
            await session.commit()
            own = await repo.get_by_id(TENANT_A, version.id)
            other = await repo.get_by_id(TENANT_B, version.id)
            return version.id, own, other

    _, own, other = _run(_roundtrip())
    assert own is not None
    assert other is None


def test_form_version_repo__published_definition_is_immutable(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The SQL adapter refuses a definition update after publication."""

    async def _attempt_update() -> None:
        async with sql_session_factory() as session:
            version = _two_step_version(TENANT_A)
            repo = SqlFormVersionRepository(session)
            await repo.add(version)
            await session.commit()
            await repo.update_draft(version)

    with pytest.raises(InvariantViolation, match="cannot update a published version"):
        _run(_attempt_update())


def test_form_session_repo__crud_is_tenant_scoped(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Sessions resolve per tenant; cross-tenant ids read as missing."""
    case_id, version_id = uuid4(), uuid4()

    async def _seed() -> UUID:
        async with sql_session_factory() as session:
            repo = SqlFormSessionRepository(session)
            session_obj = _session(TENANT_A, case_id, version_id)
            await repo.add(session_obj)
            await session.commit()
            return session_obj.id

    session_id = _run(_seed())

    async def _get(tenant_id: UUID) -> FormSession | None:
        async with sql_session_factory() as session:
            return await SqlFormSessionRepository(session).get(tenant_id, session_id)

    assert _run(_get(TENANT_A)) is not None
    assert _run(_get(TENANT_B)) is None


def test_form_session_repo__open_lookup__ignores_submitted(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only ``in_progress`` rows resume; terminal rows never reopen."""
    case_id, version_id = uuid4(), uuid4()

    async def _seed() -> None:
        async with sql_session_factory() as session:
            repo = SqlFormSessionRepository(session)
            await repo.add(
                _session(TENANT_A, case_id, version_id, status="submitted", version_token=3)
            )
            await session.commit()

    _run(_seed())

    async def _lookup() -> FormSession | None:
        async with sql_session_factory() as session:
            return await SqlFormSessionRepository(session).get_open_for_case_and_form(
                TENANT_A, case_id, version_id, ACTOR_ID
            )

    assert _run(_lookup()) is None


def test_form_session_repo__stale_update__conflicts(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second write at the same token loses the CAS race (HTTP 409)."""
    case_id, version_id = uuid4(), uuid4()

    async def _seed() -> UUID:
        async with sql_session_factory() as session:
            repo = SqlFormSessionRepository(session)
            session_obj = _session(TENANT_A, case_id, version_id)
            await repo.add(session_obj)
            await session.commit()
            return session_obj.id

    session_id = _run(_seed())

    async def _update_to(token: int) -> None:
        async with sql_session_factory() as session:
            repo = SqlFormSessionRepository(session)
            stored = await repo.get(TENANT_A, session_id)
            assert stored is not None
            progressed = FormSession(
                id=stored.id,
                tenant_id=stored.tenant_id,
                case_id=stored.case_id,
                form_version_id=stored.form_version_id,
                actor_id=stored.actor_id,
                current_step_index=1,
                completed_steps=frozenset({"basics"}),
                step_data={"title": "x"},
                status="in_progress",
                version_token=token,
                created_at=stored.created_at,
                updated_at=NOW,
                submitted_at=None,
            )
            await repo.update(progressed)
            await session.commit()

    _run(_update_to(2))
    with pytest.raises(Conflict):
        _run(_update_to(2))
