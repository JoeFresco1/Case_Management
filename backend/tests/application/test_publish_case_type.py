"""Application tests for `publish_case_type.handle` (FM-008 / FR-015).

The acceptance scenarios for this slice are encoded as
``test_<unit>_<scenario>__<expectation>`` (AGENTS.md §4.R5):

1. **Success**: draft → validate → publish flips the row to published,
   promotes it as the CaseType's current version, and emits a
   ``ConfigurationPublished`` audit event in the same transaction.
2. **Validator rejection**: a schema with a missing required field
   raises ``ValidationFailed`` and writes nothing (no draft, no audit).
3. **Idempotent re-publish**: publishing the same version a second
   time raises ``InvariantViolation`` because the row's status is no
   longer ``draft``.
4. **Tenant isolation (2-tenant)**: a tenant B actor cannot publish a
   version that belongs to tenant A — the version lookup misses and
   ``NotFound`` is raised.
5. **Unknown version**: a request for a non-existent version id
   returns ``NotFound``.
6. **History endpoint** (companion, via ``publication_history``):
   ``GET /api/v1/admin/case-types/{key}/versions`` returns every
   version newest-first.

Naming follows AGENTS.md §4.R5. Tests are deterministic (fixed clock,
fixed UUIDs).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.application.configuration import draft_case_type, publish_case_type
from app.application.configuration.publication_history import (
    PublicationHistoryQuery,
)
from app.application.configuration.publication_history import (
    handle as history_handle,
)
from app.domain.case_type import (
    CaseType,
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersionStatus,
)
from app.domain.errors import InvariantViolation, NotFound, ValidationFailed

from ._publish_case_type_fakes import (
    FixedClock,
    InMemoryAuditStore,
    InMemoryCaseTypeRepository,
    InMemoryCaseTypeVersionRepository,
)

# ---------------------------------------------------------------------------
# Constants — two distinct tenants
# ---------------------------------------------------------------------------


ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
BETA_TENANT = UUID("22222222-2222-2222-2222-222222222222")

ADMIN_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SUPERVISOR_USER = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def _healthy_schema() -> CaseTypeSchema:
    return CaseTypeSchema(
        fields=(
            CaseTypeField(
                key="title",
                label="Title",
                type="text",
                required=True,
            ),
            CaseTypeField(
                key="priority",
                label="Priority",
                type="enum",
                required=True,
                enum_values=("low", "normal", "high"),
            ),
        ),
        document_classifications=("Correspondence",),
        rules=({"op": "exists", "field": "title"},),
    )


def _seed_case_type(
    *, tenant_id: UUID, key: str = "ServiceRequest"
) -> tuple[UUID, InMemoryCaseTypeRepository, InMemoryCaseTypeVersionRepository]:
    """Seed an empty CaseType header; returns its id and fresh repos."""
    case_type_repo = InMemoryCaseTypeRepository()
    version_repo = InMemoryCaseTypeVersionRepository()
    case_type = CaseType.create(
        tenant_id=tenant_id,
        key=key,
        name="Service Request",
    )
    case_type_repo.add(case_type)
    return case_type.id, case_type_repo, version_repo


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


@pytest.fixture
def audit() -> InMemoryAuditStore:
    return InMemoryAuditStore()


@pytest.fixture
def seeded_acme() -> tuple[UUID, InMemoryCaseTypeRepository, InMemoryCaseTypeVersionRepository]:
    case_type_id, ctr, vtr = _seed_case_type(tenant_id=ACME_TENANT)
    return case_type_id, ctr, vtr


# ---------------------------------------------------------------------------
# 1. Success
# ---------------------------------------------------------------------------


async def test_publish_case_type__draft_validate_publish__flips_status_promotes_current_and_audits(
    seeded_acme: tuple[UUID, InMemoryCaseTypeRepository, InMemoryCaseTypeVersionRepository],
    clock: FixedClock,
    audit: InMemoryAuditStore,
) -> None:
    """Happy path: draft → validate → publish succeeds, the row is
    flipped to ``published``, the CaseType's ``current_version_id`` is
    set, and one ``ConfigurationPublished`` audit row is appended."""
    _, case_type_repo, version_repo = seeded_acme

    # Step 1: draft.
    draft_result = await draft_case_type.handle(
        draft_case_type.DraftCaseTypeVersionCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            case_type_key="ServiceRequest",
            schema=_healthy_schema(),
            correlation_id="corr-draft",
        ),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        audit_store=audit,
        clock=clock,
    )
    assert draft_result.version_number == 1
    assert len(version_repo._by_id) == 1
    assert audit.events[-1].event_type == "ConfigurationDrafted"

    # Step 2 + 3: publish.
    result = await publish_case_type.handle(
        publish_case_type.PublishCaseTypeVersionCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            case_type_version_id=draft_result.case_type_version_id,
            correlation_id="corr-publish",
        ),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        audit_store=audit,
        clock=clock,
    )

    assert result.status is CaseTypeVersionStatus.PUBLISHED
    assert result.version_number == 1

    # Row was transitioned (draft → published) exactly once.
    assert version_repo.status_transitions == [
        (
            draft_result.case_type_version_id,
            CaseTypeVersionStatus.DRAFT,
            CaseTypeVersionStatus.PUBLISHED,
        )
    ]

    # CaseType header points at the published version.
    assert case_type_repo.set_current_version_calls == [
        (ACME_TENANT, draft_result.case_type_id, draft_result.case_type_version_id)
    ]
    promoted = await case_type_repo.get(ACME_TENANT, draft_result.case_type_id)
    assert promoted is not None
    assert promoted.current_version_id == draft_result.case_type_version_id

    # The persisted row carries the publish stamps.
    stored = await version_repo.get(ACME_TENANT, draft_result.case_type_version_id)
    assert stored is not None
    assert stored.status is CaseTypeVersionStatus.PUBLISHED
    assert stored.published_at == FIXED_NOW
    assert stored.published_by == ADMIN_USER

    # Audit: one Drafted + one Published, both tenant-scoped and
    # carrying the correlation ids.
    types = [e.event_type for e in audit.events]
    assert types == ["ConfigurationDrafted", "ConfigurationPublished"]
    published_audit = audit.events[-1]
    assert published_audit.tenant_id == ACME_TENANT
    assert published_audit.subject_id == draft_result.case_type_version_id
    assert published_audit.correlation_id == "corr-publish"
    assert published_audit.payload["case_type_key"] == "ServiceRequest"
    assert published_audit.payload["version_number"] == 1


async def test_publish_case_type__second_draft_then_publish__version_number_increments(
    seeded_acme: tuple[UUID, InMemoryCaseTypeRepository, InMemoryCaseTypeVersionRepository],
    clock: FixedClock,
    audit: InMemoryAuditStore,
) -> None:
    """Version numbers are monotonic per CaseType (1, 2, 3, …)."""
    _, case_type_repo, version_repo = seeded_acme

    first = await draft_case_type.handle(
        draft_case_type.DraftCaseTypeVersionCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            case_type_key="ServiceRequest",
            schema=_healthy_schema(),
            correlation_id="c1",
        ),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        audit_store=audit,
        clock=clock,
    )
    await publish_case_type.handle(
        publish_case_type.PublishCaseTypeVersionCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            case_type_version_id=first.case_type_version_id,
            correlation_id="c1",
        ),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        audit_store=audit,
        clock=clock,
    )

    second = await draft_case_type.handle(
        draft_case_type.DraftCaseTypeVersionCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            case_type_key="ServiceRequest",
            schema=_healthy_schema(),
            correlation_id="c2",
        ),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        audit_store=audit,
        clock=clock,
    )
    assert second.version_number == 2
    publish_result = await publish_case_type.handle(
        publish_case_type.PublishCaseTypeVersionCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            case_type_version_id=second.case_type_version_id,
            correlation_id="c2",
        ),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        audit_store=audit,
        clock=clock,
    )
    assert publish_result.version_number == 2
    # Current version points at the latest publish.
    promoted = await case_type_repo.get(ACME_TENANT, first.case_type_id)
    assert promoted is not None
    assert promoted.current_version_id == second.case_type_version_id


# ---------------------------------------------------------------------------
# 2. Validator rejection
# ---------------------------------------------------------------------------


async def test_publish_case_type__missing_required_field__raises_validation(
    seeded_acme: tuple[UUID, InMemoryCaseTypeRepository, InMemoryCaseTypeVersionRepository],
    clock: FixedClock,
    audit: InMemoryAuditStore,
) -> None:
    """An invalid draft schema raises ``ValidationFailed`` at draft time
    and writes nothing. (The publish path also re-runs the validator;
    a defect raises before any state change.)"""
    _, case_type_repo, version_repo = seeded_acme

    bad_schema = CaseTypeSchema(
        fields=(),  # invalid — at least one field is required
        document_classifications=(),
        rules=(),
    )
    with pytest.raises(ValidationFailed):
        await draft_case_type.handle(
            draft_case_type.DraftCaseTypeVersionCommand(
                tenant_id=ACME_TENANT,
                actor_id=ADMIN_USER,
                case_type_key="ServiceRequest",
                schema=bad_schema,
                correlation_id="c-bad",
            ),
            case_type_repo=case_type_repo,
            case_type_version_repo=version_repo,
            audit_store=audit,
            clock=clock,
        )
    assert version_repo._by_id == {}
    assert audit.events == []
    assert case_type_repo.set_current_version_calls == []


async def test_publish_case_type__duplicate_field_key__raises_validation(
    seeded_acme: tuple[UUID, InMemoryCaseTypeRepository, InMemoryCaseTypeVersionRepository],
    clock: FixedClock,
    audit: InMemoryAuditStore,
) -> None:
    """Validator catches duplicate keys (FR-015 acceptance scenario)."""
    _, case_type_repo, version_repo = seeded_acme

    dup_schema = CaseTypeSchema(
        fields=(
            CaseTypeField(key="title", label="Title", type="text"),
            CaseTypeField(key="title", label="Title 2", type="text"),
        ),
        document_classifications=(),
        rules=(),
    )
    with pytest.raises(ValidationFailed) as excinfo:
        await draft_case_type.handle(
            draft_case_type.DraftCaseTypeVersionCommand(
                tenant_id=ACME_TENANT,
                actor_id=ADMIN_USER,
                case_type_key="ServiceRequest",
                schema=dup_schema,
                correlation_id="c-dup",
            ),
            case_type_repo=case_type_repo,
            case_type_version_repo=version_repo,
            audit_store=audit,
            clock=clock,
        )
    assert "duplicate" in str(excinfo.value).lower()
    assert version_repo._by_id == {}
    assert audit.events == []


# ---------------------------------------------------------------------------
# 3. Idempotent re-publish — published version UPDATE raises
# ---------------------------------------------------------------------------


async def test_publish_case_type__re_publish_published_version__raises_invariant(
    seeded_acme: tuple[UUID, InMemoryCaseTypeRepository, InMemoryCaseTypeVersionRepository],
    clock: FixedClock,
    audit: InMemoryAuditStore,
) -> None:
    """Once published, the version's status is no longer ``draft``.
    A second publish call must raise ``InvariantViolation`` and the
    repository must not issue another status flip (FR-015 / AC-007)."""
    _, case_type_repo, version_repo = seeded_acme

    draft_result = await draft_case_type.handle(
        draft_case_type.DraftCaseTypeVersionCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            case_type_key="ServiceRequest",
            schema=_healthy_schema(),
            correlation_id="c1",
        ),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        audit_store=audit,
        clock=clock,
    )
    await publish_case_type.handle(
        publish_case_type.PublishCaseTypeVersionCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            case_type_version_id=draft_result.case_type_version_id,
            correlation_id="c1",
        ),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        audit_store=audit,
        clock=clock,
    )
    initial_audit_count = len(audit.events)

    with pytest.raises(InvariantViolation):
        await publish_case_type.handle(
            publish_case_type.PublishCaseTypeVersionCommand(
                tenant_id=ACME_TENANT,
                actor_id=ADMIN_USER,
                case_type_version_id=draft_result.case_type_version_id,
                correlation_id="c2",
            ),
            case_type_repo=case_type_repo,
            case_type_version_repo=version_repo,
            audit_store=audit,
            clock=clock,
        )

    # No second status flip, no second publish audit row.
    assert version_repo.status_transitions == [
        (
            draft_result.case_type_version_id,
            CaseTypeVersionStatus.DRAFT,
            CaseTypeVersionStatus.PUBLISHED,
        )
    ]
    assert len(audit.events) == initial_audit_count


# ---------------------------------------------------------------------------
# 4. Tenant isolation — 2-tenant test
# ---------------------------------------------------------------------------


async def test_publish_case_type__cross_tenant_publish__raises_not_found_and_does_not_flip(
    clock: FixedClock,
    audit: InMemoryAuditStore,
) -> None:
    """Tenant isolation: a tenant B actor cannot publish a version that
    belongs to tenant A. The repository scopes the lookup by
    ``tenant_id``, so the version is invisible and ``NotFound`` is
    raised. No status flip, no audit row, no current-version promotion
    in either tenant."""
    # Seed two tenants, each with its own CaseType + draft version.
    _, acme_ctr, acme_vtr = _seed_case_type(tenant_id=ACME_TENANT)
    _, beta_ctr, beta_vtr = _seed_case_type(tenant_id=BETA_TENANT, key="ServiceRequest")

    # Tenant A drafts and would normally publish — we keep it as a draft
    # so we can verify tenant B cannot flip it.
    draft_result = await draft_case_type.handle(
        draft_case_type.DraftCaseTypeVersionCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            case_type_key="ServiceRequest",
            schema=_healthy_schema(),
            correlation_id="cA",
        ),
        case_type_repo=acme_ctr,
        case_type_version_repo=acme_vtr,
        audit_store=audit,
        clock=clock,
    )

    # Tenant B tries to publish tenant A's version id.
    with pytest.raises(NotFound):
        await publish_case_type.handle(
            publish_case_type.PublishCaseTypeVersionCommand(
                tenant_id=BETA_TENANT,
                actor_id=SUPERVISOR_USER,
                case_type_version_id=draft_result.case_type_version_id,
                correlation_id="cB",
            ),
            case_type_repo=beta_ctr,
            case_type_version_repo=beta_vtr,
            audit_store=audit,
            clock=clock,
        )

    # Tenant A's draft is still a draft — no status flip occurred in
    # either tenant.
    stored_a = await acme_vtr.get(ACME_TENANT, draft_result.case_type_version_id)
    assert stored_a is not None
    assert stored_a.status is CaseTypeVersionStatus.DRAFT
    assert acme_vtr.status_transitions == []
    assert beta_vtr.status_transitions == []
    # Tenant B's CaseType current_version_id was never set.
    beta_ct = await beta_ctr.get_by_key(BETA_TENANT, "ServiceRequest")
    assert beta_ct is not None
    assert beta_ct.current_version_id is None


async def test_publish_case_type__same_key_two_tenants__each_can_publish_own_version(
    clock: FixedClock,
    audit: InMemoryAuditStore,
) -> None:
    """Two tenants share a CaseType *key* (``ServiceRequest``) but live
    in different tenants. Each tenant's draft + publish flow stays
    inside its own tenant — no cross-tenant leakage in the
    repositories."""
    _, acme_ctr, acme_vtr = _seed_case_type(tenant_id=ACME_TENANT)
    _, beta_ctr, beta_vtr = _seed_case_type(tenant_id=BETA_TENANT, key="ServiceRequest")

    acme_draft = await draft_case_type.handle(
        draft_case_type.DraftCaseTypeVersionCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            case_type_key="ServiceRequest",
            schema=_healthy_schema(),
            correlation_id="cA",
        ),
        case_type_repo=acme_ctr,
        case_type_version_repo=acme_vtr,
        audit_store=audit,
        clock=clock,
    )
    beta_draft = await draft_case_type.handle(
        draft_case_type.DraftCaseTypeVersionCommand(
            tenant_id=BETA_TENANT,
            actor_id=SUPERVISOR_USER,
            case_type_key="ServiceRequest",
            schema=_healthy_schema(),
            correlation_id="cB",
        ),
        case_type_repo=beta_ctr,
        case_type_version_repo=beta_vtr,
        audit_store=audit,
        clock=clock,
    )

    # Publish each in its own tenant.
    acme_pub = await publish_case_type.handle(
        publish_case_type.PublishCaseTypeVersionCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            case_type_version_id=acme_draft.case_type_version_id,
            correlation_id="cA",
        ),
        case_type_repo=acme_ctr,
        case_type_version_repo=acme_vtr,
        audit_store=audit,
        clock=clock,
    )
    beta_pub = await publish_case_type.handle(
        publish_case_type.PublishCaseTypeVersionCommand(
            tenant_id=BETA_TENANT,
            actor_id=SUPERVISOR_USER,
            case_type_version_id=beta_draft.case_type_version_id,
            correlation_id="cB",
        ),
        case_type_repo=beta_ctr,
        case_type_version_repo=beta_vtr,
        audit_store=audit,
        clock=clock,
    )

    assert acme_pub.status is CaseTypeVersionStatus.PUBLISHED
    assert beta_pub.status is CaseTypeVersionStatus.PUBLISHED

    # Each tenant's CaseType points at its OWN published version, not
    # the other tenant's.
    acme_ct = await acme_ctr.get(ACME_TENANT, acme_pub.case_type_id)
    beta_ct = await beta_ctr.get(BETA_TENANT, beta_pub.case_type_id)
    assert acme_ct is not None
    assert beta_ct is not None
    assert acme_ct.current_version_id == acme_pub.case_type_version_id
    assert beta_ct.current_version_id == beta_pub.case_type_version_id
    assert acme_ct.current_version_id != beta_ct.current_version_id


# ---------------------------------------------------------------------------
# 5. Unknown version
# ---------------------------------------------------------------------------


async def test_publish_case_type__unknown_version_id__raises_not_found(
    seeded_acme: tuple[UUID, InMemoryCaseTypeRepository, InMemoryCaseTypeVersionRepository],
    clock: FixedClock,
    audit: InMemoryAuditStore,
) -> None:
    """A publish call referencing a non-existent version id returns
    ``NotFound``. No row mutation, no audit row."""
    _, case_type_repo, version_repo = seeded_acme

    with pytest.raises(NotFound):
        await publish_case_type.handle(
            publish_case_type.PublishCaseTypeVersionCommand(
                tenant_id=ACME_TENANT,
                actor_id=ADMIN_USER,
                case_type_version_id=uuid4(),
                correlation_id="c-nf",
            ),
            case_type_repo=case_type_repo,
            case_type_version_repo=version_repo,
            audit_store=audit,
            clock=clock,
        )
    assert version_repo.status_transitions == []
    assert audit.events == []
    assert case_type_repo.set_current_version_calls == []


async def test_publish_case_type__unknown_case_type_key_on_draft__raises_not_found(
    seeded_acme: tuple[UUID, InMemoryCaseTypeRepository, InMemoryCaseTypeVersionRepository],
    clock: FixedClock,
    audit: InMemoryAuditStore,
) -> None:
    """Drafting against an unknown case_type_key returns ``NotFound``."""
    _, case_type_repo, version_repo = seeded_acme

    with pytest.raises(NotFound):
        await draft_case_type.handle(
            draft_case_type.DraftCaseTypeVersionCommand(
                tenant_id=ACME_TENANT,
                actor_id=ADMIN_USER,
                case_type_key="DoesNotExist",
                schema=_healthy_schema(),
                correlation_id="c-nf",
            ),
            case_type_repo=case_type_repo,
            case_type_version_repo=version_repo,
            audit_store=audit,
            clock=clock,
        )
    assert version_repo._by_id == {}
    assert audit.events == []


# ---------------------------------------------------------------------------
# 6. Publication history (FR-033 / P10-US4) — the response backing
# `GET /api/v1/admin/case-types/{key}/versions`.
# ---------------------------------------------------------------------------


async def test_publication_history__returns_every_version_newest_first_with_metadata(
    seeded_acme: tuple[UUID, InMemoryCaseTypeRepository, InMemoryCaseTypeVersionRepository],
    clock: FixedClock,
    audit: InMemoryAuditStore,
) -> None:
    """The history use case returns every CaseTypeVersion for the
    CaseType, newest-first, with field_keys and
    document_classifications as a fingerprint. This is what the admin
    router (`interfaces/http/routers/admin_case_types.py`) maps to
    `GET /api/v1/admin/case-types/{key}/versions` (FR-033)."""
    case_type_id, case_type_repo, version_repo = seeded_acme

    # Draft + publish version 1.
    v1 = await draft_case_type.handle(
        draft_case_type.DraftCaseTypeVersionCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            case_type_key="ServiceRequest",
            schema=_healthy_schema(),
            correlation_id="c1",
        ),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        audit_store=audit,
        clock=clock,
    )
    await publish_case_type.handle(
        publish_case_type.PublishCaseTypeVersionCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            case_type_version_id=v1.case_type_version_id,
            correlation_id="c1",
        ),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        audit_store=audit,
        clock=clock,
    )

    # Draft + leave as draft for version 2.
    v2 = await draft_case_type.handle(
        draft_case_type.DraftCaseTypeVersionCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            case_type_key="ServiceRequest",
            schema=_healthy_schema(),
            correlation_id="c2",
        ),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        audit_store=audit,
        clock=clock,
    )

    entries = await history_handle(
        PublicationHistoryQuery(tenant_id=ACME_TENANT, case_type_id=case_type_id),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
    )

    assert [e.version_number for e in entries] == [2, 1]
    assert entries[0].status is CaseTypeVersionStatus.DRAFT
    assert entries[0].id == v2.case_type_version_id
    assert entries[1].status is CaseTypeVersionStatus.PUBLISHED
    assert entries[1].id == v1.case_type_version_id
    assert entries[1].published_by == ADMIN_USER

    # Fingerprint columns are present.
    for entry in entries:
        assert "title" in entry.field_keys
        assert "priority" in entry.field_keys
        assert "Correspondence" in entry.document_classifications


async def test_publication_history__cross_tenant_history_lookup__raises_not_found(
    seeded_acme: tuple[UUID, InMemoryCaseTypeRepository, InMemoryCaseTypeVersionRepository],
    clock: FixedClock,
    audit: InMemoryAuditStore,
) -> None:
    """A history request scoped to tenant B for an ACME CaseType must
    return ``NotFound`` — no leakage of the CaseType id across tenants."""
    case_type_id, case_type_repo, version_repo = seeded_acme

    with pytest.raises(NotFound):
        await history_handle(
            PublicationHistoryQuery(tenant_id=BETA_TENANT, case_type_id=case_type_id),
            case_type_repo=case_type_repo,
            case_type_version_repo=version_repo,
        )


# ---------------------------------------------------------------------------
# 7. Draft on unknown tenant key — version id never resolves
# ---------------------------------------------------------------------------


async def test_publish_case_type__version_id_from_other_tenant__raises_not_found(
    seeded_acme: tuple[UUID, InMemoryCaseTypeRepository, InMemoryCaseTypeVersionRepository],
    clock: FixedClock,
    audit: InMemoryAuditStore,
) -> None:
    """An attacker who guesses a tenant A version id and submits it as
    a tenant B publish request gets ``NotFound``. Tenant B's
    ``get(tenant_id, version_id)`` ignores the row because
    ``v.tenant_id != tenant_id``."""
    _, case_type_repo, version_repo = seeded_acme

    # Seed a tenant A draft.
    draft_result = await draft_case_type.handle(
        draft_case_type.DraftCaseTypeVersionCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            case_type_key="ServiceRequest",
            schema=_healthy_schema(),
            correlation_id="c1",
        ),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        audit_store=audit,
        clock=clock,
    )

    # Publish lookup under tenant B with the same version id.
    with pytest.raises(NotFound):
        await publish_case_type.handle(
            publish_case_type.PublishCaseTypeVersionCommand(
                tenant_id=BETA_TENANT,
                actor_id=SUPERVISOR_USER,
                case_type_version_id=draft_result.case_type_version_id,
                correlation_id="c-cross",
            ),
            case_type_repo=case_type_repo,
            case_type_version_repo=version_repo,
            audit_store=audit,
            clock=clock,
        )
    # No status flip on the ACME draft.
    stored = await version_repo.get(ACME_TENANT, draft_result.case_type_version_id)
    assert stored is not None
    assert stored.status is CaseTypeVersionStatus.DRAFT
