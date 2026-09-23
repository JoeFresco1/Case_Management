"""Tests for form submission (P07 / FM-036).

Covers:
- Submit succeeds with published form bound to ServiceRequest
- Hidden field not overwritten (P07-US3)
- Conditional section hides/shows deterministically (P07-US2)
- Draft form not submittable (explicit acceptance criterion)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.forms import submit_form as submit_form_uc
from app.domain.audit import CASE_UPDATED
from app.domain.case import Case, CaseReference
from app.domain.case_type import (
    CaseType,
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.errors import Conflict, InvariantViolation, NotFound, ValidationFailed
from app.domain.form import (
    FormBinding,
    FormDefinition,
    FormFieldSpec,
    FormSectionSpec,
    FormVersion,
    apply_hidden_field_preservation,
)
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    CaseTypeVersionRepository,
    OutboxWriter,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime.now(tz=UTC)

    def now(self) -> datetime:
        return self._now


class _FakeCaseRepo(CaseRepository):
    def __init__(self) -> None:
        self._cases: dict[UUID, Case] = {}

    async def add(self, case: Case) -> None:
        self._cases[case.id] = case

    async def update(self, case: Case) -> None:
        if case.id not in self._cases:
            raise NotFound("case not found")
        # Optimistic concurrency check
        existing = self._cases[case.id]
        if existing.version_token != case.version_token - 1:
            raise Conflict("stale concurrency token")
        self._cases[case.id] = case

    async def get(self, tenant_id: UUID, case_id: UUID) -> Case | None:
        case = self._cases.get(case_id)
        if case and case.tenant_id == tenant_id:
            return case
        return None

    async def get_by_reference(self, tenant_id: UUID, reference: CaseReference) -> Case | None:
        for case in self._cases.values():
            if case.tenant_id == tenant_id and case.reference == reference:
                return case
        return None

    async def list(self, tenant_id: UUID, *, limit: int = 50, offset: int = 0) -> list[Case]:
        cases = [c for c in self._cases.values() if c.tenant_id == tenant_id]
        return cases[offset : offset + limit]

    async def next_reference(
        self, tenant_id: UUID, *, now: datetime, type_key: str | None = None
    ) -> CaseReference:
        return CaseReference(f"C-TEST-{now.year}-000001")

    async def list_open_for_queues(
        self, tenant_id: UUID, *, queue_keys: tuple[str, ...], limit: int = 200
    ) -> list[Case]:
        return []


class _FakeCaseTypeVersionRepo(CaseTypeVersionRepository):
    def __init__(self) -> None:
        self._versions: dict[UUID, CaseTypeVersion] = {}

    async def get(self, tenant_id: UUID, case_type_version_id: UUID) -> CaseTypeVersion | None:
        v = self._versions.get(case_type_version_id)
        if v and v.tenant_id == tenant_id:
            return v
        return None

    async def get_published_for_case_type(
        self, tenant_id: UUID, case_type_id: UUID
    ) -> CaseTypeVersion | None:
        for v in self._versions.values():
            if (
                v.tenant_id == tenant_id
                and v.case_type_id == case_type_id
                and v.status == CaseTypeVersionStatus.PUBLISHED
            ):
                return v
        return None

    async def get_by_key(self, tenant_id: UUID, case_type_key: str) -> CaseTypeVersion | None:
        return None

    async def add(self, version: CaseTypeVersion) -> None:
        self._versions[version.id] = version


class _FakeFormBindingRepo:
    def __init__(self) -> None:
        self._bindings: dict[UUID, FormBinding] = {}

    async def get_published_for_key(self, tenant_id: UUID, form_key: str) -> FormBinding | None:
        for b in self._bindings.values():
            if b.tenant_id == tenant_id and b.published_at is not None:
                # Note: In real repo we join FormVersion to get the key. Here we assume
                # the binding's form_version_id matches the form we want.
                return b
        return None

    async def list_published_for_form_version(
        self, tenant_id: UUID, form_version_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> list[FormBinding]:
        bindings = [
            b
            for b in self._bindings.values()
            if b.tenant_id == tenant_id and b.form_version_id == form_version_id
        ]
        return bindings[offset : offset + limit]

    async def add(self, binding: FormBinding) -> None:
        self._bindings[binding.id] = binding

    async def mark_published(
        self, tenant_id: UUID, binding_id: UUID, *, published_at: datetime, published_by: UUID
    ) -> None:
        b = self._bindings.get(binding_id)
        if not b or b.tenant_id != tenant_id:
            raise NotFound("binding not found")
        if b.published_at is not None:
            raise InvariantViolation("binding already published")
        self._bindings[binding_id] = FormBinding(
            id=b.id,
            tenant_id=b.tenant_id,
            case_type_version_id=b.case_type_version_id,
            form_version_id=b.form_version_id,
            published_at=published_at,
            published_by=published_by,
            created_at=b.created_at,
        )


class _FakeFormVersionLoader:
    def __init__(self) -> None:
        self._versions: dict[UUID, FormVersion] = {}

    async def get_by_id(self, tenant_id: UUID, form_version_id: UUID) -> FormVersion | None:
        v = self._versions.get(form_version_id)
        if v and v.tenant_id == tenant_id:
            return v
        return None

    def add(self, version: FormVersion) -> None:
        self._versions[version.id] = version


class _FakeAudit(AuditEventStore):
    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID,
        actor_type: str,
        subject_type: str,
        subject_id: UUID,
        case_id: UUID | None,
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        self._events.append(
            {
                "tenant_id": tenant_id,
                "event_type": event_type,
                "actor_id": actor_id,
                "actor_type": actor_type,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "case_id": case_id,
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "payload": payload,
                "occurred_at": occurred_at,
            }
        )

    async def list_for_case(
        self,
        *,
        tenant_id: UUID,
        case_id: UUID,
        event_types: list[str] | None = None,
        actor_id: UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[Any]:
        return []

    async def list_for_export(
        self,
        *,
        tenant_id: UUID,
        case_id: UUID | None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[Any]:
        return []

    async def get_event(self, *, tenant_id: UUID, event_id: UUID) -> Any | None:
        return None

    async def list_access_decisions(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID | None = None,
        subject_type: str | None = None,
        action: str | None = None,
        result: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[Any]:
        return []


class _FakeOutbox(OutboxWriter):
    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        event_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        self._events.append(
            {
                "tenant_id": tenant_id,
                "event_type": event_type,
                "event_id": event_id,
                "payload": payload,
            }
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class Fixtures:
    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_type: CaseType
    case_type_version: CaseTypeVersion
    case: Case
    form_version: FormVersion
    form_binding: FormBinding
    clock: _Clock
    case_repo: _FakeCaseRepo
    ctv_repo: _FakeCaseTypeVersionRepo
    form_binding_repo: _FakeFormBindingRepo
    form_version_loader: _FakeFormVersionLoader
    audit: _FakeAudit
    outbox: _FakeOutbox


def make_fixtures() -> Fixtures:
    tenant_id = uuid4()
    actor_id = uuid4()
    actor_roles = ("reviewer",)  # reviewer has CASE_UPDATE permission
    now = datetime.now(tz=UTC)

    # Case type with fields: title, priority, isAppeal, appealReason
    ct = CaseType.create(
        tenant_id=tenant_id,
        key="ServiceRequest",
        name="Service Request",
        description="A service request case",
        now=now,
    )

    schema = CaseTypeSchema(
        fields=(
            CaseTypeField(key="title", label="Title", type="text", required=True),
            CaseTypeField(
                key="priority",
                label="Priority",
                type="enum",
                required=True,
                enum_values=("low", "normal", "high", "urgent"),
            ),
            CaseTypeField(key="isAppeal", label="Is Appeal", type="boolean", required=False),
            CaseTypeField(
                key="appealReason", label="Appeal Reason", type="longtext", required=False
            ),
        ),
        document_classifications=("Contract", "Correspondence", "Evidence"),
    )

    ctv = CaseTypeVersion.draft(
        tenant_id=tenant_id,
        case_type_id=ct.id,
        version_number=1,
        schema=schema,
        now=now,
    ).publish(actor_id=actor_id, at=now)

    # Published case type
    ct = CaseType(
        id=ct.id,
        tenant_id=ct.tenant_id,
        key=ct.key,
        name=ct.name,
        description=ct.description,
        sensitivity_default=ct.sensitivity_default,
        current_version_id=ctv.id,
        created_at=ct.created_at,
        updated_at=ct.updated_at,
    )

    # Case
    case = Case.create(
        tenant_id=tenant_id,
        reference=CaseReference("C-TEST-2026-000001"),
        case_type_version=ctv,
        title="Initial Case",
        description="",
        data={"title": "Initial Case", "priority": "normal", "isAppeal": False},
        priority="normal",
        created_by=actor_id,
        now=now,
        sensitivity="internal",
    )

    # Form definition with conditional section
    form_definition = FormDefinition(
        sections=(
            FormSectionSpec(
                key="basic",
                label="Basic Information",
                fields=(
                    FormFieldSpec(key="title", label="Title", required=True),
                    FormFieldSpec(key="priority", label="Priority", required=True),
                    FormFieldSpec(key="isAppeal", label="Is appeal", field_type="boolean"),
                ),
            ),
            FormSectionSpec(
                key="appeal",
                label="Appeal Details",
                fields=(FormFieldSpec(key="appealReason", label="Appeal Reason", required=False),),
                visible_when={"eq": {"field": "isAppeal", "value": True}},
            ),
        ),
        validations=(
            {
                "when": {"truthy": {"field": "isAppeal"}},
                "then": {"required": "appealReason"},
            },
        ),
    )

    form_version = FormVersion.draft(
        tenant_id=tenant_id,
        key="ServiceRequestIntake",
        version_number=1,
        definition=form_definition,
        now=now,
    ).publish(actor_id=actor_id, at=now)

    form_binding = FormBinding.draft(
        tenant_id=tenant_id,
        case_type_version_id=ctv.id,
        form_version_id=form_version.id,
        now=now,
    ).publish(actor_id=actor_id, at=now)

    clock = _Clock(now)
    case_repo = _FakeCaseRepo()
    ctv_repo = _FakeCaseTypeVersionRepo()
    form_binding_repo = _FakeFormBindingRepo()
    form_version_loader = _FakeFormVersionLoader()
    audit = _FakeAudit()
    outbox = _FakeOutbox()

    # Seed - use synchronous adds since fakes don't need async
    case_repo._cases[case.id] = case
    ctv_repo._versions[ctv.id] = ctv
    form_binding_repo._bindings[form_binding.id] = form_binding
    form_version_loader.add(form_version)

    return Fixtures(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_roles=actor_roles,
        case_type=ct,
        case_type_version=ctv,
        case=case,
        form_version=form_version,
        form_binding=form_binding,
        clock=clock,
        case_repo=case_repo,
        ctv_repo=ctv_repo,
        form_binding_repo=form_binding_repo,
        form_version_loader=form_version_loader,
        audit=audit,
        outbox=outbox,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_form_succeeds_with_published_form():
    """P07-US1: Intake user submits published form bound to ServiceRequest."""
    fx = make_fixtures()

    result = await submit_form_uc.handle(
        submit_form_uc.SubmitFormCommand(
            tenant_id=fx.tenant_id,
            actor_id=fx.actor_id,
            actor_roles=fx.actor_roles,
            case_id=fx.case.id,
            form_key="ServiceRequestIntake",
            submitted_data={"title": "Updated Title", "priority": "high", "isAppeal": False},
            expected_version_token=fx.case.version_token,
        ),
        case_repo=fx.case_repo,
        case_type_version_repo=fx.ctv_repo,
        form_binding_repo=fx.form_binding_repo,
        form_version_loader=fx.form_version_loader,
        audit=fx.audit,
        outbox=fx.outbox,
        clock=fx.clock,
    )

    # Form fields map to case.data, not case.title
    assert result.case.data["title"] == "Updated Title"
    assert result.case.data["priority"] == "high"
    assert result.case.data["isAppeal"] is False
    assert len(fx.audit._events) == 1
    assert fx.audit._events[0]["event_type"] == CASE_UPDATED
    assert fx.audit._events[0]["payload"]["form_key"] == "ServiceRequestIntake"


@pytest.mark.asyncio
async def test_submit_form_hidden_field_preserved():
    """P07-US3: Hidden field data is preserved, not overwritten."""
    fx = make_fixtures()

    # First submission with appeal section visible
    submitted_data_1 = {
        "title": "Case with Appeal",
        "priority": "normal",
        "isAppeal": True,
        "appealReason": "Original reason",
    }
    result1 = await submit_form_uc.handle(
        submit_form_uc.SubmitFormCommand(
            tenant_id=fx.tenant_id,
            actor_id=fx.actor_id,
            actor_roles=fx.actor_roles,
            case_id=fx.case.id,
            form_key="ServiceRequestIntake",
            submitted_data=submitted_data_1,
            expected_version_token=fx.case.version_token,
        ),
        case_repo=fx.case_repo,
        case_type_version_repo=fx.ctv_repo,
        form_binding_repo=fx.form_binding_repo,
        form_version_loader=fx.form_version_loader,
        audit=fx.audit,
        outbox=fx.outbox,
        clock=fx.clock,
    )

    assert result1.case.data["appealReason"] == "Original reason"
    assert result1.case.data["isAppeal"] is True

    # Second submission with appeal section hidden (isAppeal = false)
    # The appealReason should be preserved, not blanked
    result2 = await submit_form_uc.handle(
        submit_form_uc.SubmitFormCommand(
            tenant_id=fx.tenant_id,
            actor_id=fx.actor_id,
            actor_roles=fx.actor_roles,
            case_id=fx.case.id,
            form_key="ServiceRequestIntake",
            submitted_data={"title": "Updated", "priority": "high", "isAppeal": False},
            expected_version_token=result1.case.version_token,
        ),
        case_repo=fx.case_repo,
        case_type_version_repo=fx.ctv_repo,
        form_binding_repo=fx.form_binding_repo,
        form_version_loader=fx.form_version_loader,
        audit=fx.audit,
        outbox=fx.outbox,
        clock=fx.clock,
    )

    # appealReason should still be "Original reason" (preserved)
    assert result2.case.data["appealReason"] == "Original reason"
    assert result2.case.data["isAppeal"] is False
    # hidden_keys should include appealReason
    assert "appealReason" in result2.hidden_keys


@pytest.mark.asyncio
async def test_submit_form_conditional_section_shows_when_true():
    """P07-US2: Conditional section shows when condition is true."""
    fx = make_fixtures()

    # Submit with isAppeal = true -> appeal section visible
    submitted_data_2 = {
        "title": "Appeal Case",
        "priority": "normal",
        "isAppeal": True,
        "appealReason": "Detailed reason",
    }
    result = await submit_form_uc.handle(
        submit_form_uc.SubmitFormCommand(
            tenant_id=fx.tenant_id,
            actor_id=fx.actor_id,
            actor_roles=fx.actor_roles,
            case_id=fx.case.id,
            form_key="ServiceRequestIntake",
            submitted_data=submitted_data_2,
            expected_version_token=fx.case.version_token,
        ),
        case_repo=fx.case_repo,
        case_type_version_repo=fx.ctv_repo,
        form_binding_repo=fx.form_binding_repo,
        form_version_loader=fx.form_version_loader,
        audit=fx.audit,
        outbox=fx.outbox,
        clock=fx.clock,
    )

    assert result.case.data["isAppeal"] is True
    assert result.case.data["appealReason"] == "Detailed reason"
    assert "appealReason" not in result.hidden_keys


@pytest.mark.asyncio
async def test_submit_form_conditional_section_hides_when_false():
    """P07-US2: Conditional section hides when condition is false."""
    fx = make_fixtures()

    # Submit with isAppeal = false -> appeal section hidden
    result = await submit_form_uc.handle(
        submit_form_uc.SubmitFormCommand(
            tenant_id=fx.tenant_id,
            actor_id=fx.actor_id,
            actor_roles=fx.actor_roles,
            case_id=fx.case.id,
            form_key="ServiceRequestIntake",
            submitted_data={"title": "Normal Case", "priority": "normal", "isAppeal": False},
            expected_version_token=fx.case.version_token,
        ),
        case_repo=fx.case_repo,
        case_type_version_repo=fx.ctv_repo,
        form_binding_repo=fx.form_binding_repo,
        form_version_loader=fx.form_version_loader,
        audit=fx.audit,
        outbox=fx.outbox,
        clock=fx.clock,
    )

    assert result.case.data["isAppeal"] is False
    assert "appealReason" in result.hidden_keys
    # appealReason should not be in the case data (never submitted)
    assert "appealReason" not in result.case.data


@pytest.mark.asyncio
async def test_submit_form_draft_form_rejected():
    """Draft form is not submittable (explicit acceptance criterion)."""
    fx = make_fixtures()

    # Create a draft form version
    draft_form = FormVersion.draft(
        tenant_id=fx.tenant_id,
        key="ServiceRequestIntake",
        version_number=2,
        definition=fx.form_version.definition,
        now=fx.clock.now(),
    )
    fx.form_version_loader.add(draft_form)

    # Remove the published binding, add draft binding instead
    fx.form_binding_repo._bindings.clear()
    draft_binding = FormBinding.draft(
        tenant_id=fx.tenant_id,
        case_type_version_id=fx.case_type_version.id,
        form_version_id=draft_form.id,
        now=fx.clock.now(),
    )
    await fx.form_binding_repo.add(draft_binding)

    with pytest.raises(ValidationFailed, match="no published form binding"):
        await submit_form_uc.handle(
            submit_form_uc.SubmitFormCommand(
                tenant_id=fx.tenant_id,
                actor_id=fx.actor_id,
                actor_roles=fx.actor_roles,
                case_id=fx.case.id,
                form_key="ServiceRequestIntake",
                submitted_data={"title": "Test", "priority": "normal"},
                expected_version_token=fx.case.version_token,
            ),
            case_repo=fx.case_repo,
            case_type_version_repo=fx.ctv_repo,
            form_binding_repo=fx.form_binding_repo,
            form_version_loader=fx.form_version_loader,
            audit=fx.audit,
            outbox=fx.outbox,
            clock=fx.clock,
        )


@pytest.mark.asyncio
async def test_submit_form_form_level_validation_enforced():
    """P07 acceptance scenario G2: Form-level validation (when isAppeal=true,
    appealReason required)."""
    fx = make_fixtures()

    # Submit with isAppeal = true but missing appealReason
    match_str = "form rule requires 'appealReason' when the condition holds"
    with pytest.raises(ValidationFailed, match=match_str):
        await submit_form_uc.handle(
            submit_form_uc.SubmitFormCommand(
                tenant_id=fx.tenant_id,
                actor_id=fx.actor_id,
                actor_roles=fx.actor_roles,
                case_id=fx.case.id,
                form_key="ServiceRequestIntake",
                submitted_data={"title": "Appeal Case", "priority": "normal", "isAppeal": True},
                expected_version_token=fx.case.version_token,
            ),
            case_repo=fx.case_repo,
            case_type_version_repo=fx.ctv_repo,
            form_binding_repo=fx.form_binding_repo,
            form_version_loader=fx.form_version_loader,
            audit=fx.audit,
            outbox=fx.outbox,
            clock=fx.clock,
        )


@pytest.mark.asyncio
async def test_submit_form_binding_immutability():
    """P07-US4: Form binding is immutable on publish."""
    fx = make_fixtures()

    # Try to publish the same binding twice
    with pytest.raises(Exception, match="already published"):
        await fx.form_binding_repo.mark_published(
            tenant_id=fx.tenant_id,
            binding_id=fx.form_binding.id,
            published_at=fx.clock.now(),
            published_by=fx.actor_id,
        )


@pytest.mark.asyncio
async def test_apply_hidden_field_preservation_helper():
    """Test the apply_hidden_field_preservation helper directly."""
    stored = {"title": "Original", "appealReason": "Preserved value", "isAppeal": True}
    submitted = {"title": "Updated", "isAppeal": False}
    hidden = frozenset({"appealReason"})

    merged = apply_hidden_field_preservation(
        stored_data=stored,
        submitted_data=submitted,
        hidden_keys=hidden,
    )

    assert merged["title"] == "Updated"
    assert merged["isAppeal"] is False
    assert merged["appealReason"] == "Preserved value"


@pytest.mark.asyncio
async def test_submit_form_stale_version_token_raises_conflict():
    """Stale If-Match token raises Conflict before any audit write."""
    fx = make_fixtures()

    with pytest.raises(Exception, match="stale concurrency token"):
        await submit_form_uc.handle(
            submit_form_uc.SubmitFormCommand(
                tenant_id=fx.tenant_id,
                actor_id=fx.actor_id,
                actor_roles=fx.actor_roles,
                case_id=fx.case.id,
                form_key="ServiceRequestIntake",
                submitted_data={"title": "Test", "priority": "normal"},
                expected_version_token=999,  # Stale token
            ),
            case_repo=fx.case_repo,
            case_type_version_repo=fx.ctv_repo,
            form_binding_repo=fx.form_binding_repo,
            form_version_loader=fx.form_version_loader,
            audit=fx.audit,
            outbox=fx.outbox,
            clock=fx.clock,
        )

    # Audit should not have been written for failed attempt
    assert len(fx.audit._events) == 0


@pytest.mark.asyncio
async def test_submit_form_cross_tenant_returns_not_found():
    """Cross-tenant form access returns NotFound (no existence leak)."""
    fx = make_fixtures()
    other_tenant = uuid4()

    with pytest.raises(NotFound):
        await submit_form_uc.handle(
            submit_form_uc.SubmitFormCommand(
                tenant_id=other_tenant,
                actor_id=fx.actor_id,
                actor_roles=fx.actor_roles,
                case_id=fx.case.id,
                form_key="ServiceRequestIntake",
                submitted_data={"title": "Test", "priority": "normal"},
                expected_version_token=fx.case.version_token,
            ),
            case_repo=fx.case_repo,
            case_type_version_repo=fx.ctv_repo,
            form_binding_repo=fx.form_binding_repo,
            form_version_loader=fx.form_version_loader,
            audit=fx.audit,
            outbox=fx.outbox,
            clock=fx.clock,
        )


@pytest.mark.asyncio
async def test_submit_form_missing_case_raises_not_found():
    """Missing case raises NotFound."""
    fx = make_fixtures()
    missing_case_id = uuid4()

    with pytest.raises(NotFound):
        await submit_form_uc.handle(
            submit_form_uc.SubmitFormCommand(
                tenant_id=fx.tenant_id,
                actor_id=fx.actor_id,
                actor_roles=fx.actor_roles,
                case_id=missing_case_id,
                form_key="ServiceRequestIntake",
                submitted_data={"title": "Test", "priority": "normal"},
                expected_version_token=1,
            ),
            case_repo=fx.case_repo,
            case_type_version_repo=fx.ctv_repo,
            form_binding_repo=fx.form_binding_repo,
            form_version_loader=fx.form_version_loader,
            audit=fx.audit,
            outbox=fx.outbox,
            clock=fx.clock,
        )


@pytest.mark.asyncio
async def test_submit_form_missing_binding_raises_validation_failed():
    """Missing published binding raises ValidationFailed."""
    fx = make_fixtures()
    # Remove the binding
    fx.form_binding_repo._bindings.clear()

    with pytest.raises(ValidationFailed, match="no published form binding"):
        await submit_form_uc.handle(
            submit_form_uc.SubmitFormCommand(
                tenant_id=fx.tenant_id,
                actor_id=fx.actor_id,
                actor_roles=fx.actor_roles,
                case_id=fx.case.id,
                form_key="NonExistentForm",
                submitted_data={"title": "Test", "priority": "normal"},
                expected_version_token=fx.case.version_token,
            ),
            case_repo=fx.case_repo,
            case_type_version_repo=fx.ctv_repo,
            form_binding_repo=fx.form_binding_repo,
            form_version_loader=fx.form_version_loader,
            audit=fx.audit,
            outbox=fx.outbox,
            clock=fx.clock,
        )


@pytest.mark.asyncio
async def test_submit_form_mismatched_case_type_version_raises_validation_failed():
    """Binding for different case type version raises ValidationFailed."""
    fx = make_fixtures()

    # Create a different case type version
    other_ctv = CaseTypeVersion.draft(
        tenant_id=fx.tenant_id,
        case_type_id=fx.case_type.id,
        version_number=2,
        schema=fx.case_type_version.schema,
        now=fx.clock.now(),
    ).publish(actor_id=fx.actor_id, at=fx.clock.now())
    await fx.ctv_repo.add(other_ctv)

    # Remove the original binding, add binding for the OTHER case type version
    fx.form_binding_repo._bindings.clear()
    other_binding = FormBinding.draft(
        tenant_id=fx.tenant_id,
        case_type_version_id=other_ctv.id,  # Different CTV!
        form_version_id=fx.form_version.id,
        now=fx.clock.now(),
    ).publish(actor_id=fx.actor_id, at=fx.clock.now())
    await fx.form_binding_repo.add(other_binding)

    with pytest.raises(ValidationFailed, match="does not match the case's case-type version"):
        await submit_form_uc.handle(
            submit_form_uc.SubmitFormCommand(
                tenant_id=fx.tenant_id,
                actor_id=fx.actor_id,
                actor_roles=fx.actor_roles,
                case_id=fx.case.id,
                form_key="ServiceRequestIntake",
                submitted_data={"title": "Test", "priority": "normal"},
                expected_version_token=fx.case.version_token,
            ),
            case_repo=fx.case_repo,
            case_type_version_repo=fx.ctv_repo,
            form_binding_repo=fx.form_binding_repo,
            form_version_loader=fx.form_version_loader,
            audit=fx.audit,
            outbox=fx.outbox,
            clock=fx.clock,
        )
