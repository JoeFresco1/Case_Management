"""Tests for form intake submission (T3-US1 / FM-036, FM-038 execution depth).

Covers the acceptance contract from the task spec:

- intake submission creates a case from a published case-bound form;
- deterministic validation names every offending field without losing data
  (nothing is written on failure);
- attachment blobs route into the document pipeline in the same transaction;
- lookup blanks pre-populate from the configured source;
- calculated formulas are platform-computed (caller values never win);
- external signatures fail closed without a wired connector (seam);
- authorization and tenant isolation fail closed;
- dependency failure (store / lookup / signer down) raises
  ``DependencyUnavailable`` with nothing staged (caller rolls back).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.forms import intake_submission as intake_uc
from app.domain.audit import CASE_CREATED
from app.domain.case import Case, CaseReference
from app.domain.case_type import (
    CaseType,
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
)
from app.domain.document import Document, DocumentVersion
from app.domain.errors import (
    DependencyUnavailable,
    Forbidden,
    NotFound,
    ValidationFailed,
)
from app.domain.form import (
    FormBinding,
    FormDefinition,
    FormFieldSpec,
    FormSectionSpec,
    FormVersion,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class _FakeCaseRepo:
    """In-memory case repo; ``committed`` flips only when the caller commits."""

    def __init__(self) -> None:
        self._cases: dict[UUID, Case] = {}
        self.staged_adds = 0

    async def add(self, case: Case) -> None:
        self._cases[case.id] = case
        self.staged_adds += 1

    async def update(self, case: Case) -> None:
        self._cases[case.id] = case

    async def get(self, tenant_id: UUID, case_id: UUID) -> Case | None:
        case = self._cases.get(case_id)
        if case and case.tenant_id == tenant_id:
            return case
        return None

    async def get_by_reference(self, tenant_id: UUID, reference: CaseReference) -> Case | None:
        return None

    async def list(self, tenant_id: UUID, *, limit: int = 50, offset: int = 0) -> list[Case]:
        return [c for c in self._cases.values() if c.tenant_id == tenant_id]

    async def next_reference(
        self, tenant_id: UUID, *, now: datetime, type_key: str | None = None
    ) -> CaseReference:
        return CaseReference(f"C-TEST-{now.year}-000001")

    async def list_open_for_queues(
        self, tenant_id: UUID, *, queue_keys: tuple[str, ...], limit: int = 200
    ) -> list[Case]:
        return []


class _FakeCaseTypeRepo:
    def __init__(self) -> None:
        self._types: dict[UUID, CaseType] = {}

    async def get(self, tenant_id: UUID, case_type_id: UUID) -> CaseType | None:
        ct = self._types.get(case_type_id)
        if ct and ct.tenant_id == tenant_id:
            return ct
        return None

    async def get_by_key(self, tenant_id: UUID, key: str) -> CaseType | None:
        for ct in self._types.values():
            if ct.tenant_id == tenant_id and ct.key == key:
                return ct
        return None

    async def add(self, case_type: CaseType) -> None:
        self._types[case_type.id] = case_type


class _FakeCaseTypeVersionRepo:
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
                return b
        return None

    async def add(self, binding: FormBinding) -> None:
        self._bindings[binding.id] = binding


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


class _FakeDocumentRepo:
    def __init__(self) -> None:
        self.docs: dict[UUID, Document] = {}
        self.versions: list[DocumentVersion] = []

    async def add_document_and_version(
        self, *, document: Document, version: DocumentVersion
    ) -> None:
        self.docs[document.id] = document
        self.versions.append(version)

    async def get(self, tenant_id: UUID, document_id: UUID) -> Document | None:
        doc = self.docs.get(document_id)
        if doc and doc.tenant_id == tenant_id:
            return doc
        return None

    async def list_for_case(self, *, tenant_id: UUID, case_id: UUID) -> list[Document]:
        return [d for d in self.docs.values() if d.tenant_id == tenant_id and d.case_id == case_id]


class _FakeDocumentStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.puts = 0

    async def put(self, *, tenant_id: UUID, content: bytes, content_type: str, sha256: str) -> str:
        if self.fail:
            raise DependencyUnavailable("document store is unreachable")
        self.puts += 1
        return f"local://{tenant_id}/{sha256}"

    async def get(self, *, tenant_id: UUID, content_ref: str) -> bytes:
        raise DependencyUnavailable("not implemented in fake")


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


class _FakeOutbox:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[dict[str, Any]] = []

    async def enqueue(self, **kwargs: Any) -> None:
        if self.fail:
            raise DependencyUnavailable("outbox is unreachable")
        self.events.append(kwargs)


class _FakeLookup:
    """Deterministic lookup source: ``city`` -> ``city_name`` mapping."""

    def __init__(self, values: dict[str, str] | None = None, *, fail: bool = False) -> None:
        self.values = values or {}
        self.fail = fail
        self.calls = 0

    async def resolve(self, tenant_id: UUID, source: str, query: dict[str, Any]) -> str | None:
        self.calls += 1
        if self.fail:
            raise DependencyUnavailable(f"lookup source {source!r} is unreachable")
        return self.values.get(source)


class _FakeSigner:
    def __init__(self, *, verified: bool = True, fail: bool = False) -> None:
        self.verified = verified
        self.fail = fail
        self.calls = 0

    async def verify_external(
        self, tenant_id: UUID, connector: str, payload: dict[str, Any]
    ) -> bool:
        self.calls += 1
        if self.fail:
            raise DependencyUnavailable("signature connector is unreachable")
        return self.verified


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
    form_version: FormVersion
    clock: _Clock
    case_repo: _FakeCaseRepo
    case_type_repo: _FakeCaseTypeRepo
    ctv_repo: _FakeCaseTypeVersionRepo
    binding_repo: _FakeFormBindingRepo
    version_loader: _FakeFormVersionLoader
    document_repo: _FakeDocumentRepo
    document_store: _FakeDocumentStore
    audit: _FakeAudit
    outbox: _FakeOutbox


def _base_form_definition() -> FormDefinition:
    """Intake form covering every executed field family."""
    return FormDefinition(
        sections=(
            FormSectionSpec(
                key="basics",
                label="Basics",
                fields=(
                    FormFieldSpec(key="title", label="Title", required=True),
                    FormFieldSpec(
                        key="priority",
                        label="Priority",
                        field_type="enum",
                        required=True,
                        options=("low", "normal", "high", "urgent"),
                    ),
                    FormFieldSpec(
                        key="quantity", label="Quantity", field_type="integer", required=True
                    ),
                    FormFieldSpec(
                        key="unit_price",
                        label="Unit price",
                        field_type="decimal",
                        required=True,
                    ),
                    FormFieldSpec(
                        key="total",
                        label="Total",
                        field_type="calculated",
                        read_only=True,
                        formula={"op": "mul", "operands": ["quantity", "unit_price"]},
                    ),
                    FormFieldSpec(
                        key="city", label="City", field_type="lookup", lookup_source="city"
                    ),
                    FormFieldSpec(
                        key="photo",
                        label="Photo",
                        field_type="attachment",
                        attachment_classification="Evidence",
                    ),
                    FormFieldSpec(
                        key="hand_sign",
                        label="Signature",
                        field_type="signature",
                        signature_kind="handwritten",
                    ),
                    FormFieldSpec(
                        key="ref_code",
                        label="Reference",
                        field_type="system",
                        read_only=True,
                        system_property="case.reference",
                    ),
                ),
            ),
            FormSectionSpec(
                key="appeal",
                label="Appeal",
                fields=(FormFieldSpec(key="appealReason", label="Appeal Reason"),),
                visible_when={"eq": {"field": "priority", "value": "urgent"}},
            ),
        ),
        validations=(),
    )


def make_fixtures(
    definition: FormDefinition | None = None,
    *,
    store_fail: bool = False,
    outbox_fail: bool = False,
) -> Fixtures:
    tenant_id = uuid4()
    actor_id = uuid4()
    actor_roles = ("intake",)
    now = datetime.now(tz=UTC)
    ct = CaseType.create(
        tenant_id=tenant_id,
        key="ServiceRequest",
        name="Service Request",
        description="synthetic intake type",
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
            CaseTypeField(key="quantity", label="Quantity", type="integer"),
            CaseTypeField(key="unit_price", label="Unit price", type="decimal"),
            CaseTypeField(key="total", label="Total", type="decimal"),
            CaseTypeField(key="city", label="City", type="text"),
            CaseTypeField(key="photo", label="Photo", type="text"),
            CaseTypeField(key="hand_sign", label="Signature", type="text"),
            CaseTypeField(key="esign", label="E-sign", type="text"),
            CaseTypeField(key="ref_code", label="Reference", type="text"),
            CaseTypeField(key="appealReason", label="Appeal Reason", type="longtext"),
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
    form_version = FormVersion.draft(
        tenant_id=tenant_id,
        key="ServiceRequestIntake",
        version_number=1,
        definition=definition or _base_form_definition(),
        now=now,
    ).publish(actor_id=actor_id, at=now)
    binding = FormBinding.draft(
        tenant_id=tenant_id,
        case_type_version_id=ctv.id,
        form_version_id=form_version.id,
        now=now,
    ).publish(actor_id=actor_id, at=now)

    clock = _Clock(now)
    case_repo = _FakeCaseRepo()
    case_type_repo = _FakeCaseTypeRepo()
    case_type_repo._types[ct.id] = ct
    ctv_repo = _FakeCaseTypeVersionRepo()
    ctv_repo._versions[ctv.id] = ctv
    binding_repo = _FakeFormBindingRepo()
    binding_repo._bindings[binding.id] = binding
    version_loader = _FakeFormVersionLoader()
    version_loader.add(form_version)
    return Fixtures(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_roles=actor_roles,
        case_type=ct,
        case_type_version=ctv,
        form_version=form_version,
        clock=clock,
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        ctv_repo=ctv_repo,
        binding_repo=binding_repo,
        version_loader=version_loader,
        document_repo=_FakeDocumentRepo(),
        document_store=_FakeDocumentStore(fail=store_fail),
        audit=_FakeAudit(),
        outbox=_FakeOutbox(fail=outbox_fail),
    )


def _valid_submission() -> dict[str, Any]:
    return {
        "title": "Leaking faucet",
        "priority": "high",
        "quantity": 2,
        "unit_price": 10.5,
        "total": 0.0,  # why: caller value must be overwritten by the formula.
        "city": "Springfield",
        "hand_sign": "Jane Example",
    }


async def _handle(fx: Fixtures, **overrides: Any) -> Any:
    params: dict[str, Any] = {
        "tenant_id": fx.tenant_id,
        "actor_id": fx.actor_id,
        "actor_roles": fx.actor_roles,
        "form_key": "ServiceRequestIntake",
        "submitted_data": _valid_submission(),
        "attachments": {},
    }
    params.update(overrides)
    return await intake_uc.handle(
        intake_uc.SubmitIntakeCommand(**params),
        case_repo=fx.case_repo,
        case_type_repo=fx.case_type_repo,
        case_type_version_repo=fx.ctv_repo,
        form_binding_repo=fx.binding_repo,
        form_version_loader=fx.version_loader,
        document_repo=fx.document_repo,
        document_store=fx.document_store,
        audit=fx.audit,
        outbox=fx.outbox,
        clock=fx.clock,
    )


# ---------------------------------------------------------------------------
# given / when / then — intake success + atomicity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_intake__creates_case_with_audit_and_outbox_atomically() -> None:
    """T3-US1: intake submission creates the case with audit + outbox."""
    # given a published case-bound form
    fx = make_fixtures()

    # when the intake user submits
    result = await _handle(fx)

    # then the case, audit row, and outbox row share one transaction
    assert result.case.title == "Leaking faucet"
    assert result.case.data["priority"] == "high"
    assert len(fx.audit.events) == 1
    assert fx.audit.events[0]["event_type"] == CASE_CREATED
    assert fx.audit.events[0]["payload"]["form_key"] == "ServiceRequestIntake"
    assert len(fx.outbox.events) == 1
    assert fx.outbox.events[0]["event_type"] == CASE_CREATED


@pytest.mark.asyncio
async def test_submit_intake__calculated_fields_are_platform_computed() -> None:
    """Calculated ``total`` is recomputed; caller-supplied 0.0 never wins."""
    # given an intake submission with a stale calculated value
    fx = make_fixtures()

    # when submitted
    result = await _handle(fx)

    # then the formula owns the value (2 * 10.5)
    assert result.case.data["total"] == pytest.approx(21.0)
    assert "total" in result.calculated_keys


@pytest.mark.asyncio
async def test_submit_intake__system_field_filled_from_context() -> None:
    """System ``ref_code`` carries the issued case reference."""
    # given an intake submission
    fx = make_fixtures()

    # when submitted
    result = await _handle(fx)

    # then the system property resolves from context, never from input
    assert result.case.data["ref_code"] == result.case.reference.value


@pytest.mark.asyncio
async def test_submit_intake__lookup_blank_prepopulated_from_source() -> None:
    """Blank ``city`` lookup fills deterministically from the source."""
    # given a lookup source that knows the city code
    fx = make_fixtures()
    lookup = _FakeLookup(values={"city": "Springfield"})

    # when submitted without a city value
    data = _valid_submission()
    del data["city"]
    result = await intake_uc.handle(
        intake_uc.SubmitIntakeCommand(
            tenant_id=fx.tenant_id,
            actor_id=fx.actor_id,
            actor_roles=fx.actor_roles,
            form_key="ServiceRequestIntake",
            submitted_data=data,
            attachments={},
        ),
        case_repo=fx.case_repo,
        case_type_repo=fx.case_type_repo,
        case_type_version_repo=fx.ctv_repo,
        form_binding_repo=fx.binding_repo,
        form_version_loader=fx.version_loader,
        document_repo=fx.document_repo,
        document_store=fx.document_store,
        audit=fx.audit,
        outbox=fx.outbox,
        clock=fx.clock,
        lookup=lookup,
    )

    # then the lookup value lands in the stored data
    assert result.case.data["city"] == "Springfield"
    assert lookup.calls == 1


@pytest.mark.asyncio
async def test_submit_intake__attachment_blob_routed_to_document_pipeline() -> None:
    """Inline attachment bytes land as Document + version + audit + outbox."""
    # given an intake submission with an inline photo blob
    fx = make_fixtures()
    content = b"fake-image-bytes"
    blob = intake_uc.AttachmentBlob(
        file_name="photo.png", content_type="image/png", content=content
    )

    # when submitted
    result = await _handle(fx, attachments={"photo": blob})

    # then the document pipeline ran in the same transaction
    assert "photo" in result.document_ids
    doc = fx.document_repo.docs[result.document_ids["photo"]]
    assert doc.sha256 == hashlib.sha256(content).hexdigest()
    assert doc.classification == "Evidence"
    assert result.case.data["photo"] == {"document_id": str(doc.id)}
    assert any(e["event_type"] == "DocumentAdded" for e in fx.audit.events)
    assert any(e["event_type"] == "DocumentAdded" for e in fx.outbox.events)


@pytest.mark.asyncio
async def test_submit_intake__hidden_required_field_does_not_block() -> None:
    """Conditionally-hidden ``appealReason`` never blocks a normal intake."""
    # given a submission where the appeal section stays hidden
    fx = make_fixtures()

    # when submitted without appealReason
    result = await _handle(fx)

    # then the hidden key is reported but not enforced
    assert "appealReason" in result.hidden_keys
    assert "appealReason" not in result.case.data


# ---------------------------------------------------------------------------
# given / when / then — deterministic validation without data loss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_intake__errors_name_every_field_and_write_nothing() -> None:
    """Validation collects all offenders; the caller keeps entered data."""
    # given a submission with two independent defects
    fx = make_fixtures()
    bad = _valid_submission()
    del bad["title"]
    bad["priority"] = "not-a-priority"

    # when submitted
    with pytest.raises(ValidationFailed) as exc_info:
        await _handle(fx, submitted_data=bad)

    # then every offender is named and nothing was staged
    assert exc_info.value.field is not None
    assert "title" in exc_info.value.field.split(",")
    assert "priority" in exc_info.value.field.split(",")
    assert fx.case_repo.staged_adds == 0
    assert fx.audit.events == []
    assert fx.outbox.events == []
    # and the caller still holds the entered payload (no data loss)
    assert bad["priority"] == "not-a-priority"


@pytest.mark.asyncio
async def test_submit_intake__type_error_names_field() -> None:
    """A text value for the integer ``quantity`` fails with the field named."""
    # given a wrong-typed quantity
    fx = make_fixtures()
    bad = _valid_submission()
    bad["quantity"] = "two"

    # when submitted then the field is named and nothing is staged
    with pytest.raises(ValidationFailed) as exc_info:
        await _handle(fx, submitted_data=bad)
    assert exc_info.value.field is not None
    assert "quantity" in exc_info.value.field.split(",")
    assert fx.case_repo.staged_adds == 0


# ---------------------------------------------------------------------------
# given / when / then — signature seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_intake__external_signature_without_connector_fails_closed() -> None:
    """An external signature with no wired connector is rejected by field."""
    # given a form whose signature must go through an external signer
    definition = FormDefinition(
        sections=(
            FormSectionSpec(
                key="sign",
                label="Sign",
                fields=(
                    FormFieldSpec(key="title", label="Title", required=True),
                    FormFieldSpec(
                        key="priority",
                        label="Priority",
                        field_type="enum",
                        required=True,
                        options=("low", "normal", "high", "urgent"),
                    ),
                    FormFieldSpec(
                        key="esign",
                        label="E-sign",
                        field_type="signature",
                        signature_kind="external",
                        signature_connector="acme-sign",
                        required=True,
                    ),
                ),
            ),
        ),
        validations=(),
    )
    fx = make_fixtures(definition)
    fx.ctv_repo._versions[fx.case_type_version.id] = fx.case_type_version

    # when submitted with no connector wired then it fails closed
    with pytest.raises(ValidationFailed) as exc_info:
        await _handle(
            fx,
            submitted_data={
                "title": "Needs signing",
                "priority": "normal",
                "esign": "signed-payload",
            },
        )
    assert exc_info.value.field == "esign"
    assert fx.case_repo.staged_adds == 0


@pytest.mark.asyncio
async def test_submit_intake__external_signature_verified_by_connector() -> None:
    """A verifying connector accepts the external signature (seam)."""
    # given the external-signature form plus a verifying connector
    definition = FormDefinition(
        sections=(
            FormSectionSpec(
                key="sign",
                label="Sign",
                fields=(
                    FormFieldSpec(key="title", label="Title", required=True),
                    FormFieldSpec(
                        key="priority",
                        label="Priority",
                        field_type="enum",
                        required=True,
                        options=("low", "normal", "high", "urgent"),
                    ),
                    FormFieldSpec(
                        key="esign",
                        label="E-sign",
                        field_type="signature",
                        signature_kind="external",
                        signature_connector="acme-sign",
                        required=True,
                    ),
                ),
            ),
        ),
        validations=(),
    )
    fx = make_fixtures(definition)

    # when submitted then the seam verdict accepts
    result = await intake_uc.handle(
        intake_uc.SubmitIntakeCommand(
            tenant_id=fx.tenant_id,
            actor_id=fx.actor_id,
            actor_roles=fx.actor_roles,
            form_key="ServiceRequestIntake",
            submitted_data={
                "title": "Needs signing",
                "priority": "normal",
                "esign": "signed-payload",
            },
            attachments={},
        ),
        case_repo=fx.case_repo,
        case_type_repo=fx.case_type_repo,
        case_type_version_repo=fx.ctv_repo,
        form_binding_repo=fx.binding_repo,
        form_version_loader=fx.version_loader,
        document_repo=fx.document_repo,
        document_store=fx.document_store,
        audit=fx.audit,
        outbox=fx.outbox,
        clock=fx.clock,
        signature_connector=_FakeSigner(verified=True),
    )
    assert result.case.data["esign"] == "signed-payload"


@pytest.mark.asyncio
async def test_submit_intake__external_signature_rejected_by_connector() -> None:
    """A negative connector verdict rejects the field (no silent accept)."""
    # given the external-signature form plus a rejecting connector
    definition = FormDefinition(
        sections=(
            FormSectionSpec(
                key="sign",
                label="Sign",
                fields=(
                    FormFieldSpec(key="title", label="Title", required=True),
                    FormFieldSpec(
                        key="priority",
                        label="Priority",
                        field_type="enum",
                        required=True,
                        options=("low", "normal", "high", "urgent"),
                    ),
                    FormFieldSpec(
                        key="esign",
                        label="E-sign",
                        field_type="signature",
                        signature_kind="external",
                        required=True,
                    ),
                ),
            ),
        ),
        validations=(),
    )
    fx = make_fixtures(definition)

    # when submitted then the field is rejected and nothing is staged
    with pytest.raises(ValidationFailed) as exc_info:
        await intake_uc.handle(
            intake_uc.SubmitIntakeCommand(
                tenant_id=fx.tenant_id,
                actor_id=fx.actor_id,
                actor_roles=fx.actor_roles,
                form_key="ServiceRequestIntake",
                submitted_data={
                    "title": "Needs signing",
                    "priority": "normal",
                    "esign": "signed-payload",
                },
                attachments={},
            ),
            case_repo=fx.case_repo,
            case_type_repo=fx.case_type_repo,
            case_type_version_repo=fx.ctv_repo,
            form_binding_repo=fx.binding_repo,
            form_version_loader=fx.version_loader,
            document_repo=fx.document_repo,
            document_store=fx.document_store,
            audit=fx.audit,
            outbox=fx.outbox,
            clock=fx.clock,
            signature_connector=_FakeSigner(verified=False),
        )
    assert exc_info.value.field == "esign"
    assert fx.case_repo.staged_adds == 0


# ---------------------------------------------------------------------------
# given / when / then — auth + tenant isolation fail closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_intake__unauthorized_actor_denied_before_writes() -> None:
    """An actor without ``CASE_CREATE`` is denied with nothing staged."""
    # given an actor with no grants
    fx = make_fixtures()

    # when they submit then Forbidden and nothing is staged
    with pytest.raises(Forbidden):
        await _handle(fx, actor_roles=())
    assert fx.case_repo.staged_adds == 0
    assert fx.audit.events == []
    assert fx.outbox.events == []


@pytest.mark.asyncio
async def test_submit_intake__cross_tenant_form_never_leaks() -> None:
    """A form from another tenant surfaces as Forbidden/NotFound, unstaged."""
    # given a binding that belongs to a different tenant
    fx = make_fixtures()
    other_tenant = uuid4()
    other_binding = FormBinding.draft(
        tenant_id=other_tenant,
        case_type_version_id=fx.case_type_version.id,
        form_version_id=fx.form_version.id,
        now=fx.clock.now(),
    ).publish(actor_id=fx.actor_id, at=fx.clock.now())
    fx.binding_repo._bindings.clear()
    fx.binding_repo._bindings[other_binding.id] = other_binding

    # when submitted then no cross-tenant existence is confirmed
    with pytest.raises((Forbidden, NotFound, ValidationFailed)):
        await _handle(fx)
    assert fx.case_repo.staged_adds == 0
    assert fx.audit.events == []


# ---------------------------------------------------------------------------
# given / when / then — dependency failure rolls back
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_intake__store_failure_stages_nothing() -> None:
    """A down document store raises with zero staged rows (rollback)."""
    # given an unreachable document store
    fx = make_fixtures(store_fail=True)
    blob = intake_uc.AttachmentBlob(
        file_name="photo.png", content_type="image/png", content=b"bytes"
    )

    # when submitted then nothing is staged (caller has nothing to undo)
    with pytest.raises(DependencyUnavailable):
        await _handle(fx, attachments={"photo": blob})
    assert fx.case_repo.staged_adds == 0
    assert fx.audit.events == []
    assert fx.outbox.events == []
    assert fx.document_repo.docs == {}


@pytest.mark.asyncio
async def test_submit_intake__lookup_failure_rolls_back() -> None:
    """A down lookup source raises with zero staged rows (rollback)."""
    # given an unreachable lookup source and a blank lookup field
    fx = make_fixtures()
    data = _valid_submission()
    del data["city"]

    # when submitted then nothing is staged
    with pytest.raises(DependencyUnavailable):
        await intake_uc.handle(
            intake_uc.SubmitIntakeCommand(
                tenant_id=fx.tenant_id,
                actor_id=fx.actor_id,
                actor_roles=fx.actor_roles,
                form_key="ServiceRequestIntake",
                submitted_data=data,
                attachments={},
            ),
            case_repo=fx.case_repo,
            case_type_repo=fx.case_type_repo,
            case_type_version_repo=fx.ctv_repo,
            form_binding_repo=fx.binding_repo,
            form_version_loader=fx.version_loader,
            document_repo=fx.document_repo,
            document_store=fx.document_store,
            audit=fx.audit,
            outbox=fx.outbox,
            clock=fx.clock,
            lookup=_FakeLookup(fail=True),
        )
    assert fx.case_repo.staged_adds == 0
    assert fx.audit.events == []
    assert fx.outbox.events == []


@pytest.mark.asyncio
async def test_submit_intake__outbox_failure_propagates_for_rollback() -> None:
    """An outbox failure propagates so the caller rolls back the insert."""
    # given an unreachable outbox
    fx = make_fixtures(outbox_fail=True)

    # when submitted then the failure propagates (caller rolls back)
    with pytest.raises(DependencyUnavailable):
        await _handle(fx)


@pytest.mark.asyncio
async def test_submit_intake__draft_form_rejected() -> None:
    """A retired/draft form version is never submittable on intake."""
    # given only a draft form version behind the binding
    fx = make_fixtures()
    draft = FormVersion.draft(
        tenant_id=fx.tenant_id,
        key="ServiceRequestIntake",
        version_number=2,
        definition=fx.form_version.definition,
        now=fx.clock.now(),
    )
    fx.version_loader.add(draft)
    fx.binding_repo._bindings.clear()
    draft_binding = FormBinding.draft(
        tenant_id=fx.tenant_id,
        case_type_version_id=fx.case_type_version.id,
        form_version_id=draft.id,
        now=fx.clock.now(),
    )
    await fx.binding_repo.add(draft_binding)

    # when submitted then rejected with nothing staged
    with pytest.raises(ValidationFailed, match="no published form binding"):
        await _handle(fx)
    assert fx.case_repo.staged_adds == 0
