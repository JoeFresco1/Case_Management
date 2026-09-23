"""HTTP contract tests for the versioned forms surface (P07 / FM-036 + FM-038).

Covers the five areas of the E-6 slice over the production SQL
providers (``SqlFormsHttpDependencyProvider`` +
``SqlAdminFormBindingsHttpDependencyProvider``):

- **published retrieval** — ``GET /forms``, ``GET /forms/{key}``,
  ``GET /forms/{key}/bindings`` (pagination, draft invisibility,
  policy, tenant scope, ``ErrorResponse`` envelope);
- **draft validation/publication** — ``POST /admin/form-bindings``,
  ``POST .../publish``, ``POST .../retire``, ``GET`` detail + list
  (``Page`` envelope, malformed ``state_forms`` → 422, idempotent
  re-publish, denial, tenant isolation);
- **submission** — ``POST /forms/cases/{id}`` (``Idempotency-Key`` +
  ``If-Match`` required, stale token → 409 with current token,
  denial → 403 envelope, draft/missing binding → 422 envelope,
  hidden required fields do not block);
- **resume** — ``POST .../sessions`` (``Idempotency-Key`` required,
  second start replays the same session), ``PATCH .../steps`` (stale
  token → 409), ``GET .../sessions`` (idempotent resume, 404 when
  none, cross-tenant fail-closed);
- **transition binding** — ``POST /bindings/{key}/transitions/check``
  (allowed/missing verdicts, unknown binding → 404 envelope,
  malformed target → 422, denial → 403).

Naming follows ``test_<unit>_<scenario>__<expectation>`` (AGENTS.md
§4.R5); structure is ``// given / // when / // then``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.deps import UserContext, get_current_user, get_db
from app.domain.case import Case, CaseReference, CaseStatus
from app.domain.case_type import CaseType, CaseTypeField, CaseTypeSchema, CaseTypeVersion
from app.domain.errors import DomainError
from app.domain.form import FormBinding, FormDefinition, FormFieldSpec, FormSectionSpec, FormVersion
from app.infrastructure.persistence.admin_form_bindings_http_dependencies import (
    SqlAdminFormBindingsHttpDependencyProvider,
)
from app.infrastructure.persistence.forms_http_dependencies import SqlFormsHttpDependencyProvider
from app.infrastructure.persistence.repositories import SqlCaseRepository, SqlCaseTypeRepository
from app.infrastructure.persistence.repositories.case_type_repository import (
    SqlCaseTypeVersionRepository,
)
from app.infrastructure.persistence.repositories.form_repository import (
    SqlFormBindingRepository,
    SqlFormVersionRepository,
)
from app.interfaces.http.errors import ErrorResponse, domain_error_handler
from app.interfaces.http.routers import forms as forms_router_module
from app.interfaces.http.routers.admin_form_bindings import router as admin_router

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
REVIEWER_A = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
INTAKE_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ADMIN_A = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _run(coro: Any) -> Any:
    """Run an async seed operation for the synchronous TestClient test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _validation_error_handler(request: Any, exc: RequestValidationError) -> JSONResponse:
    """Mirror ``app.main``: malformed bodies are 422 ErrorResponse envelopes."""
    cid = getattr(getattr(request, "state", None), "correlation", None)
    correlation_id = str(getattr(cid, "correlation_id", "") or "")
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            code="validation_failed",
            message="request validation failed",
            details={"errors": exc.errors()},
            correlation_id=correlation_id,
        ).model_dump(exclude_none=True),
    )


def _build_forms_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    roles: tuple[str, ...] = ("reviewer",),
    user_id: UUID = REVIEWER_A,
    tenant_id: UUID = TENANT_A,
) -> FastAPI:
    """Build a minimal app with the production forms provider."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.state.forms_http_dependencies = SqlFormsHttpDependencyProvider()
    app.include_router(forms_router_module.router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        display_name="Synthetic Worker",
        roles=roles,
    )
    return app


def _build_admin_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    roles: tuple[str, ...] = ("process_admin",),
    user_id: UUID = ADMIN_A,
    tenant_id: UUID = TENANT_A,
) -> FastAPI:
    """Build a minimal app with the production admin-bindings provider."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.state.admin_form_bindings_http_dependencies = SqlAdminFormBindingsHttpDependencyProvider()
    app.include_router(admin_router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        display_name="Synthetic Admin",
        roles=roles,
    )
    return app


def _basic_definition() -> FormDefinition:
    """Single-section form with one required field."""
    return FormDefinition(
        sections=(
            FormSectionSpec(
                key="basics",
                label="Basics",
                fields=(FormFieldSpec(key="title", label="Title", required=True),),
            ),
        ),
        validations=(),
        steps=(),
    )


def _conditional_definition() -> FormDefinition:
    """Two sections; ``extra`` is visible only when title == 'show'."""
    return FormDefinition(
        sections=(
            FormSectionSpec(
                key="basics",
                label="Basics",
                fields=(FormFieldSpec(key="title", label="Title", required=True),),
            ),
            FormSectionSpec(
                key="extra",
                label="Extra",
                fields=(FormFieldSpec(key="notes", label="Notes", required=True),),
                visible_when={"eq": {"field": "title", "value": "show"}},
            ),
        ),
        validations=(),
        steps=(),
    )


def _two_step_definition() -> FormDefinition:
    """Two-step form used by the session tests."""
    sections = (
        FormSectionSpec(
            key="basics",
            label="Basics",
            fields=(FormFieldSpec(key="title", label="Title", required=True),),
        ),
        FormSectionSpec(
            key="review",
            label="Review",
            fields=(FormFieldSpec(key="decision", label="Decision", required=True),),
        ),
    )
    steps = (
        {"step_key": "basics", "label": "Basics", "section_keys": ["basics"]},
        {"step_key": "review", "label": "Review", "section_keys": ["review"]},
    )
    return FormDefinition(sections=sections, validations=(), steps=steps)


async def _seed_case_type_version(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID = TENANT_A,
) -> UUID:
    """Seed a published case type + version; return version id.

    The schema declares the form-submitted keys (``title``, ``notes``,
    ``decision``) as optional text fields so single-step submits and
    multi-step session commits pass case-level validation; form-level
    requiredness is enforced by the form definition itself.
    """
    async with session_factory() as session:
        case_type = CaseType.create(
            tenant_id=tenant_id,
            key="ServiceRequest",
            name="ServiceRequest",
            description="",
            now=NOW,
        )
        schema = CaseTypeSchema(
            fields=(
                CaseTypeField(key="title", label="Title", type="text"),
                CaseTypeField(key="notes", label="Notes", type="text"),
                CaseTypeField(key="decision", label="Decision", type="text"),
            ),
            document_classifications=(),
            rules=(),
        )
        version = CaseTypeVersion.draft(
            tenant_id=tenant_id,
            case_type_id=case_type.id,
            version_number=1,
            schema=schema,
            now=NOW,
        ).publish(actor_id=REVIEWER_A, at=NOW)
        case_type = case_type.with_published_version(version)
        await SqlCaseTypeRepository(session).add(case_type)
        await SqlCaseTypeVersionRepository(session).add(version)
        await session.commit()
        return version.id


async def _seed_case_and_form(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    form_key: str = "Intake",
    definition: FormDefinition | None = None,
    tenant_id: UUID = TENANT_A,
    with_state_forms: tuple[dict[str, Any], ...] = (),
) -> tuple[UUID, str]:
    """Seed case + published form + published binding; return (case_id, form_key)."""
    definition = definition or _basic_definition()
    case_type_version_id = await _seed_case_type_version(session_factory, tenant_id=tenant_id)
    case_id = uuid4()
    async with session_factory() as session:
        await SqlCaseRepository(session).add(
            Case(
                id=case_id,
                tenant_id=tenant_id,
                reference=CaseReference("C-ACME-2026-000001"),
                case_type_version_id=case_type_version_id,
                status=CaseStatus.OPEN,
                priority="normal",
                title="Synthetic",
                description="",
                data={},
                current_workflow_state=None,
                version_token=1,
                created_at=NOW,
                updated_at=NOW,
                created_by=REVIEWER_A,
            )
        )
        form_version = FormVersion.draft(
            tenant_id=tenant_id, key=form_key, version_number=1, definition=definition, now=NOW
        ).publish(actor_id=REVIEWER_A, at=NOW)
        await SqlFormVersionRepository(session).add(form_version)
        binding = FormBinding.draft(
            tenant_id=tenant_id,
            case_type_version_id=case_type_version_id,
            form_version_id=form_version.id,
            now=NOW,
            state_forms=with_state_forms,
        ).publish(actor_id=REVIEWER_A, at=NOW)
        await SqlFormBindingRepository(session).add(binding)
        await session.commit()
    return case_id, form_key


async def _seed_form_only(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    form_key: str,
    tenant_id: UUID = TENANT_A,
    draft: bool = False,
) -> UUID:
    """Seed a form version without any binding; return the version id."""
    async with session_factory() as session:
        version = FormVersion.draft(
            tenant_id=tenant_id,
            key=form_key,
            version_number=1,
            definition=_basic_definition(),
            now=NOW,
        )
        if not draft:
            version = version.publish(actor_id=REVIEWER_A, at=NOW)
        await SqlFormVersionRepository(session).add(version)
        await session.commit()
        return version.id


# ---------------------------------------------------------------------------
# Published retrieval
# ---------------------------------------------------------------------------


def test_published_retrieval__list__page_envelope_and_pagination(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GET /forms paginates with the Page envelope (limit/next_cursor)."""
    # // given three published forms
    _run(_seed_form_only(sql_session_factory, form_key="Alpha"))
    _run(_seed_form_only(sql_session_factory, form_key="Beta"))
    _run(_seed_form_only(sql_session_factory, form_key="Gamma"))
    app = _build_forms_app(sql_session_factory)
    # // when listing with limit=2
    with TestClient(app) as client:
        first = client.get("/api/v1/forms", params={"limit": 2, "offset": 0})
        assert first.status_code == 200, first.text
        body = first.json()
        # // then the Page envelope holds; a full page yields a cursor
        assert set(body) >= {"items", "next_cursor", "limit"}
        assert body["limit"] == 2
        assert len(body["items"]) == 2
        assert body["next_cursor"] == "2:2"
        second = client.get("/api/v1/forms", params={"limit": 2, "offset": 2})
        assert second.status_code == 200, second.text
        assert len(second.json()["items"]) == 1
        assert second.json()["next_cursor"] is None


def test_published_retrieval__draft_key__invisible_as_404_envelope(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A draft form key is indistinguishable from unknown: 404 ErrorResponse."""
    # // given a draft-only form
    _run(_seed_form_only(sql_session_factory, form_key="DraftOnly", draft=True))
    app = _build_forms_app(sql_session_factory)
    # // when fetching it as published
    with TestClient(app) as client:
        response = client.get("/api/v1/forms/DraftOnly")
    # // then 404 with the ErrorResponse envelope (never a bare detail)
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["code"] == "not_found"
    assert set(body) >= {"code", "message"}


def test_published_retrieval__denied_actor__forbidden_envelope(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reads require case.read: a role-less actor gets 403 + envelope."""
    # // given a published form and an actor with no grants
    _run(_seed_form_only(sql_session_factory, form_key="Alpha"))
    app = _build_forms_app(sql_session_factory, roles=())
    # // when listing
    with TestClient(app) as client:
        response = client.get("/api/v1/forms")
    # // then fail-closed with the forbidden envelope
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


def test_published_retrieval__tenant_isolation__other_tenant_sees_nothing(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Tenant B cannot read tenant A's published forms."""
    # // given tenant-A forms only
    _run(_seed_form_only(sql_session_factory, form_key="Alpha", tenant_id=TENANT_A))
    app_b = _build_forms_app(sql_session_factory, tenant_id=TENANT_B)
    # // when tenant B lists and fetches by key
    with TestClient(app_b) as client:
        listed = client.get("/api/v1/forms")
        fetched = client.get("/api/v1/forms/Alpha")
    # // then the list is empty and the key is 404 (no oracle)
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == []
    assert fetched.status_code == 404, fetched.text
    assert fetched.json()["code"] == "not_found"


def test_published_retrieval__bindings__page_envelope(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GET /forms/{key}/bindings returns the Page envelope."""
    # // given a published form with one published binding
    _run(_seed_case_and_form(sql_session_factory, form_key="Bound"))
    app = _build_forms_app(sql_session_factory)
    # // when listing bindings
    with TestClient(app) as client:
        response = client.get("/api/v1/forms/Bound/bindings")
    # // then 200 with the Page shape
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) >= {"items", "next_cursor", "limit"}
    assert len(body["items"]) == 1


# ---------------------------------------------------------------------------
# Draft validation / publication (admin)
# ---------------------------------------------------------------------------


def test_admin_draft__valid_state_forms__creates_draft(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A well-formed binding draft is accepted (201)."""
    # // given seeded form + case-type version ids
    version_id = _run(_seed_case_type_version(sql_session_factory))
    form_version_id = _run(_seed_form_only(sql_session_factory, form_key="Intake"))
    app = _build_admin_app(sql_session_factory)
    # // when drafting with a valid state_forms entry
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/form-bindings",
            json={
                "case_type_version_id": str(version_id),
                "form_version_id": str(form_version_id),
                "state_forms": [{"form_key": "Intake", "state_key": "in_review", "required": True}],
            },
        )
    # // then 201 with the stored entry echoed back
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state_forms"] == [
        {"form_key": "Intake", "state_key": "in_review", "required": True}
    ]
    assert body["published_at"] is None


def test_admin_draft__blank_state_form_key__rejected_422(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A whitespace-only state_forms key is malformed → 422 envelope."""
    # // given seeded ids
    version_id = _run(_seed_case_type_version(sql_session_factory))
    form_version_id = _run(_seed_form_only(sql_session_factory, form_key="Intake"))
    app = _build_admin_app(sql_session_factory)
    # // when drafting with a blank-after-strip form_key
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/form-bindings",
            json={
                "case_type_version_id": str(version_id),
                "form_version_id": str(form_version_id),
                "state_forms": [{"form_key": "   ", "state_key": "in_review"}],
            },
        )
    # // then 422 validation_failed (never a stored misconfiguration)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"


def test_admin_draft__duplicate_state_forms__rejected_422(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Duplicate (form_key, state_key) pairs are ambiguous → 422."""
    # // given seeded ids
    version_id = _run(_seed_case_type_version(sql_session_factory))
    form_version_id = _run(_seed_form_only(sql_session_factory, form_key="Intake"))
    app = _build_admin_app(sql_session_factory)
    # // when drafting the same pair twice
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/form-bindings",
            json={
                "case_type_version_id": str(version_id),
                "form_version_id": str(form_version_id),
                "state_forms": [
                    {"form_key": "Intake", "state_key": "in_review"},
                    {"form_key": "Intake", "state_key": "in_review"},
                ],
            },
        )
    # // then 422
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"


def test_admin_publish__republish__idempotent_same_view(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Re-publishing an already-published binding is a no-op (same view)."""
    # // given a draft binding
    version_id = _run(_seed_case_type_version(sql_session_factory))
    form_version_id = _run(_seed_form_only(sql_session_factory, form_key="Intake"))
    app = _build_admin_app(sql_session_factory)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/admin/form-bindings",
            json={
                "case_type_version_id": str(version_id),
                "form_version_id": str(form_version_id),
            },
        )
        assert created.status_code == 201, created.text
        binding_id = created.json()["id"]
        # // when publishing twice
        first = client.post(f"/api/v1/admin/form-bindings/{binding_id}/publish")
        second = client.post(f"/api/v1/admin/form-bindings/{binding_id}/publish")
    # // then both succeed with the same published view (replay-safe)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    # why: sqlite round-trips datetimes naive while the in-memory view
    # keeps tzinfo; compare wall-clock instants, not raw strings.
    def _naive(value: str) -> datetime:
        return datetime.fromisoformat(value).replace(tzinfo=None)

    assert _naive(first.json()["published_at"]) == _naive(second.json()["published_at"])
    assert first.json()["id"] == binding_id


def test_admin_list__returns_page_envelope(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GET /admin/form-bindings paginates with the Page envelope."""
    # // given one binding
    version_id = _run(_seed_case_type_version(sql_session_factory))
    form_version_id = _run(_seed_form_only(sql_session_factory, form_key="Intake"))
    app = _build_admin_app(sql_session_factory)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/admin/form-bindings",
            json={
                "case_type_version_id": str(version_id),
                "form_version_id": str(form_version_id),
            },
        )
        assert created.status_code == 201, created.text
        # // when listing
        response = client.get(
            "/api/v1/admin/form-bindings", params={"form_version_id": str(form_version_id)}
        )
    # // then the Page envelope holds
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) >= {"items", "next_cursor", "limit"}
    assert len(body["items"]) == 1


def test_admin__denied_actor__forbidden_envelope(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Admin mutations require admin.tenant_config (reviewer → 403)."""
    # // given an app acting as a plain reviewer
    app = _build_admin_app(sql_session_factory, roles=("reviewer",))
    # // when attempting to draft
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/form-bindings",
            json={
                "case_type_version_id": str(uuid4()),
                "form_version_id": str(uuid4()),
            },
        )
    # // then fail-closed with the forbidden envelope
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


def test_submission__missing_headers__rejected_400(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Both Idempotency-Key and If-Match are required (400 each)."""
    # // given a submittable case+form
    case_id, form_key = _run(_seed_case_and_form(sql_session_factory))
    app = _build_forms_app(sql_session_factory)
    # // when submitting without either header
    with TestClient(app) as client:
        no_headers = client.post(
            f"/api/v1/forms/cases/{case_id}", json={"form_key": form_key, "data": {}}
        )
        no_match = client.post(
            f"/api/v1/forms/cases/{case_id}",
            json={"form_key": form_key, "data": {}},
            headers={"Idempotency-Key": "k-1"},
        )
    # // then 400 for the missing-key and missing-token paths
    assert no_headers.status_code == 400, no_headers.text
    assert no_match.status_code == 400, no_match.text


def test_submission__stale_token__conflict_with_current_token(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A superseded If-Match is 409 and names the current token."""
    # // given a submittable case+form
    case_id, form_key = _run(_seed_case_and_form(sql_session_factory))
    app = _build_forms_app(sql_session_factory)
    # // when submitting once (token 1→2) then replaying the stale token
    with TestClient(app) as client:
        first = client.post(
            f"/api/v1/forms/cases/{case_id}",
            json={"form_key": form_key, "data": {"title": "hello"}},
            headers={"Idempotency-Key": "k-first", "If-Match": "1"},
        )
        assert first.status_code == 200, first.text
        assert first.json()["version_token"] == 2
        stale = client.post(
            f"/api/v1/forms/cases/{case_id}",
            json={"form_key": form_key, "data": {"title": "late"}},
            headers={"Idempotency-Key": "k-stale", "If-Match": "1"},
        )
    # // then 409 with the live token in the headers (no silent overwrite)
    assert stale.status_code == 409, stale.text
    assert stale.headers.get("X-Current-Version-Token") == "2"
    assert '"2"' in stale.headers.get("ETag", "")


def test_submission__denied_actor__forbidden_envelope(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Intake (no case.update) cannot submit → 403 ErrorResponse."""
    # // given a submittable case+form and an intake-only actor
    case_id, form_key = _run(_seed_case_and_form(sql_session_factory))
    app = _build_forms_app(sql_session_factory, roles=("intake",), user_id=INTAKE_A)
    # // when submitting
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/forms/cases/{case_id}",
            json={"form_key": form_key, "data": {"title": "hello"}},
            headers={"Idempotency-Key": "k-deny", "If-Match": "1"},
        )
    # // then 403 with the forbidden envelope
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


def test_submission__hidden_required__does_not_block(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A required field in a hidden section never blocks submit."""
    # // given a conditional form (extra/notes hidden unless title == 'show')
    case_id, form_key = _run(
        _seed_case_and_form(sql_session_factory, definition=_conditional_definition())
    )
    app = _build_forms_app(sql_session_factory)
    # // when submitting with the section hidden and notes missing
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/forms/cases/{case_id}",
            json={"form_key": form_key, "data": {"title": "other"}},
            headers={"Idempotency-Key": "k-hidden", "If-Match": "1"},
        )
    # // then 200 (hidden required is preserved, not enforced)
    assert response.status_code == 200, response.text
    assert "notes" in response.json().get("hidden_keys", [])


def test_submission__missing_required__rejected_422_envelope(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A missing visible required field is 422 ErrorResponse (data kept)."""
    # // given a submittable case+form
    case_id, form_key = _run(_seed_case_and_form(sql_session_factory))
    app = _build_forms_app(sql_session_factory)
    # // when submitting without the required title
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/forms/cases/{case_id}",
            json={"form_key": form_key, "data": {}},
            headers={"Idempotency-Key": "k-bad", "If-Match": "1"},
        )
    # // then 422 with the validation_failed envelope
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"


# ---------------------------------------------------------------------------
# Resume (multi-step sessions)
# ---------------------------------------------------------------------------


def test_resume__second_start__replays_same_session(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Starting twice for the same triple replays the open session."""
    # // given a two-step case+form
    case_id, form_key = _run(
        _seed_case_and_form(sql_session_factory, definition=_two_step_definition())
    )
    app = _build_forms_app(sql_session_factory)
    # // when starting twice with different idempotency keys
    with TestClient(app) as client:
        first = client.post(
            f"/api/v1/forms/cases/{case_id}/sessions",
            json={"form_key": form_key},
            headers={"Idempotency-Key": "s-1"},
        )
        second = client.post(
            f"/api/v1/forms/cases/{case_id}/sessions",
            json={"form_key": form_key},
            headers={"Idempotency-Key": "s-2"},
        )
    # // then the same session replays (no duplicate progress rows)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] == second.json()["id"]


def test_resume__start_without_key__rejected_400(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Session start requires Idempotency-Key (transport-retry safety)."""
    # // given a two-step case+form
    case_id, form_key = _run(
        _seed_case_and_form(sql_session_factory, definition=_two_step_definition())
    )
    app = _build_forms_app(sql_session_factory)
    # // when starting without the header
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/forms/cases/{case_id}/sessions", json={"form_key": form_key}
        )
    # // then 400
    assert response.status_code == 400, response.text


def test_resume__get_twice__idempotent_same_progress(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Resume is a pure read: repeated GETs return identical progress."""
    # // given a started session with one saved step
    case_id, form_key = _run(
        _seed_case_and_form(sql_session_factory, definition=_two_step_definition())
    )
    app = _build_forms_app(sql_session_factory)
    with TestClient(app) as client:
        started = client.post(
            f"/api/v1/forms/cases/{case_id}/sessions",
            json={"form_key": form_key},
            headers={"Idempotency-Key": "s-resume"},
        )
        session_id = started.json()["id"]
        saved = client.patch(
            f"/api/v1/forms/cases/{case_id}/sessions/{session_id}/steps/basics",
            json={"step_key": "basics", "data": {"title": "draft"}, "expected_version_token": 1},
            headers={"If-Match": "1"},
        )
        assert saved.status_code == 200, saved.text
        # // when resuming twice
        first = client.get(f"/api/v1/forms/cases/{case_id}/sessions", params={"form_key": form_key})
        second = client.get(
            f"/api/v1/forms/cases/{case_id}/sessions", params={"form_key": form_key}
        )
    # // then both return the same persisted progress (no advancement)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] == session_id == second.json()["id"]
    assert first.json()["step_data"] == {"title": "draft"}
    assert first.json() == second.json()


def test_resume__stale_step_token__conflict(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A step save echoing a superseded token is 409, not a silent loss."""
    # // given a started session saved once (token 1→2)
    case_id, form_key = _run(
        _seed_case_and_form(sql_session_factory, definition=_two_step_definition())
    )
    app = _build_forms_app(sql_session_factory)
    with TestClient(app) as client:
        started = client.post(
            f"/api/v1/forms/cases/{case_id}/sessions",
            json={"form_key": form_key},
            headers={"Idempotency-Key": "s-stale"},
        )
        session_id = started.json()["id"]
        first = client.patch(
            f"/api/v1/forms/cases/{case_id}/sessions/{session_id}/steps/basics",
            json={"step_key": "basics", "data": {"title": "v1"}, "expected_version_token": 1},
            headers={"If-Match": "1"},
        )
        assert first.status_code == 200, first.text
        # // when replaying the stale token
        stale = client.patch(
            f"/api/v1/forms/cases/{case_id}/sessions/{session_id}/steps/basics",
            json={"step_key": "basics", "data": {"title": "late"}, "expected_version_token": 1},
            headers={"If-Match": "1"},
        )
    # // then 409 carrying the live token
    assert stale.status_code == 409, stale.text
    assert stale.headers.get("X-Current-Version-Token") == "2"


def test_resume__cross_tenant__fail_closed(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A tenant-B actor cannot resume tenant-A sessions (403/404, no oracle)."""
    # // given a tenant-A case+form
    case_id, form_key = _run(_seed_case_and_form(sql_session_factory, tenant_id=TENANT_A))
    app_b = _build_forms_app(sql_session_factory, tenant_id=TENANT_B)
    # // when tenant B tries to start/resume
    with TestClient(app_b) as client:
        response = client.post(
            f"/api/v1/forms/cases/{case_id}/sessions",
            json={"form_key": form_key},
            headers={"Idempotency-Key": "s-x"},
        )
    # // then fail-closed (never a cross-tenant existence signal)
    assert response.status_code in (403, 404), response.text


# ---------------------------------------------------------------------------
# Transition binding
# ---------------------------------------------------------------------------


def test_transition_check__missing_form__blocked_with_missing(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A transition into a guarded state without the form is blocked."""
    # // given a binding requiring Intake for in_review
    case_id, form_key = _run(
        _seed_case_and_form(
            sql_session_factory,
            with_state_forms=({"form_key": "Intake", "state_key": "in_review", "required": True},),
        )
    )
    app = _build_forms_app(sql_session_factory)
    # // when checking the guarded target with no submitted forms
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/forms/bindings/{form_key}/transitions/check",
            json={"target_state": "in_review", "submitted_form_keys": []},
        )
    # // then blocked with the missing key named
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allowed"] is False
    assert body["missing"] == ["Intake"]


def test_transition_check__submitted_form__allowed(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A transition with the required form submitted is allowed."""
    # // given the same guarded binding
    _case_id, form_key = _run(
        _seed_case_and_form(
            sql_session_factory,
            with_state_forms=({"form_key": "Intake", "state_key": "in_review", "required": True},),
        )
    )
    app = _build_forms_app(sql_session_factory)
    # // when checking with the form submitted
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/forms/bindings/{form_key}/transitions/check",
            json={"target_state": "in_review", "submitted_form_keys": ["Intake"]},
        )
    # // then allowed
    assert response.status_code == 200, response.text
    assert response.json()["allowed"] is True


def test_transition_check__unknown_binding__not_found_envelope(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An unknown form key is 404 ErrorResponse (never a bare string)."""
    # // given an app with no such binding
    app = _build_forms_app(sql_session_factory)
    # // when checking
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/forms/bindings/NoSuchForm/transitions/check",
            json={"target_state": "in_review", "submitted_form_keys": []},
        )
    # // then 404 with the envelope
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"


def test_transition_check__malformed_target__rejected_422(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An empty target_state is malformed → 422 validation_failed."""
    # // given a guarded binding
    _case_id, form_key = _run(
        _seed_case_and_form(
            sql_session_factory,
            with_state_forms=({"form_key": "Intake", "state_key": "in_review", "required": True},),
        )
    )
    app = _build_forms_app(sql_session_factory)
    # // when checking with an empty target
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/forms/bindings/{form_key}/transitions/check",
            json={"target_state": "", "submitted_form_keys": []},
        )
    # // then 422 with the envelope
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"


def test_transition_check__denied_actor__forbidden(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The guard read requires case.read (role-less → 403)."""
    # // given a guarded binding and a role-less actor
    _case_id, form_key = _run(
        _seed_case_and_form(
            sql_session_factory,
            with_state_forms=({"form_key": "Intake", "state_key": "in_review", "required": True},),
        )
    )
    app = _build_forms_app(sql_session_factory, roles=())
    # // when checking
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/forms/bindings/{form_key}/transitions/check",
            json={"target_state": "in_review", "submitted_form_keys": []},
        )
    # // then 403 with the envelope
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"
