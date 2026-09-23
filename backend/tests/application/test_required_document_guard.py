"""Application tests for the required-document-requirements guard (P03-US5 / FM-024).

The document-requirements guard is the v1 expression of Pillar P03 §4
"P03-US5: a case cannot transition past a state unless the required
documents are present." It is evaluated by the same rules engine as
every other workflow guard, but it touches a new context field —
``case.documents_by_classification.<X>`` — and therefore exercises the
plumbing that the runtime use case added in this slice:

- ``validate_definition`` rejects a guard whose ``classification`` is
  empty, not a string, or not in the case-type's allowlist.
- ``execute_transition`` resolves the per-classification count from
  ``DocumentRepository.list_for_case`` and surfaces a
  ``GuardBlocked(reason="missing_documents")`` with a safe details
  block that names the missing classification.
- ``simulate_workflow.simulate`` renders the document clause in the
  per-step guard trace so an administrator can preview a workflow
  against a synthetic case before publication.

Acceptance scenarios (AGENTS.md §4.R5):

1. The transition is blocked when the case has no documents at all.
2. The transition is blocked when the case has documents but none of
   the required classification.
3. The transition succeeds when at least one document of the required
   classification is present.
4. The structured failure carries a safe explanation
   (``reason="missing_documents"`` + ``details.classification``).
5. The simulator renders the per-clause document guard in the trace.

Naming follows AGENTS.md §4.R5: tests are sortable
(``pytest --collect-only``) and filterable (``-k``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.authorization import (
    Action,
    Policy,
    Resource,
    authorize,
    configure_default_policy,
)
from app.application.configuration.validate_definition import validate_definition
from app.application.workflow import execute_transition as transition_uc
from app.application.workflow.execute_transition import (
    ExecuteTransitionCommand,
    GuardBlocked,
)
from app.application.workflow.simulate_workflow import simulate
from app.domain.case import Case, CaseReference
from app.domain.case_type import (
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.document import Document, DocumentSensitivity
from app.domain.errors import ValidationFailed
from app.domain.workflow import (
    WorkflowDefinition,
    WorkflowState,
    WorkflowTransition,
    WorkflowVersion,
)
from tests.application.workflow._execute_transition_fakes import (
    FixedClock,
    InMemoryAuditStore,
    InMemoryCaseRepository,
    InMemoryOutboxWriter,
    InMemoryTaskRepository,
    InMemoryWorkflowInstanceRepository,
    InMemoryWorkflowVersionRepository,
    StoredInstance,
)

# ---------------------------------------------------------------------------
# Constants — two distinct tenants for the tenant-isolation test
# ---------------------------------------------------------------------------


ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
BETA_TENANT = UUID("22222222-2222-2222-2222-222222222222")

REVIEWER_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

# The synthetic case type declares three classifications; only
# ``Contract`` is required for the test transition to fire.
SYNTHETIC_CLASSIFICATIONS = ("Contract", "Correspondence", "Evidence")


# ---------------------------------------------------------------------------
# Policy fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_default_policy() -> None:
    """Reset the policy singleton around every test.

    Why: the default policy is a process singleton
    (``app.application.authorization._default_policy``). Tests that
    call ``configure_default_policy`` would leak into the next test
    without this guard.
    """
    from app.application import authorization as authz_mod  # noqa: PLC0415

    original = authz_mod._default_policy
    configure_default_policy(Policy())
    yield
    configure_default_policy(original)


# ---------------------------------------------------------------------------
# Document fakes
# ---------------------------------------------------------------------------


class InMemoryDocumentRepository:
    """In-memory ``DocumentRepository`` for required-document-guard tests.

    Mirrors the slice of the port the runtime guard evaluator uses —
    only ``list_for_case`` is exercised by the guard. The fake is
    deterministic: ``list_for_case`` returns the documents that were
    staged via :meth:`add`, in the order they were added.
    """

    def __init__(self) -> None:
        self._by_case: dict[UUID, list[Document]] = {}
        self.list_calls: list[tuple[UUID, UUID]] = []

    def add(self, document: Document) -> None:
        self._by_case.setdefault(document.case_id, []).append(document)

    async def list_for_case(self, *, tenant_id: UUID, case_id: UUID) -> Sequence[Document]:
        self.list_calls.append((tenant_id, case_id))
        return list(self._by_case.get(case_id, ()))


def _make_document(
    *,
    tenant_id: UUID,
    case_id: UUID,
    classification: str,
    file_name: str = "contract.pdf",
) -> Document:
    """Build a valid Document aggregate for tests.

    The Document aggregate insists on a real SHA-256 + storage ref;
    this helper bakes in synthetic values so the tests stay readable.
    """
    return Document(
        id=uuid4(),
        tenant_id=tenant_id,
        case_id=case_id,
        file_name=file_name,
        content_type="application/pdf",
        classification=classification,
        sensitivity=DocumentSensitivity.INTERNAL,
        sha256="0" * 64,
        size=1024,
        current_version=1,
        storage_ref=f"local://documents/{case_id}/{file_name}",
        uploaded_by=REVIEWER_USER,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )


# ---------------------------------------------------------------------------
# Workflow builders
# ---------------------------------------------------------------------------


def _build_guarded_workflow(
    *,
    tenant_id: UUID,
    classification: str = "Contract",
    min_count: int = 1,
) -> tuple[WorkflowDefinition, WorkflowVersion]:
    """Build a published workflow with a single document-requirements guard.

    The ``submit_for_review`` transition requires at least ``min_count``
    documents of ``classification`` to be attached to the case. Other
    transitions are unguarded so the tests can pin the case to any
    state without ceremony.
    """
    states: tuple[WorkflowState, ...] = (
        WorkflowState(key="intake", label="Intake", is_initial=True),
        WorkflowState(key="in_review", label="In review"),
        WorkflowState(key="closed", label="Closed", is_terminal=True),
    )
    guarded = WorkflowTransition(
        key="submit_for_review",
        from_state="intake",
        to_state="in_review",
        human_driven=True,
        guard={
            "op": "exists",
            "field": f"case.documents_by_classification.{classification}",
            "min": min_count,
            "classification": classification,
        },
    )
    transitions: tuple[WorkflowTransition, ...] = (
        guarded,
        WorkflowTransition(
            key="approve",
            from_state="in_review",
            to_state="closed",
            human_driven=True,
        ),
    )
    definition = WorkflowDefinition.create(
        tenant_id=tenant_id,
        key="ServiceRequestWorkflow",
        name="Service Request Workflow",
    )
    version = WorkflowVersion.draft(
        tenant_id=tenant_id,
        workflow_definition_id=definition.id,
        version_number=1,
        states=states,
        transitions=transitions,
    )
    published = version.publish(actor_id=REVIEWER_USER, at=FIXED_NOW)
    return definition, published


def _build_case(
    *,
    tenant_id: UUID,
    case_type_version: CaseTypeVersion,
) -> Case:
    """Build a synthetic case pinned to ``intake``."""
    return Case.create(
        tenant_id=tenant_id,
        reference=CaseReference(value=f"C-ACME-2026-{uuid4().int % 1_000_000:06d}"),
        case_type_version=case_type_version,
        title="Service request",
        description="",
        data={"title": "Service request"},
        priority="normal",
        created_by=REVIEWER_USER,
        now=FIXED_NOW,
        sensitivity="internal",
    )


def _build_case_type_version(*, tenant_id: UUID) -> CaseTypeVersion:
    """Build a published CaseTypeVersion with the synthetic classifications."""
    return CaseTypeVersion(
        id=uuid4(),
        tenant_id=tenant_id,
        case_type_id=uuid4(),
        version_number=1,
        schema=CaseTypeSchema(
            fields=(CaseTypeField(key="title", label="Title", type="text", required=True),),
            document_classifications=SYNTHETIC_CLASSIFICATIONS,
        ),
        status=CaseTypeVersionStatus.PUBLISHED,
        published_at=FIXED_NOW,
        published_by=REVIEWER_USER,
    )


# ---------------------------------------------------------------------------
# Seeded fixture — fully-wired case + workflow + repositories
# ---------------------------------------------------------------------------


@dataclass
class _Seed:
    case: Case
    case_id: UUID
    case_repo: InMemoryCaseRepository
    task_repo: InMemoryTaskRepository
    instance_repo: InMemoryWorkflowInstanceRepository
    workflow_version_repo: InMemoryWorkflowVersionRepository
    document_repo: InMemoryDocumentRepository
    definition: WorkflowDefinition
    version: WorkflowVersion
    instance: StoredInstance


async def _seed_guarded_case(
    *,
    tenant_id: UUID = ACME_TENANT,
    classification: str = "Contract",
    min_count: int = 1,
    with_documents: tuple[tuple[str, str], ...] = (),
) -> _Seed:
    """Wire a fully-wired case + guarded workflow for the runtime tests.

    ``with_documents`` is a sequence of ``(file_name, classification)``
    pairs the test wants staged on the case. Empty by default — the
    "missing" path tests rely on that.
    """
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    instance_repo = InMemoryWorkflowInstanceRepository()
    workflow_version_repo = InMemoryWorkflowVersionRepository()
    document_repo = InMemoryDocumentRepository()

    definition, version = _build_guarded_workflow(
        tenant_id=tenant_id,
        classification=classification,
        min_count=min_count,
    )
    case_type_version = _build_case_type_version(tenant_id=tenant_id)
    case = _build_case(tenant_id=tenant_id, case_type_version=case_type_version)
    case_repo.add(case)
    for file_name, cls in with_documents:
        document_repo.add(
            _make_document(
                tenant_id=tenant_id,
                case_id=case.id,
                classification=cls,
                file_name=file_name,
            )
        )
    instance = StoredInstance(
        id=uuid4(),
        tenant_id=tenant_id,
        case_id=case.id,
        workflow_definition_id=definition.id,
        workflow_version_id=version.id,
        current_state="intake",
        correlation_id="corr-doc-guard",
        version_token=1,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
    instance_repo.add(instance)
    # The runtime resolves the workflow version from the repository
    # keyed by the instance's ``workflow_version_id``; stage it
    # explicitly so the runtime lookup hits.
    await workflow_version_repo.add(version)
    return _Seed(
        case=case,
        case_id=case.id,
        case_repo=case_repo,
        task_repo=task_repo,
        instance_repo=instance_repo,
        workflow_version_repo=workflow_version_repo,
        document_repo=document_repo,
        definition=definition,
        version=version,
        instance=instance,
    )


# ---------------------------------------------------------------------------
# Validator tests — guard shape and allowlist cross-check
# ---------------------------------------------------------------------------


def test_validate_definition__document_guard_with_known_classification__accepts() -> None:
    """The validator accepts a well-formed document-requirements guard.

    The guard references ``case.documents_by_classification.Contract``
    and the case-type allowlist contains ``Contract``; the validator's
    cross-check is satisfied.
    """
    _, version = _build_guarded_workflow(
        tenant_id=ACME_TENANT, classification="Contract", min_count=1
    )
    # No exception: the guard shape and the allowlist are both clean.
    validate_definition(
        version,
        known_queues=(),
        known_classifications=SYNTHETIC_CLASSIFICATIONS,
    )


def test_validate_definition__unknown_document_classification__reports_field_path() -> None:
    """The validator rejects a guard that references a classification
    the tenant has not configured.

    The defect must surface with a precise ``field=`` path so the HTTP
    layer can render a targeted message that names the offending
    transition + ``classification`` key.
    """
    _, version = _build_guarded_workflow(
        tenant_id=ACME_TENANT, classification="Contsract", min_count=1
    )
    with pytest.raises(ValidationFailed) as exc_info:
        validate_definition(
            version,
            known_queues=(),
            known_classifications=SYNTHETIC_CLASSIFICATIONS,
        )
    assert exc_info.value.field is not None
    assert exc_info.value.field.endswith(".classification")
    assert "Contsract" in str(exc_info.value)


def test_validate_definition__document_guard_with_empty_classification__rejects_shape() -> None:
    """The validator rejects a document guard with an empty classification
    even when the allowlist cross-check is disabled.

    The shape check is always on: a workflow with an empty
    ``classification`` field would degrade to "no document required"
    at runtime, which is the wrong default.
    """
    _, version = _build_guarded_workflow(
        tenant_id=ACME_TENANT, classification="Contract", min_count=1
    )
    # Rebuild the version with an empty classification in the guard.
    empty_guard = {
        "op": "exists",
        "field": "case.documents_by_classification.Contract",
        "min": 1,
        "classification": "   ",
    }
    guarded = WorkflowTransition(
        key="submit_for_review",
        from_state="intake",
        to_state="in_review",
        human_driven=True,
        guard=empty_guard,
    )
    states: tuple[WorkflowState, ...] = (
        WorkflowState(key="intake", label="Intake", is_initial=True),
        WorkflowState(key="in_review", label="In review"),
        WorkflowState(key="closed", label="Closed", is_terminal=True),
    )
    broken_version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=version.workflow_definition_id,
        version_number=2,
        states=states,
        transitions=(
            guarded,
            WorkflowTransition(
                key="approve",
                from_state="in_review",
                to_state="closed",
                human_driven=True,
            ),
        ),
    )
    with pytest.raises(ValidationFailed) as exc_info:
        validate_definition(
            broken_version,
            known_queues=(),
            known_classifications=(),
        )
    assert exc_info.value.field is not None
    assert exc_info.value.field.endswith(".classification")


# ---------------------------------------------------------------------------
# Runtime tests — execute_transition document guard evaluation
# ---------------------------------------------------------------------------


async def test_execute_transition__missing_documents__blocks_with_missing_documents_reason(
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
    clock: FixedClock,
) -> None:
    """The transition is blocked when the case has no documents at all.

    The runtime surfaces ``reason="missing_documents"`` and a safe
    ``details`` payload that names the missing classification. No
    partial commit lands (Constitution III) — the audit and outbox
    are empty.
    """
    seed = await _seed_guarded_case(tenant_id=ACME_TENANT)
    await authorize(
        Action.CASE_TRANSITION,
        _actor(roles=("reviewer",)),
        Resource(tenant_id=ACME_TENANT, kind="case", id=seed.case_id),
    )

    with pytest.raises(GuardBlocked) as exc_info:
        await transition_uc.handle(
            ExecuteTransitionCommand(
                tenant_id=ACME_TENANT,
                actor_id=REVIEWER_USER,
                actor_roles=("reviewer",),
                case_id=seed.case_id,
                transition_key="submit_for_review",
                correlation_id="corr-missing-all",
            ),
            case_repo=seed.case_repo,
            instance_repo=seed.instance_repo,
            workflow_version_repo=seed.workflow_version_repo,
            task_repo=seed.task_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
            document_repo=seed.document_repo,
        )

    assert exc_info.value.reason == "missing_documents"
    assert exc_info.value.details.get("classification") == "Contract"
    assert exc_info.value.details.get("field") == ("case.documents_by_classification.Contract")
    assert exc_info.value.details.get("min") == 1
    # No partial commit.
    assert audit.events == []
    assert outbox.records == []
    # The document repo was actually consulted (not a silent fallback).
    assert seed.document_repo.list_calls == [(ACME_TENANT, seed.case_id)]


async def test_execute_transition__wrong_classification_only__blocks(
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
    clock: FixedClock,
) -> None:
    """A case that has documents of the wrong classification is still blocked.

    The guard looks for ``Contract``; a single ``Correspondence``
    document is present; the runtime must surface
    ``reason="missing_documents"`` for the required classification.
    """
    seed = await _seed_guarded_case(
        tenant_id=ACME_TENANT,
        with_documents=(("letter.eml", "Correspondence"),),
    )
    await authorize(
        Action.CASE_TRANSITION,
        _actor(roles=("reviewer",)),
        Resource(tenant_id=ACME_TENANT, kind="case", id=seed.case_id),
    )

    with pytest.raises(GuardBlocked) as exc_info:
        await transition_uc.handle(
            ExecuteTransitionCommand(
                tenant_id=ACME_TENANT,
                actor_id=REVIEWER_USER,
                actor_roles=("reviewer",),
                case_id=seed.case_id,
                transition_key="submit_for_review",
                correlation_id="corr-wrong-class",
            ),
            case_repo=seed.case_repo,
            instance_repo=seed.instance_repo,
            workflow_version_repo=seed.workflow_version_repo,
            task_repo=seed.task_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
            document_repo=seed.document_repo,
        )

    assert exc_info.value.reason == "missing_documents"
    assert exc_info.value.details.get("classification") == "Contract"


async def test_execute_transition__contract_present__advances_case(
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
    clock: FixedClock,
) -> None:
    """A case with at least one Contract-classified document advances.

    Mirrors the success path of the existing execute_transition
    tests: the case moves to ``in_review``, the instance pointer is
    bumped, the audit + outbox rows land in the same transaction, and
    the document repository was consulted exactly once.
    """
    seed = await _seed_guarded_case(
        tenant_id=ACME_TENANT,
        with_documents=(
            ("msa.pdf", "Contract"),
            ("letter.eml", "Correspondence"),
        ),
    )
    await authorize(
        Action.CASE_TRANSITION,
        _actor(roles=("reviewer",)),
        Resource(tenant_id=ACME_TENANT, kind="case", id=seed.case_id),
    )

    result = await transition_uc.handle(
        ExecuteTransitionCommand(
            tenant_id=ACME_TENANT,
            actor_id=REVIEWER_USER,
            actor_roles=("reviewer",),
            case_id=seed.case_id,
            transition_key="submit_for_review",
            correlation_id="corr-contract-present",
        ),
        case_repo=seed.case_repo,
        instance_repo=seed.instance_repo,
        workflow_version_repo=seed.workflow_version_repo,
        task_repo=seed.task_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
        document_repo=seed.document_repo,
    )

    assert result.to_state == "in_review"
    assert result.from_state == "intake"
    stored = await seed.case_repo.get(ACME_TENANT, seed.case_id)
    assert stored is not None
    assert stored.current_workflow_state == "in_review"
    assert seed.document_repo.list_calls == [(ACME_TENANT, seed.case_id)]
    assert len(audit.events) == 1
    assert len(outbox.records) == 1


async def test_execute_transition__document_repo_missing__blocks_fail_closed(
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
    clock: FixedClock,
) -> None:
    """A document-requirements guard without a document repository fails closed.

    The runtime never silently accepts a guard it cannot evaluate
    (Constitution V / R12). The use case raises ``GuardBlocked`` with
    ``reason="guard_invalid"`` and a field marker so the operator
    knows to wire a ``DocumentRepository``.
    """
    seed = await _seed_guarded_case(tenant_id=ACME_TENANT)
    await authorize(
        Action.CASE_TRANSITION,
        _actor(roles=("reviewer",)),
        Resource(tenant_id=ACME_TENANT, kind="case", id=seed.case_id),
    )

    with pytest.raises(GuardBlocked) as exc_info:
        await transition_uc.handle(
            ExecuteTransitionCommand(
                tenant_id=ACME_TENANT,
                actor_id=REVIEWER_USER,
                actor_roles=("reviewer",),
                case_id=seed.case_id,
                transition_key="submit_for_review",
                correlation_id="corr-no-repo",
            ),
            case_repo=seed.case_repo,
            instance_repo=seed.instance_repo,
            workflow_version_repo=seed.workflow_version_repo,
            task_repo=seed.task_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
            document_repo=None,
        )

    assert exc_info.value.reason == "guard_invalid"
    assert audit.events == []
    assert outbox.records == []


async def test_execute_transition__tenant_isolation__blocker_does_not_leak_documents(
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
    clock: FixedClock,
) -> None:
    """The blocker's details never include the case's actual document list.

    Cross-tenant actors are denied at the repository layer, but the
    test specifically asserts that the structured exception names the
    missing classification (which is part of the guard, not the case)
    and does not echo the case's document set.
    """
    seed = await _seed_guarded_case(
        tenant_id=ACME_TENANT,
        with_documents=(("letter.eml", "Correspondence"),),
    )
    await authorize(
        Action.CASE_TRANSITION,
        _actor(roles=("reviewer",)),
        Resource(tenant_id=ACME_TENANT, kind="case", id=seed.case_id),
    )

    with pytest.raises(GuardBlocked) as exc_info:
        await transition_uc.handle(
            ExecuteTransitionCommand(
                tenant_id=ACME_TENANT,
                actor_id=REVIEWER_USER,
                actor_roles=("reviewer",),
                case_id=seed.case_id,
                transition_key="submit_for_review",
                correlation_id="corr-no-leak",
            ),
            case_repo=seed.case_repo,
            instance_repo=seed.instance_repo,
            workflow_version_repo=seed.workflow_version_repo,
            task_repo=seed.task_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
            document_repo=seed.document_repo,
        )

    blob = str(exc_info.value.details)
    assert "letter.eml" not in blob
    assert "Correspondence" not in blob
    # The guard-level metadata is the only payload that may surface.
    assert exc_info.value.details.get("classification") == "Contract"


# ---------------------------------------------------------------------------
# Simulator tests — guard evaluation trace (P10 §3)
# ---------------------------------------------------------------------------


def _synthetic_case_with_classification(*, classification_counts: dict[str, int]) -> dict[str, Any]:
    """Build a synthetic case the simulator can walk.

    The simulator's guard evaluator looks up
    ``case.documents_by_classification.<X>`` against a nested mapping
    in the synthetic case. Missing keys evaluate to ``None`` (and
    therefore ``False`` for ``exists``) — the natural "missing
    document" rendering.
    """
    return {
        "case": {
            "id": "case-1",
            "tenant_id": str(ACME_TENANT),
            "documents_by_classification": dict(classification_counts),
            "documents": sum(classification_counts.values()),
            "participants": 1,
            "tasks": 0,
        }
    }


def test_simulate__missing_contract__blocks_and_renders_failed_guard() -> None:
    """The simulator stops at the document-requirements guard and
    surfaces the failing clause in the trace.

    The synthetic case has zero Contract documents; the simulator
    records one ``SimulatedStep`` (the attempted transition) — none,
    because the failure happens before any step — and a
    ``SimulationBlocker(kind="guard_failed", ...)`` whose
    ``failed_guard`` carries the rule's ``field`` and ``value`` (the
    resolved count, ``0``) so the admin UI can render a targeted
    explanation.
    """
    _, version = _build_guarded_workflow(
        tenant_id=ACME_TENANT, classification="Contract", min_count=1
    )
    synthetic_case = _synthetic_case_with_classification(
        classification_counts={"Correspondence": 1}
    )

    result = simulate(version, synthetic_case)

    assert result.outcome == "blocked"
    assert result.final_state == "intake"
    assert len(result.blockers) == 1
    blocker = result.blockers[0]
    assert blocker.kind == "guard_failed"
    assert blocker.transition == "submit_for_review"
    assert blocker.failed_guard is not None
    assert blocker.failed_guard.rule["field"] == "case.documents_by_classification.Contract"
    # The synthetic case has no ``Contract`` key, so the resolved value
    # is ``None``. The simulator surfaces the resolved value verbatim in
    # the trace so the admin UI can render a precise explanation.
    assert blocker.failed_guard.inputs["value"] is None
    assert blocker.failed_guard.result is False


def test_simulate__contract_present__completes_vertical() -> None:
    """The simulator advances to the terminal state when the required
    Contract document is present.

    The trace shows the guarded transition firing with a single
    successful guard clause. The block list is empty.
    """
    _, version = _build_guarded_workflow(
        tenant_id=ACME_TENANT, classification="Contract", min_count=1
    )
    synthetic_case = _synthetic_case_with_classification(
        classification_counts={"Contract": 1, "Correspondence": 2}
    )

    result = simulate(version, synthetic_case)

    assert result.outcome == "completed"
    assert result.final_state == "closed"
    assert result.blockers == ()
    # Two steps: ``submit_for_review`` (guarded, evaluated), then
    # ``approve`` (unguarded).
    transitions_fired = [step.transition for step in result.steps]
    assert transitions_fired == ["submit_for_review", "approve"]
    # The first step's guard trace carries the rule + the resolved
    # value so the admin UI can render the per-clause explanation.
    first_step = result.steps[0]
    assert len(first_step.guards) == 1
    assert first_step.guards[0].result is True
    assert first_step.guards[0].inputs["value"] == 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


@pytest.fixture
def audit() -> InMemoryAuditStore:
    return InMemoryAuditStore()


@pytest.fixture
def outbox() -> InMemoryOutboxWriter:
    return InMemoryOutboxWriter()


def _actor(*, roles: tuple[str, ...]) -> Any:
    """Build a duck-typed UserContext for ``authorize``.

    The application layer's policy accepts a structural ``UserContext``
    with ``user_id`` + ``tenant_id`` + ``roles``. We avoid importing
    the FastAPI-bound ``app.deps.UserContext`` here to keep the test
    independent of the web layer.
    """

    @dataclass
    class _Stub:
        user_id: UUID = REVIEWER_USER
        tenant_id: UUID = ACME_TENANT
        roles: tuple[str, ...] = ()
        is_system: bool = False

    return _Stub(roles=roles)
