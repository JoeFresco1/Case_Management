"""Migration/model parity: ORM writes against migrated databases (T024/T030/T058).

``create_all``-based tests can never catch model/migration drift: they
build the schema from the models. This file migrates a scratch sqlite
file with alembic (the real DDL, same as production runs) and then
exercises the exact ORM writes that drift broke:

- T058: document + version insert through ``SqlDocumentRepository``
  (migration 0005 used ``version`` / lacked ``content_type`` while the
  adapter speaks ``version_number`` / ``content_type``; the dead
  ``documents.uploaded_at`` column is gone).
- T024: ``idempotency_keys.expires_at`` round-trips through the model.
- T030: ``audit_events.payload_hash`` is persisted by
  ``SqlAuditEventStore`` and the 4-tuple dedupe distinguishes same
  payload (duplicate) from different payload (new event).

Hermetic like ``test_baseline_migrations.py``: alembic runs in a
subprocess against a tmp file; the ORM session opens afterward on the
same file. Async tests run under ``asyncio_mode = auto``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.audit import payload_hash
from app.domain.document import Document, DocumentSensitivity, DocumentVersion
from app.infrastructure.persistence import models as m
from app.infrastructure.persistence.audit_outbox import (
    SqlAuditEventStore,
    _make_dedupe,
)
from app.infrastructure.persistence.documents import SqlDocumentRepository

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_ROOT / "backend"

TENANT = UUID("11111111-1111-1111-1111-111111111111")
ACTOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
SHA = "a" * 64


def _migrate(db_file: Path) -> None:
    """Run ``alembic upgrade head`` against ``db_file`` in a subprocess."""
    env = os.environ.copy()
    env["APP_ENV"] = "test"
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"alembic failed:\n{proc.stdout}\n{proc.stderr}"


def _columns(db_file: Path, table: str) -> set[str]:
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        return {c["name"] for c in sa_inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def _seed_chain(session: object) -> tuple[UUID, UUID]:
    """Insert tenant/user/case-type/version/case rows; return (tenant, case)."""
    case_type_id, version_id, case_id = uuid4(), uuid4(), uuid4()
    session.add(m.Tenant(id=TENANT, key="ACME", name="ACME", created_at=NOW))
    session.add(
        m.User(
            id=ACTOR,
            tenant_id=TENANT,
            key="intake",
            display_name="Intake",
            roles=["intake"],
            is_system=False,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.add(
        m.CaseType(
            id=case_type_id,
            tenant_id=TENANT,
            key="ServiceRequest",
            name="Service Request",
            created_at=NOW,
            updated_at=NOW,
            current_version_id=version_id,
        )
    )
    session.add(
        m.CaseTypeVersion(
            id=version_id,
            tenant_id=TENANT,
            case_type_id=case_type_id,
            version_number=1,
            schema_json={"fields": []},
            status="published",
            created_at=NOW,
        )
    )
    session.add(
        m.Case(
            id=case_id,
            tenant_id=TENANT,
            reference="C-ACME-2026-000001",
            case_type_version_id=version_id,
            status="open",
            priority="normal",
            title="parity",
            created_by=ACTOR,
            version_token=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    return TENANT, case_id


def _document(case_id: UUID) -> tuple[Document, DocumentVersion]:
    doc = Document.create(
        tenant_id=TENANT,
        case_id=case_id,
        file_name="contract.pdf",
        content_type="application/pdf",
        classification="Contract",
        sensitivity=DocumentSensitivity.INTERNAL,
        sha256=SHA,
        size=12,
        storage_ref="t/" + SHA[:2] + "/" + SHA,
        uploaded_by=ACTOR,
        now=NOW,
    )
    version = DocumentVersion(
        id=uuid4(),
        tenant_id=TENANT,
        document_id=doc.id,
        version_number=1,
        sha256=SHA,
        size=12,
        storage_ref="t/" + SHA[:2] + "/" + SHA,
        content_type="application/pdf",
        uploaded_by=ACTOR,
        uploaded_at=NOW,
    )
    return doc, version


async def test_migrated_schema__matches_model_columns(tmp_path: Path) -> None:
    """The migrated DDL carries exactly the columns the ORM maps."""
    db_file = tmp_path / "parity.db"
    _migrate(db_file)

    versions = _columns(db_file, "document_versions")
    assert "version_number" in versions
    assert "content_type" in versions
    assert "version" not in versions

    documents = _columns(db_file, "documents")
    assert "uploaded_at" not in documents

    assert "payload_hash" in _columns(db_file, "audit_events")
    assert "expires_at" in _columns(db_file, "idempotency_keys")
    assert "version_token" in _columns(db_file, "queues")
    assert "template" in _columns(db_file, "reference_formats")
    assert "scope" in _columns(db_file, "break_glass_grants")
    assert "step_data" in _columns(db_file, "form_sessions")
    assert "definition_json" in _columns(db_file, "form_versions")
    assert "state_forms" in _columns(db_file, "form_bindings")
    holds = _columns(db_file, "legal_holds")
    assert "placed_by" in holds
    assert "placed_reason" in holds
    assert "released_at" in holds
    assert "is_active" in holds


async def test_migrated_db__document_roundtrip(tmp_path: Path) -> None:
    """T058: document + version rows persist through the SQL adapter."""
    db_file = tmp_path / "parity.db"
    _migrate(db_file)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    try:
        sm = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with sm() as session:
            _, case_id = _seed_chain(session)
            doc, version = _document(case_id)
            await SqlDocumentRepository(session).add_document_and_version(
                document=doc, version=version
            )
            await session.commit()
        async with sm() as session:
            fetched = await SqlDocumentRepository(session).get(TENANT, doc.id)
            assert fetched is not None
            assert fetched.current_version == 1
            assert fetched.sha256 == SHA
    finally:
        await engine.dispose()


async def test_migrated_db__idempotency_expires_at_roundtrip(tmp_path: Path) -> None:
    """T024: the model sees the migrated ``expires_at`` column."""
    db_file = tmp_path / "parity.db"
    _migrate(db_file)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    try:
        sm = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with sm() as session:
            _seed_chain(session)
            session.add(
                m.IdempotencyKey(
                    tenant_id=TENANT,
                    key="k-1",
                    request_hash="h",
                    response_status=201,
                    response_body={},
                    correlation_id="c",
                    created_at=NOW,
                    expires_at=NOW,
                )
            )
            await session.commit()
        async with sm() as session:
            row = (
                await session.execute(
                    select(m.IdempotencyKey).where(
                        m.IdempotencyKey.tenant_id == TENANT,
                        m.IdempotencyKey.key == "k-1",
                    )
                )
            ).scalar_one()
            assert row.expires_at is not None
    finally:
        await engine.dispose()


async def test_migrated_db__audit_dedupe_honors_payload_hash(tmp_path: Path) -> None:
    """T030: same key + same payload dedupes; different payload appends."""
    db_file = tmp_path / "parity.db"
    _migrate(db_file)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    try:
        sm = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with sm() as session:
            _seed_chain(session)
            store = SqlAuditEventStore(session)
            await store.append(
                tenant_id=TENANT,
                event_type="CaseCreated",
                actor_id=ACTOR,
                actor_type="user",
                subject_type="case",
                subject_id=uuid4(),
                case_id=None,
                correlation_id="corr-1",
                causation_id=None,
                payload={"ref": "A"},
                occurred_at=NOW,
            )
            await session.commit()
        async with sm() as session:
            dedupe = _make_dedupe(session)
            assert await dedupe(TENANT, "CaseCreated", "corr-1", payload_hash({"ref": "A"}))
            assert not await dedupe(TENANT, "CaseCreated", "corr-1", payload_hash({"ref": "B"}))
    finally:
        await engine.dispose()


async def test_migrated_db__reference_format_roundtrip(tmp_path: Path) -> None:
    """FM-009: the override row persists through the SQL adapter on migrated DDL."""
    from app.domain.reference_format import ReferenceFormat  # noqa: PLC0415
    from app.infrastructure.persistence.repositories import (  # noqa: PLC0415
        SqlReferenceFormatRepository,
    )

    db_file = tmp_path / "parity.db"
    _migrate(db_file)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    try:
        sm = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with sm() as session:
            _seed_chain(session)
            await SqlReferenceFormatRepository(session).upsert(
                ReferenceFormat(tenant_id=TENANT, template="PR-{SEQ}"),
                now=NOW,
            )
            await session.commit()
        async with sm() as session:
            fetched = await SqlReferenceFormatRepository(session).get_for_tenant(TENANT)
            assert fetched is not None
            assert fetched.template == "PR-{SEQ}"
    finally:
        await engine.dispose()


async def test_migrated_db__form_session_roundtrip(tmp_path: Path) -> None:
    """FM-038: session rows persist through the SQL adapter on migrated DDL."""
    from app.application.forms.advanced_execution import (  # noqa: PLC0415
        FormSession,
    )
    from app.infrastructure.persistence.repositories.form_repository import (  # noqa: PLC0415
        SqlFormSessionRepository,
    )

    db_file = tmp_path / "parity.db"
    _migrate(db_file)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    try:
        sm = async_sessionmaker(bind=engine, expire_on_commit=False)
        session_id = uuid4()
        async with sm() as session:
            _seed_chain(session)
            await SqlFormSessionRepository(session).add(
                FormSession(
                    id=session_id,
                    tenant_id=TENANT,
                    case_id=uuid4(),
                    form_version_id=uuid4(),
                    actor_id=ACTOR,
                    current_step_index=0,
                    completed_steps=frozenset(),
                    step_data={},
                    status="in_progress",
                    version_token=1,
                    created_at=NOW,
                    updated_at=NOW,
                    submitted_at=None,
                )
            )
            await session.commit()
        async with sm() as session:
            fetched = await SqlFormSessionRepository(session).get(TENANT, session_id)
            assert fetched is not None
            assert fetched.status == "in_progress"
            assert fetched.version_token == 1
    finally:
        await engine.dispose()


async def test_migrated_db__legal_hold_roundtrip(tmp_path: Path) -> None:
    """FM-039: legal hold rows persist through the SQL adapter on migrated DDL."""
    from app.domain.legal_hold import LegalHold  # noqa: PLC0415
    from app.infrastructure.persistence.repositories import (  # noqa: PLC0415
        SqlLegalHoldRepository,
    )

    db_file = tmp_path / "parity.db"
    _migrate(db_file)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    try:
        sm = async_sessionmaker(bind=engine, expire_on_commit=False)
        hold_id = uuid4()
        case_id = uuid4()
        async with sm() as session:
            _, case_id = _seed_chain(session)
            repo = SqlLegalHoldRepository(session)
            hold = LegalHold.place(
                tenant_id=TENANT,
                case_id=case_id,
                placed_by=ACTOR,
                reason="litigation",
                now=NOW,
            )
            # Override the id so the round-trip is deterministic.
            hold = LegalHold(
                id=hold_id,
                tenant_id=TENANT,
                case_id=case_id,
                placed_by=ACTOR,
                placed_reason="litigation",
                placed_at=NOW,
            )
            await repo.add(hold)
            await session.commit()
        async with sm() as session:
            repo = SqlLegalHoldRepository(session)
            fetched = await repo.get_active(TENANT, case_id)
            assert fetched is not None
            assert fetched.is_active is True
            assert fetched.placed_reason == "litigation"
            assert await repo.is_under_legal_hold(TENANT, case_id) is True
            # cross-tenant is False
            assert await repo.is_under_legal_hold(uuid4(), case_id) is False
        async with sm() as session:
            repo = SqlLegalHoldRepository(session)
            fetched = await repo.get_active(TENANT, case_id)
            released = fetched.release(released_by=ACTOR, reason="resolved", now=NOW)
            await repo.update(released)
            await session.commit()
        async with sm() as session:
            repo = SqlLegalHoldRepository(session)
            assert await repo.get_active(TENANT, case_id) is None
            assert await repo.is_under_legal_hold(TENANT, case_id) is False
    finally:
        await engine.dispose()
