"""Interface tests for import dry-run/commit + quarantine release/reject.

Covers the HTTP boundary in ``app.interfaces.http.routers.imports`` and
``app.interfaces.http.routers.channels`` (quarantine slice):

- dry-run preview returns a ``PREVIEWED`` job without writing cases;
- commit creates cases plus reconciliation;
- replay with the same ``Idempotency-Key`` returns the identical body;
- mutations without ``Idempotency-Key`` are 400;
- key reuse with a different payload is 409;
- non-admin actors are 403 with the ``ErrorResponse`` envelope;
- cross-tenant reads are 404 without leaking foreign existence;
- malformed UUIDs / status filters are 422;
- list pagination is bounded (default 50, max 200);
- release/reject move HELD records to terminal states; resolving a
  terminal record the wrong way is 422;
- dependency failure surfaces as 503/500 ``ErrorResponse`` without
  traceback or SQL.

Naming follows AGENTS.md R5: ``test_<unit>_<scenario>__<expectation>``.
Structure is ``// given / // when / // then``. The suite is sqlite-backed
so it never requires Docker (AGENTS.md R1 fallback).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import Base
from app.deps import UserContext, get_current_user, get_db
from app.domain.case_type import CaseTypeField, CaseTypeSchema, CaseTypeVersionStatus
from app.domain.errors import DependencyUnavailable, DomainError
from app.infrastructure.persistence import models  # noqa: F401 — register tables
from app.infrastructure.persistence.channels_http_dependencies import (
    SqlChannelHttpDependencyProvider,
)
from app.infrastructure.persistence.idempotency_http_dependencies import (
    SqlIdempotencyHttpDependencyProvider,
)
from app.infrastructure.persistence.imports_http_dependencies import (
    SqlImportHttpDependencyProvider,
)
from app.infrastructure.storage.local_document_store import LocalDocumentStore
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.idempotency import IdempotencyMiddleware
from app.interfaces.http.routers.channels import router as channels_router
from app.interfaces.http.routers.imports import router as imports_router

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ADMIN_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ADMIN_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
INTAKE_A = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


def _utc(year: int, month: int, day: int) -> datetime:
    """Return a fixed UTC instant for deterministic seeding."""
    return datetime(year, month, day, 12, 0, 0, tzinfo=UTC)


FIXED_NOW = _utc(2026, 8, 31)


def _run(coro: Any) -> Any:
    """Run an awaitable to completion in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed_tenant_and_type(
    session: AsyncSession, *, tenant_id: UUID, key: str = "SERVICE_REQUEST"
) -> None:
    """Insert a tenant plus a published case type with title/priority.

    Why uppercase ``SERVICE_REQUEST``: ``SqlCaseRepository.next_reference``
    renders ``{TYPE}`` through ``ReferenceFormat`` which only accepts
    ``[A-Z0-9_]+``. The mixed-case demo key ``ServiceRequest`` would fail
    reference validation on a real commit (``type_key has invalid
    characters``) and quarantine the row instead of creating a case.
    """
    from app.infrastructure.persistence.models import (  # noqa: PLC0415
        CaseType as CaseTypeRow,
    )
    from app.infrastructure.persistence.models import (  # noqa: PLC0415
        CaseTypeVersion as CaseTypeVersionRow,
    )
    from app.infrastructure.persistence.models import Tenant as TenantRow  # noqa: PLC0415

    session.add(
        TenantRow(id=tenant_id, key=f"T{tenant_id.hex[:6]}", name="Synthetic", created_at=FIXED_NOW)
    )
    case_type_id = uuid4()
    version_id = uuid4()
    session.add(
        CaseTypeRow(
            id=case_type_id,
            tenant_id=tenant_id,
            key=key,
            name=key,
            description="",
            sensitivity_default="internal",
            current_version_id=version_id,
            created_at=FIXED_NOW,
            updated_at=FIXED_NOW,
        )
    )
    schema = CaseTypeSchema(
        fields=(
            CaseTypeField(
                key="title", label="Title", type="text", required=True, sensitivity="internal"
            ),
            CaseTypeField(
                key="priority",
                label="Priority",
                type="enum",
                required=True,
                enum_values=("low", "normal", "high", "urgent"),
                sensitivity="internal",
            ),
        )
    )
    session.add(
        CaseTypeVersionRow(
            id=version_id,
            tenant_id=tenant_id,
            case_type_id=case_type_id,
            version_number=1,
            schema_json={
                "fields": [
                    {
                        "key": f.key,
                        "label": f.label,
                        "type": f.type,
                        "required": f.required,
                        "enum_values": list(f.enum_values),
                        "sensitivity": f.sensitivity,
                        "searchable": f.searchable,
                    }
                    for f in schema.fields
                ]
            },
            document_classifications=[],
            rules=[],
            status=CaseTypeVersionStatus.PUBLISHED.value,
            published_at=FIXED_NOW,
            published_by=ADMIN_A,
            created_at=FIXED_NOW,
        )
    )
    await session.flush()


async def _seed_two_tenants_async(sm: async_sessionmaker[AsyncSession]) -> None:
    """Seed TENANT_A and TENANT_B with their own published case types."""
    async with sm() as session:
        await _seed_tenant_and_type(session, tenant_id=TENANT_A)
        await _seed_tenant_and_type(session, tenant_id=TENANT_B)
        await session.commit()


def _import_payload(*, source_label: str = "synthetic-import") -> dict[str, Any]:
    """Return a minimal valid dry-run/commit body for SERVICE_REQUEST."""
    return {
        "source_label": source_label,
        "source_format": "csv",
        "mapping": {
            "version": 1,
            "case_type_key": "SERVICE_REQUEST",
            "fields": [
                {"source": "title", "target": "title"},
                {"source": "priority", "target": "priority"},
            ],
            "title_field": "title",
            "priority_field": "priority",
        },
        "rows": [{"index": 1, "values": {"title": "Synthetic case", "priority": "normal"}}],
    }


def _build_app(
    sm: async_sessionmaker[AsyncSession],
    docs_root: Path,
    *,
    user_id: UUID = ADMIN_A,
    tenant_id: UUID = TENANT_A,
    roles: tuple[str, ...] = ("process_admin",),
) -> FastAPI:
    """Build the imports+quarantine app with idempotency middleware enabled."""

    async def _override_get_db(request: Request) -> AsyncIterator[AsyncSession]:
        # why: share the middleware-owned session so the mutation and its
        # replay record commit atomically; GETs (no middleware session)
        # get a fresh session that commits on the way out.
        shared = getattr(request.state, "idempotency_session", None)
        if shared is not None:
            yield shared
            return
        async with sm() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _override_user() -> UserContext:
        return UserContext(user_id=user_id, tenant_id=tenant_id, display_name="", roles=roles)

    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        cid = getattr(getattr(request, "state", None), "correlation", None)
        corr = getattr(cid, "correlation_id", "") if cid is not None else ""
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_failed",
                "message": "request validation failed",
                "details": {"errors": exc.errors()},
                "correlation_id": corr,
            },
        )

    app = FastAPI()
    app.state.imports_http_dependencies = SqlImportHttpDependencyProvider()
    app.state.channel_http_dependencies = SqlChannelHttpDependencyProvider()
    app.state.idempotency_http_dependencies = SqlIdempotencyHttpDependencyProvider(sessionmaker=sm)
    app.state.document_store = LocalDocumentStore(root=docs_root)
    app.add_middleware(IdempotencyMiddleware, scope_paths=("/api/v1/",))
    app.include_router(imports_router, prefix="/api/v1")
    app.include_router(channels_router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)  # type: ignore[arg-type]
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_user
    return app


def _engine_and_sm(tmp_path: Path) -> tuple[Any, async_sessionmaker[AsyncSession], Path]:
    """Create a fresh sqlite engine (NullPool) plus sessionmaker and docs dir."""
    db_file = tmp_path / f"imports_{uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}", poolclass=NullPool)
    sm = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def _create_all() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    _run(_create_all())
    docs_root = tmp_path / f"docs_{uuid4().hex}"
    docs_root.mkdir(parents=True, exist_ok=True)
    return engine, sm, docs_root


def _dispose(engine: Any) -> None:
    """Dispose the per-test engine on a fresh loop."""
    _run(engine.dispose())


def _enable_channel(client: TestClient, *, key: str = "import-main") -> str:
    """Enable an import channel requiring title; return its id."""
    response = client.post(
        "/api/v1/admin/channels",
        json={"key": key, "kind": "import", "policy": {"required_fields": ["title"]}},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 200, response.text
    return response.json()["channel"]["id"]


def _capture_held(client: TestClient, channel_id: str, *, label: str) -> str:
    """Capture a row missing title; assert quarantined and return record id."""
    response = client.post(
        f"/api/v1/admin/channels/{channel_id}/captures",
        json={"source_label": label, "values": {}},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is False
    assert body["record"] is not None
    assert body["record"]["status"] == "held"
    return body["record"]["id"]


def _error_code(body: Any) -> str | None:
    """Return the top-level or detail-wrapped error code for assertions."""
    if isinstance(body, dict):
        if isinstance(body.get("code"), str):
            return body["code"]
        detail = body.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("code"), str):
            return detail["code"]
    return None


# ---------------------------------------------------------------------------
# 1-2. success paths
# ---------------------------------------------------------------------------


def test_dry_run_success__returns_preview_without_cases(tmp_path: Path) -> None:
    """Dry-run validates and persists a PREVIEWED job without committing cases."""
    # // given a seeded tenant and the admin app
    engine, sm, docs_root = _engine_and_sm(tmp_path)
    try:
        _run(_seed_two_tenants_async(sm))
        app = _build_app(sm, docs_root)
        # // when previewing one valid row
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/imports/dry-run",
                json=_import_payload(),
                headers={"Idempotency-Key": str(uuid4())},
            )
        # // then 200 with a previewed job carrying the row report
        assert response.status_code == 200, response.text
        job = response.json()["job"]
        assert job["status"] == "previewed"
        assert job["source_row_count"] == 1
        assert job["committed_at"] is None
        assert job["results"][0]["outcome"] == "accepted"
        # // and the job is readable back without a commit marker
        with TestClient(app) as client:
            fetched = client.get(f"/api/v1/imports/{job['id']}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["status"] == "previewed"
    finally:
        _dispose(engine)


def test_commit_success__creates_cases_and_reconciliation(tmp_path: Path) -> None:
    """Commit re-validates, creates one case, and records reconciliation."""
    # // given a seeded tenant and the admin app
    engine, sm, docs_root = _engine_and_sm(tmp_path)
    try:
        _run(_seed_two_tenants_async(sm))
        app = _build_app(sm, docs_root)
        # // when committing one valid row
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/imports/commit",
                json=_import_payload(),
                headers={"Idempotency-Key": str(uuid4())},
            )
        # // then 201 with one created case and a committed reconciliation
        assert response.status_code == 201, response.text
        body = response.json()
        assert len(body["created_case_ids"]) == 1
        assert body["job"]["status"] == "committed"
        assert body["job"]["reconciliation"]["committed_count"] == 1
        assert body["job"]["reconciliation"]["rejected_count"] == 0
    finally:
        _dispose(engine)


# ---------------------------------------------------------------------------
# 3-5. idempotency
# ---------------------------------------------------------------------------


def test_commit_replay__same_idempotency_key_returns_identical_body(tmp_path: Path) -> None:
    """Retrying a commit with the same key+body replays the stored response."""
    # // given a seeded tenant and the admin app
    engine, sm, docs_root = _engine_and_sm(tmp_path)
    try:
        _run(_seed_two_tenants_async(sm))
        app = _build_app(sm, docs_root)
        key = str(uuid4())
        payload = _import_payload()
        headers = {"Idempotency-Key": key}
        # // when committing twice with the identical key and body
        with TestClient(app) as client:
            first = client.post("/api/v1/imports/commit", json=payload, headers=headers)
            second = client.post("/api/v1/imports/commit", json=payload, headers=headers)
        # // then both succeed with byte-identical bodies; the retry is a replay
        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        assert second.json() == first.json()
        assert second.headers.get("idempotency-replay") == "true"
    finally:
        _dispose(engine)


def test_mutation_missing_key__returns_400_validation_failed(tmp_path: Path) -> None:
    """Every mutation without Idempotency-Key is refused before any DB touch."""
    # // given a seeded tenant and the admin app
    engine, sm, docs_root = _engine_and_sm(tmp_path)
    try:
        _run(_seed_two_tenants_async(sm))
        app = _build_app(sm, docs_root)
        with TestClient(app) as client:
            channel_id = _enable_channel(client)
            # // when mutating without the header on all five endpoints
            dry = client.post("/api/v1/imports/dry-run", json=_import_payload())
            commit = client.post("/api/v1/imports/commit", json=_import_payload())
            capture = client.post(
                f"/api/v1/admin/channels/{channel_id}/captures",
                json={"source_label": "x", "values": {}},
            )
            release = client.post(f"/api/v1/admin/quarantine/{uuid4()}/release", json={})
            reject = client.post(f"/api/v1/admin/quarantine/{uuid4()}/reject", json={})
        # // then each is 400 carrying a validation/missing-key code
        for response in (dry, commit, capture, release, reject):
            assert response.status_code == 400, response.text
            assert _error_code(response.json()) in ("validation_failed", "missing_idempotency_key")
    finally:
        _dispose(engine)


def test_mutation_mismatched_key__returns_409_conflict(tmp_path: Path) -> None:
    """Reusing a key with a different payload is a 409 conflict."""
    # // given a committed import under key K
    engine, sm, docs_root = _engine_and_sm(tmp_path)
    try:
        _run(_seed_two_tenants_async(sm))
        app = _build_app(sm, docs_root)
        key = str(uuid4())
        with TestClient(app) as client:
            first = client.post(
                "/api/v1/imports/commit",
                json=_import_payload(source_label="label-a"),
                headers={"Idempotency-Key": key},
            )
            # // when reusing K with a different source_label
            second = client.post(
                "/api/v1/imports/commit",
                json=_import_payload(source_label="label-b"),
                headers={"Idempotency-Key": key},
            )
        # // then the first commits and the second conflicts without leaking
        assert first.status_code == 201, first.text
        assert second.status_code == 409, second.text
        assert second.json()["code"] == "conflict"
        assert "Traceback" not in second.text
    finally:
        _dispose(engine)


# ---------------------------------------------------------------------------
# 6-7. authorization + tenancy (server-side)
# ---------------------------------------------------------------------------


def test_denial__non_admin_returns_403_error_response(tmp_path: Path) -> None:
    """An intake actor cannot dry-run, commit, or inspect quarantine."""
    # // given the admin-seeded DB but an intake-only app
    engine, sm, docs_root = _engine_and_sm(tmp_path)
    try:
        _run(_seed_two_tenants_async(sm))
        app = _build_app(sm, docs_root, user_id=INTAKE_A, roles=("intake",))
        with TestClient(app) as client:
            # // when attempting privileged mutations and reads
            dry = client.post(
                "/api/v1/imports/dry-run",
                json=_import_payload(),
                headers={"Idempotency-Key": str(uuid4())},
            )
            listed = client.get("/api/v1/admin/quarantine")
        # // then both are 403 forbidden envelopes with no traceback
        assert dry.status_code == 403, dry.text
        assert dry.json()["code"] == "forbidden"
        assert listed.status_code == 403, listed.text
        assert listed.json()["code"] == "forbidden"
        for response in (dry, listed):
            assert "Traceback" not in response.text
            assert "SELECT " not in response.text.upper()
    finally:
        _dispose(engine)


def test_cross_tenant__foreign_job_or_quarantine_returns_404_without_leak(tmp_path: Path) -> None:
    """Tenant B cannot read or resolve tenant A's job/quarantine rows."""
    # // given two tenants where A owns a job and a held quarantine record
    engine, sm, docs_root = _engine_and_sm(tmp_path)
    try:
        _run(_seed_two_tenants_async(sm))
        app_a = _build_app(sm, docs_root, user_id=ADMIN_A, tenant_id=TENANT_A)
        with TestClient(app_a) as client:
            job_id = client.post(
                "/api/v1/imports/dry-run",
                json=_import_payload(),
                headers={"Idempotency-Key": str(uuid4())},
            ).json()["job"]["id"]
            channel_id = _enable_channel(client, key="import-a")
            quarantine_id = _capture_held(client, channel_id, label="row-a")
        # // when tenant B fetches the foreign job and releases the foreign record
        app_b = _build_app(sm, docs_root, user_id=ADMIN_B, tenant_id=TENANT_B)
        with TestClient(app_b) as client:
            foreign_job = client.get(f"/api/v1/imports/{job_id}")
            foreign_release = client.post(
                f"/api/v1/admin/quarantine/{quarantine_id}/release",
                json={"note": "sneaky"},
                headers={"Idempotency-Key": str(uuid4())},
            )
        # // then both are 404 without confirming foreign existence
        assert foreign_job.status_code == 404, foreign_job.text
        assert _error_code(foreign_job.json()) == "not_found"
        assert foreign_release.status_code == 404, foreign_release.text
        assert foreign_release.json()["code"] == "not_found"
        assert str(TENANT_A) not in foreign_job.text
        assert str(TENANT_A) not in foreign_release.text
    finally:
        _dispose(engine)


# ---------------------------------------------------------------------------
# 8-9. malformed input + bounded pagination
# ---------------------------------------------------------------------------


def test_malformed__bad_uuid_and_bad_status_return_422_validation_failed(tmp_path: Path) -> None:
    """Bad UUID path params and unknown quarantine status are structured 422s."""
    # // given the admin app
    engine, sm, docs_root = _engine_and_sm(tmp_path)
    try:
        _run(_seed_two_tenants_async(sm))
        app = _build_app(sm, docs_root)
        with TestClient(app) as client:
            # // when requesting a non-UUID job id and a bogus status filter
            bad_uuid = client.get("/api/v1/imports/not-a-uuid")
            bad_status = client.get("/api/v1/admin/quarantine?status=bogus")
        # // then both are 422 validation_failed envelopes without internals
        assert bad_uuid.status_code == 422, bad_uuid.text
        assert bad_uuid.json()["code"] == "validation_failed"
        assert bad_status.status_code == 422, bad_status.text
        assert bad_status.json()["code"] == "validation_failed"
        for response in (bad_uuid, bad_status):
            assert "Traceback" not in response.text
            assert "SELECT " not in response.text.upper()
    finally:
        _dispose(engine)


def test_quarantine_list__bounded_pagination_defaults_50_max_200(tmp_path: Path) -> None:
    """List endpoints default to 50 and reject limits above 200."""
    # // given the admin app
    engine, sm, docs_root = _engine_and_sm(tmp_path)
    try:
        _run(_seed_two_tenants_async(sm))
        app = _build_app(sm, docs_root)
        with TestClient(app) as client:
            # // when listing without a limit and with over/under ceilings
            default_jobs = client.get("/api/v1/imports")
            capped_jobs = client.get("/api/v1/imports?limit=200")
            over_jobs = client.get("/api/v1/imports?limit=500")
            over_quarantine = client.get("/api/v1/admin/quarantine?limit=500")
            ok_quarantine = client.get("/api/v1/admin/quarantine?limit=200")
        # // then defaults echo 50, 200 is honored, and 500 is a 422
        assert default_jobs.status_code == 200, default_jobs.text
        assert default_jobs.json()["limit"] == 50
        assert capped_jobs.status_code == 200, capped_jobs.text
        assert capped_jobs.json()["limit"] == 200
        assert over_jobs.status_code == 422, over_jobs.text
        assert over_quarantine.status_code == 422, over_quarantine.text
        assert ok_quarantine.status_code == 200, ok_quarantine.text
    finally:
        _dispose(engine)


# ---------------------------------------------------------------------------
# 10. release / reject lifecycle
# ---------------------------------------------------------------------------


def test_release_reject__held_to_terminal_and_second_resolve_conflicts(tmp_path: Path) -> None:
    """HELD resolves once; the wrong terminal transition is a 422."""
    # // given a channel and two held records
    engine, sm, docs_root = _engine_and_sm(tmp_path)
    try:
        _run(_seed_two_tenants_async(sm))
        app = _build_app(sm, docs_root)
        with TestClient(app) as client:
            channel_id = _enable_channel(client)
            first_id = _capture_held(client, channel_id, label="row-1")
            second_id = _capture_held(client, channel_id, label="row-2")
            # // when releasing the first record twice
            released = client.post(
                f"/api/v1/admin/quarantine/{first_id}/release",
                json={"note": "fixed upstream"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            released_again = client.post(
                f"/api/v1/admin/quarantine/{first_id}/release",
                json={"note": "retry"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            # // then the first release wins and the retry is idempotent
            assert released.status_code == 200, released.text
            assert released.json()["record"]["status"] == "released"
            assert released.json()["record"]["resolution_note"] == "fixed upstream"
            assert released_again.status_code == 200, released_again.text
            assert released_again.json()["record"]["status"] == "released"
            # // when rejecting an already-released record // then 422
            wrong = client.post(
                f"/api/v1/admin/quarantine/{first_id}/reject",
                json={},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert wrong.status_code == 422, wrong.text
            assert wrong.json()["code"] == "validation_failed"
            # // when rejecting the second held record twice
            rejected = client.post(
                f"/api/v1/admin/quarantine/{second_id}/reject",
                json={"note": "duplicate"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            rejected_again = client.post(
                f"/api/v1/admin/quarantine/{second_id}/reject",
                json={},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert rejected.status_code == 200, rejected.text
            assert rejected.json()["record"]["status"] == "rejected"
            assert rejected_again.status_code == 200, rejected_again.text
            # // when releasing an already-rejected record // then 422
            wrong_release = client.post(
                f"/api/v1/admin/quarantine/{second_id}/release",
                json={},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert wrong_release.status_code == 422, wrong_release.text
    finally:
        _dispose(engine)


# ---------------------------------------------------------------------------
# 11. dependency failure
# ---------------------------------------------------------------------------


def test_dependency_failure__store_unavailable_returns_503_or_500_ErrorResponse(
    tmp_path: Path,
) -> None:
    """A failing adapter surfaces as a structured 5xx without internals."""
    # // given an app whose case-type port is unavailable
    engine, sm, docs_root = _engine_and_sm(tmp_path)
    try:
        _run(_seed_two_tenants_async(sm))

        class _FailingCaseTypeRepo:
            """Fail every lookup so the preview cannot resolve its type."""

            async def get_by_key(self, *args: Any, **kwargs: Any) -> Any:
                raise DependencyUnavailable("synthetic case-type store unavailable")

            async def get(self, *args: Any, **kwargs: Any) -> Any:
                raise DependencyUnavailable("synthetic case-type store unavailable")

        real_provider = SqlImportHttpDependencyProvider()

        class _FailingImportsProvider:
            """Delegate to SQL except for the unavailable case-type port."""

            def for_session(self, session: object) -> Any:
                repos = real_provider.for_session(session)
                return repos.__class__(
                    case=repos.case,
                    case_type=_FailingCaseTypeRepo(),  # type: ignore[arg-type]
                    case_type_version=repos.case_type_version,
                    document=repos.document,
                    audit=repos.audit,
                    outbox=repos.outbox,
                    role=repos.role,
                    import_jobs=repos.import_jobs,
                    import_mappings=repos.import_mappings,
                )

        app = _build_app(sm, docs_root)
        app.state.imports_http_dependencies = _FailingImportsProvider()
        # // when previewing // then a structured 5xx with no leak
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/v1/imports/dry-run",
                json=_import_payload(),
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert response.status_code in (500, 503), response.text
        body = response.json()
        assert {"code", "message", "correlation_id"} <= set(body)
        assert "Traceback" not in response.text
        assert "SELECT " not in response.text.upper()
    finally:
        _dispose(engine)


__all__: list[str] = []
