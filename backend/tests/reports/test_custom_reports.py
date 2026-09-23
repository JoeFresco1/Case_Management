"""Tests for custom report designer and audited CSV export (P12-US6, US7 / FM-053).

Covers the acceptance criteria:

- An admin can define a custom report with groupings, aggregations,
  filters, and a time window; the persisted definition carries the
  closed-vocabulary fields and the actor + tenant metadata.
- The closed-vocabulary validator rejects unknown group-by fields,
  unknown aggregations, unknown operators for the chosen field, and
  malformed ``IN`` values.
- An authorized export renders the spec against the operational read
  models, returns a CSV body, and appends a single ``AuditExported``
  row to the audit chain.
- An unauthorized export (no ``audit.export``) raises ``Forbidden``
  and does not write an audit row.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per
AGENTS.md §4.R5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.reports import custom_reports, operational_reports
from app.deps import UserContext
from app.domain.audit import AUDIT_EXPORTED
from app.domain.case import Case, CaseReference, CaseStatus
from app.domain.errors import Forbidden, NotFound, ValidationFailed
from app.domain.queue import AssignmentPolicy, Queue

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")

NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes — local in-memory implementations.
# ---------------------------------------------------------------------------


@dataclass
class FakeCase:
    id: UUID
    tenant_id: UUID
    status: CaseStatus = CaseStatus.OPEN
    priority: str = "normal"
    owner_queue_id: str | None = None
    overdue_seconds: int = 0
    updated_at: datetime = field(default_factory=lambda: NOW)


class InMemoryCaseRepository:
    def __init__(self) -> None:
        self._cases: dict[tuple[UUID, UUID], Case] = {}
        self._fake: dict[tuple[UUID, UUID], FakeCase] = {}

    def seed(self, case: Case) -> None:
        self._cases[(case.tenant_id, case.id)] = case

    def seed_fake(self, fake: FakeCase) -> None:
        self._fake[(fake.tenant_id, fake.id)] = fake

    async def add(self, case: Case) -> None:
        self._cases[(case.tenant_id, case.id)] = case

    async def update(self, case: Case) -> None:
        self._cases[(case.tenant_id, case.id)] = case

    async def get(self, tenant_id: UUID, case_id: UUID) -> Case | None:
        return self._cases.get((tenant_id, case_id))

    async def get_by_reference(self, tenant_id: UUID, reference: CaseReference) -> Case | None:
        for c in self._cases.values():
            if c.tenant_id == tenant_id and c.reference == reference:
                return c
        return None

    async def list(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Case]:
        return list(self._cases.values())[:limit]

    async def next_reference(
        self, tenant_id: UUID, *, now: datetime, type_key: str | None = None
    ) -> CaseReference:
        return CaseReference(f"C-ACME-{now.year}-{(uuid4().int % 1_000_000):06d}")

    async def list_open_for_queues(
        self,
        tenant_id: UUID,
        *,
        queue_keys: tuple[str, ...],
        limit: int = 200,
    ) -> list[Case]:
        # The test layer wires the queue join through
        # ``_open_case_ids`` (the SQL adapter's ``INNER JOIN`` happens
        # at the persistence layer; the Case aggregate does not carry
        # ``owner_queue_id`` in v1).
        _ = queue_keys  # the test fake uses ``_open_case_ids`` directly
        wanted = set(getattr(self, "_open_case_ids", ()))
        return [
            c
            for (tid, _), c in self._cases.items()
            if tid == tenant_id and c.status is CaseStatus.OPEN and c.id in wanted
        ][:limit]


class InMemoryQueueRepository:
    def __init__(self) -> None:
        self._queues: dict[tuple[UUID, UUID], Queue] = {}
        self._by_key: dict[tuple[UUID, str], Queue] = {}

    def seed(self, queue: Queue) -> None:
        self._queues[(queue.tenant_id, queue.id)] = queue
        self._by_key[(queue.tenant_id, queue.key)] = queue

    async def list_keys(self, tenant_id: UUID) -> list[str]:
        return sorted(q.key for (tid, _), q in self._queues.items() if tid == tenant_id)

    async def list_queues(self, tenant_id: UUID) -> list[Queue]:
        return sorted(
            (q for (tid, _), q in self._queues.items() if tid == tenant_id),
            key=lambda q: q.key,
        )

    async def get(self, tenant_id: UUID, queue_id: UUID) -> Queue | None:
        return self._queues.get((tenant_id, queue_id))

    async def get_by_key(self, tenant_id: UUID, key: str) -> Queue | None:
        return self._by_key.get((tenant_id, key))

    async def add(self, queue: Queue) -> None:
        self._queues[(queue.tenant_id, queue.id)] = queue
        self._by_key[(queue.tenant_id, queue.key)] = queue

    async def update(self, queue: Queue) -> None:
        self._queues[(queue.tenant_id, queue.id)] = queue
        self._by_key[(queue.tenant_id, queue.key)] = queue

    async def delete(self, tenant_id: UUID, queue_id: UUID) -> None:
        q = self._queues.pop((tenant_id, queue_id), None)
        if q is not None:
            self._by_key.pop((tenant_id, q.key), None)


class InMemoryAuditStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def append(self, **kwargs: Any) -> None:
        row = dict(kwargs)
        row["id"] = uuid4()
        self.rows.append(row)


def _noop_dedupe(*_args: Any, **_kwargs: Any) -> bool:
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin(tenant_id: UUID = TENANT_A) -> UserContext:
    return UserContext(
        user_id=uuid4(),
        tenant_id=tenant_id,
        display_name="admin",
        roles=("process_admin",),
    )


def _reviewer(tenant_id: UUID = TENANT_A) -> UserContext:
    return UserContext(
        user_id=uuid4(),
        tenant_id=tenant_id,
        display_name="reviewer",
        roles=("reviewer",),
    )


def _build_case(
    *,
    tenant_id: UUID,
    priority: str = "normal",
) -> Case:
    return Case(
        id=uuid4(),
        tenant_id=tenant_id,
        reference=CaseReference(f"C-ACME-{NOW.year}-{(uuid4().int % 1_000_000):06d}"),
        case_type_version_id=uuid4(),
        status=CaseStatus.OPEN,
        priority=priority,
        title="Sample case",
        description="",
        data={},
        current_workflow_state=None,
        version_token=1,
        created_at=NOW,
        updated_at=NOW,
        created_by=uuid4(),
    )


def _make_clock(now: datetime) -> Any:
    class _Clock:
        def now(self) -> datetime:
            return now

    return _Clock()


# ---------------------------------------------------------------------------
# define_custom_report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_define_custom_report__persists_grouping_and_aggregation_spec() -> None:
    """Admin defines a custom report; the persisted row carries every
    closed-vocabulary field plus the actor + tenant metadata.
    """
    admin = _admin()
    repo = custom_reports.ReportDefinitionRepository()
    cmd = custom_reports.DefineCustomReportCommand(
        tenant_id=admin.tenant_id,
        actor_id=admin.user_id,
        actor_roles=admin.roles,
        key="open-by-priority",
        name="Open cases by priority",
        description="Counts open cases grouped by priority.",
        group_by=(custom_reports.GroupByField.CASE_PRIORITY,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(
            custom_reports.FilterClause(
                field="case.status",
                op=custom_reports.FilterOperator.EQ,
                value="open",
            ),
        ),
        time_window_hours=24,
    )

    definition = await custom_reports.define_custom_report(
        cmd,
        repo=repo,
        clock=_make_clock(NOW),
    )

    assert definition.tenant_id == admin.tenant_id
    assert definition.created_by == admin.user_id
    assert definition.created_at == NOW
    assert definition.key == "open-by-priority"
    assert definition.group_by == (custom_reports.GroupByField.CASE_PRIORITY,)
    assert definition.aggregations == (custom_reports.AggregationKind.COUNT,)
    persisted = await repo.get(admin.tenant_id, definition.id)
    assert persisted is not None
    assert persisted.id == definition.id


@pytest.mark.asyncio
async def test_define_custom_report__rejects_unknown_group_by_field() -> None:
    """Closed vocabulary: an unknown group-by field is rejected as
    ``ValidationFailed`` and never reaches the repository.
    """
    admin = _admin()
    repo = custom_reports.ReportDefinitionRepository()
    cmd = custom_reports.DefineCustomReportCommand(
        tenant_id=admin.tenant_id,
        actor_id=admin.user_id,
        actor_roles=admin.roles,
        key="bad",
        name="Bad",
        description="",
        group_by=("not.a.real.field",),  # type: ignore[arg-type]
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(),
        time_window_hours=None,
    )

    with pytest.raises(ValidationFailed):
        await custom_reports.define_custom_report(cmd, repo=repo, clock=_make_clock(NOW))
    assert await repo.list(admin.tenant_id) == []


@pytest.mark.asyncio
async def test_define_custom_report__rejects_unknown_operator_for_field() -> None:
    """The validator enforces ``operator must match field type``. A
    ``gt`` on a string field (``case.status``) is rejected.
    """
    admin = _admin()
    repo = custom_reports.ReportDefinitionRepository()
    cmd = custom_reports.DefineCustomReportCommand(
        tenant_id=admin.tenant_id,
        actor_id=admin.user_id,
        actor_roles=admin.roles,
        key="bad-op",
        name="Bad operator",
        description="",
        group_by=(custom_reports.GroupByField.CASE_PRIORITY,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(
            custom_reports.FilterClause(
                field="case.status",
                op=custom_reports.FilterOperator.GT,  # not allowed for strings
                value="open",
            ),
        ),
        time_window_hours=None,
    )

    with pytest.raises(ValidationFailed):
        await custom_reports.define_custom_report(cmd, repo=repo, clock=_make_clock(NOW))


@pytest.mark.asyncio
async def test_define_custom_report__rejects_non_admin_actor() -> None:
    """An actor without ``admin.tenant_config`` is denied.

    A reviewer holds ``case.read`` but never the administrative
    surface; the policy refuses the action and raises
    ``Forbidden``. The validator runs first but the policy fires
    after — either way the row is never persisted.
    """
    reviewer = _reviewer()
    repo = custom_reports.ReportDefinitionRepository()
    cmd = custom_reports.DefineCustomReportCommand(
        tenant_id=reviewer.tenant_id,
        actor_id=reviewer.user_id,
        actor_roles=reviewer.roles,
        key="reviewer-attempt",
        name="Not allowed",
        description="",
        group_by=(custom_reports.GroupByField.CASE_PRIORITY,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(),
        time_window_hours=None,
    )

    with pytest.raises(Forbidden):
        await custom_reports.define_custom_report(cmd, repo=repo, clock=_make_clock(NOW))
    assert await repo.list(reviewer.tenant_id) == []


# ---------------------------------------------------------------------------
# export_custom_report — audited (P12-US6 / FM-053)
# ---------------------------------------------------------------------------


async def _seed_definition(
    repo: custom_reports.ReportDefinitionRepository,
    *,
    admin: UserContext,
) -> custom_reports.ReportDefinition:
    """Persist a definition through the public use case for re-use."""
    return await custom_reports.define_custom_report(
        custom_reports.DefineCustomReportCommand(
            tenant_id=admin.tenant_id,
            actor_id=admin.user_id,
            actor_roles=admin.roles,
            key="open-by-status",
            name="Open by status",
            description="Counts open cases grouped by status.",
            group_by=(custom_reports.GroupByField.CASE_STATUS,),
            aggregations=(
                custom_reports.AggregationKind.COUNT,
                custom_reports.AggregationKind.OVERDUE_SUM,
            ),
            filters=(),
            time_window_hours=None,
        ),
        repo=repo,
        clock=_make_clock(NOW),
    )


@pytest.mark.asyncio
async def test_export_custom_report__authorized_export_returns_csv_with_audit() -> None:
    """An authorized export renders the spec and appends one
    ``AuditExported`` row to the audit chain.
    """
    admin = _admin()
    repo = custom_reports.ReportDefinitionRepository()
    definition = await _seed_definition(repo, admin=admin)

    case_repo = InMemoryCaseRepository()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(
        Queue.create(
            tenant_id=TENANT_A,
            key="intake_queue",
            name="Intake",
            member_user_ids=(admin.user_id,),
            assignment_policy=AssignmentPolicy.MANUAL,
        )
    )
    urgent = _build_case(tenant_id=TENANT_A, priority="high")
    routine = _build_case(tenant_id=TENANT_A, priority="normal")
    case_repo.seed(urgent)
    case_repo.seed(routine)
    case_repo._open_case_ids = (
        urgent.id,
        routine.id,
    )  # type: ignore[attr-defined]

    audit = InMemoryAuditStore()
    result = await custom_reports.export_custom_report(
        custom_reports.ExportCustomReportCommand(
            tenant_id=admin.tenant_id,
            actor_id=admin.user_id,
            actor_roles=admin.roles,
            definition_id=definition.id,
            reason="quarterly review",
            correlation_id="corr-001",
        ),
        repo=repo,
        case_repo=case_repo,
        queue_repo=queue_repo,
        audit=audit,  # type: ignore[arg-type]
        dedupe=_noop_dedupe,
        clock=_make_clock(NOW),
    )

    csv_lines = result.csv_body.strip().split("\n")
    assert csv_lines[0].startswith("case.status,count,overdue_seconds")
    assert len(csv_lines) >= 2
    assert len(audit.rows) == 1
    audit_row = audit.rows[0]
    assert audit_row["event_type"] == AUDIT_EXPORTED
    assert audit_row["subject_type"] == "report_export"
    assert audit_row["payload"]["definition_key"] == "open-by-status"
    assert audit_row["payload"]["format"] == "csv"
    assert audit_row["payload"]["row_count"] == len(result.report.items)
    assert audit_row["correlation_id"] == "corr-001"
    # The synthetic audit row id is derived from the payload hash;
    # it is a valid UUID so the caller can correlate.
    assert isinstance(result.audit_row_id, UUID)


@pytest.mark.asyncio
async def test_export_custom_report__unauthorized_export_is_forbidden_no_audit() -> None:
    """A reviewer who lacks ``audit.export`` is denied; no audit row
    is appended.
    """
    repo = custom_reports.ReportDefinitionRepository()
    admin = _admin()
    definition = await _seed_definition(repo, admin=admin)

    reviewer = _reviewer()
    case_repo = InMemoryCaseRepository()
    queue_repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()

    with pytest.raises(Forbidden):
        await custom_reports.export_custom_report(
            custom_reports.ExportCustomReportCommand(
                tenant_id=reviewer.tenant_id,
                actor_id=reviewer.user_id,
                actor_roles=reviewer.roles,
                definition_id=definition.id,
                reason="unspecified",
                correlation_id="corr-002",
            ),
            repo=repo,
            case_repo=case_repo,
            queue_repo=queue_repo,
            audit=audit,  # type: ignore[arg-type]
            dedupe=_noop_dedupe,
            clock=_make_clock(NOW),
        )
    assert audit.rows == []


@pytest.mark.asyncio
async def test_export_custom_report__missing_definition_raises_not_found() -> None:
    """An unknown definition id surfaces as ``NotFound`` so the
    router can translate to 404 without leaking cross-tenant
    existence.
    """
    admin = _admin()
    repo = custom_reports.ReportDefinitionRepository()
    case_repo = InMemoryCaseRepository()
    queue_repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()

    with pytest.raises(NotFound):
        await custom_reports.export_custom_report(
            custom_reports.ExportCustomReportCommand(
                tenant_id=admin.tenant_id,
                actor_id=admin.user_id,
                actor_roles=admin.roles,
                definition_id=uuid4(),
                reason="unspecified",
                correlation_id="corr-003",
            ),
            repo=repo,
            case_repo=case_repo,
            queue_repo=queue_repo,
            audit=audit,  # type: ignore[arg-type]
            dedupe=_noop_dedupe,
            clock=_make_clock(NOW),
        )
    assert audit.rows == []


@pytest.mark.asyncio
async def test_export_custom_report__cross_tenant_definition_is_invisible() -> None:
    """A definition persisted under tenant B is invisible from tenant A.

    Constitution V: the existence of a definition in another tenant
    must not be confirmable through the API.
    """
    repo = custom_reports.ReportDefinitionRepository()
    tenant_b_admin = _admin(tenant_id=TENANT_B)
    definition_b = await _seed_definition(repo, admin=tenant_b_admin)

    tenant_a_admin = _admin(tenant_id=TENANT_A)
    case_repo = InMemoryCaseRepository()
    queue_repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()

    with pytest.raises(NotFound):
        await custom_reports.export_custom_report(
            custom_reports.ExportCustomReportCommand(
                tenant_id=tenant_a_admin.tenant_id,
                actor_id=tenant_a_admin.user_id,
                actor_roles=tenant_a_admin.roles,
                definition_id=definition_b.id,
                reason="cross-tenant probe",
                correlation_id="corr-004",
            ),
            repo=repo,
            case_repo=case_repo,
            queue_repo=queue_repo,
            audit=audit,  # type: ignore[arg-type]
            dedupe=_noop_dedupe,
            clock=_make_clock(NOW),
        )
    assert audit.rows == []


# ---------------------------------------------------------------------------
# render_custom_report — buckets and time window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_custom_report__groups_and_counts_cases_correctly() -> None:
    """The renderer buckets cases by the declared group-by field and
    counts them. The actor's team scope is honored.
    """
    admin = _admin()
    repo = custom_reports.ReportDefinitionRepository()
    definition = await _seed_definition(repo, admin=admin)

    case_repo = InMemoryCaseRepository()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(
        Queue.create(
            tenant_id=TENANT_A,
            key="intake_queue",
            name="Intake",
            member_user_ids=(admin.user_id,),
            assignment_policy=AssignmentPolicy.MANUAL,
        )
    )
    case_repo.seed(_build_case(tenant_id=TENANT_A, priority="high"))
    case_repo.seed(_build_case(tenant_id=TENANT_A, priority="high"))
    case_repo.seed(_build_case(tenant_id=TENANT_A, priority="normal"))
    case_repo._open_case_ids = tuple(  # type: ignore[attr-defined]
        c.id for c in case_repo._cases.values()
    )

    report = await custom_reports.render_custom_report(
        operational_reports.OperationalReportsQuery(
            tenant_id=admin.tenant_id,
            actor_id=admin.user_id,
            actor_roles=admin.roles,
        ),
        definition,
        case_repo=case_repo,
        queue_repo=queue_repo,
        now=NOW,
    )

    # Three cases: two priority="high" + one priority="normal" share
    # the same status; only one bucket should appear.
    assert report.total == 1
    bucket = report.items[0]
    assert bucket.count == 3
    # ``OVERDUE_SUM`` aggregation is declared; the fake cases carry no
    # overdue seconds so the bucket sum is 0 — still rendered.
    assert bucket.overdue_seconds == 0


@pytest.mark.asyncio
async def test_render_custom_report__time_window_excludes_stale_cases() -> None:
    """A custom report with a time window excludes cases whose
    ``updated_at`` is outside the window.
    """
    admin = _admin()
    repo = custom_reports.ReportDefinitionRepository()
    definition = await custom_reports.define_custom_report(
        custom_reports.DefineCustomReportCommand(
            tenant_id=admin.tenant_id,
            actor_id=admin.user_id,
            actor_roles=admin.roles,
            key="last-day",
            name="Last day",
            description="Counts cases updated in the last 24 hours.",
            group_by=(custom_reports.GroupByField.CASE_STATUS,),
            aggregations=(custom_reports.AggregationKind.COUNT,),
            filters=(),
            time_window_hours=24,
        ),
        repo=repo,
        clock=_make_clock(NOW),
    )

    case_repo = InMemoryCaseRepository()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(
        Queue.create(
            tenant_id=TENANT_A,
            key="intake_queue",
            name="Intake",
            member_user_ids=(admin.user_id,),
            assignment_policy=AssignmentPolicy.MANUAL,
        )
    )
    fresh = _build_case(tenant_id=TENANT_A)
    stale = _build_case(tenant_id=TENANT_A)
    # Mutate ``updated_at`` directly through object.__setattr__ to
    # bypass the frozen dataclass — the use case reads the attribute.
    object.__setattr__(stale, "updated_at", NOW - timedelta(days=2))
    case_repo.seed(fresh)
    case_repo.seed(stale)
    case_repo._open_case_ids = (fresh.id, stale.id)  # type: ignore[attr-defined]

    report = await custom_reports.render_custom_report(
        operational_reports.OperationalReportsQuery(
            tenant_id=admin.tenant_id,
            actor_id=admin.user_id,
            actor_roles=admin.roles,
        ),
        definition,
        case_repo=case_repo,
        queue_repo=queue_repo,
        now=NOW,
    )

    assert report.total == 1
    assert report.items[0].count == 1


# ---------------------------------------------------------------------------
# Additional coverage: ReportDefinition validation, Repository, Validator,
# _matches_definition, _match_clause, _scalar_eq, _project_value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_definition__empty_key__raises_validation_failed() -> None:
    """Empty key raises ValidationFailed in __post_init__."""
    with pytest.raises(ValidationFailed, match="definition.key is required"):
        custom_reports.ReportDefinition(
            id=uuid4(),
            tenant_id=TENANT_A,
            key="",
            name="Test",
            description="",
            group_by=(custom_reports.GroupByField.CASE_STATUS,),
            aggregations=(custom_reports.AggregationKind.COUNT,),
            filters=(),
            time_window_hours=None,
            created_by=uuid4(),
            created_at=NOW,
        )


@pytest.mark.asyncio
async def test_report_definition__empty_name__raises_validation_failed() -> None:
    """Empty name raises ValidationFailed in __post_init__."""
    with pytest.raises(ValidationFailed, match="definition.name is required"):
        custom_reports.ReportDefinition(
            id=uuid4(),
            tenant_id=TENANT_A,
            key="test",
            name="",
            description="",
            group_by=(custom_reports.GroupByField.CASE_STATUS,),
            aggregations=(custom_reports.AggregationKind.COUNT,),
            filters=(),
            time_window_hours=None,
            created_by=uuid4(),
            created_at=NOW,
        )


@pytest.mark.asyncio
async def test_report_definition__empty_group_by__raises_validation_failed() -> None:
    """Empty group_by raises ValidationFailed in __post_init__."""
    with pytest.raises(ValidationFailed, match="group_by must contain at least one field"):
        custom_reports.ReportDefinition(
            id=uuid4(),
            tenant_id=TENANT_A,
            key="test",
            name="Test",
            description="",
            group_by=(),
            aggregations=(custom_reports.AggregationKind.COUNT,),
            filters=(),
            time_window_hours=None,
            created_by=uuid4(),
            created_at=NOW,
        )


@pytest.mark.asyncio
async def test_report_definition__empty_aggregations__raises_validation_failed() -> None:
    """Empty aggregations raises ValidationFailed in __post_init__."""
    with pytest.raises(ValidationFailed, match="aggregations must contain at least one kind"):
        custom_reports.ReportDefinition(
            id=uuid4(),
            tenant_id=TENANT_A,
            key="test",
            name="Test",
            description="",
            group_by=(custom_reports.GroupByField.CASE_STATUS,),
            aggregations=(),
            filters=(),
            time_window_hours=None,
            created_by=uuid4(),
            created_at=NOW,
        )


@pytest.mark.asyncio
async def test_repository__add_duplicate_key__raises_validation_failed() -> None:
    """Duplicate key raises ValidationFailed on add."""
    repo = custom_reports.ReportDefinitionRepository()
    definition = custom_reports.ReportDefinition(
        id=uuid4(),
        tenant_id=TENANT_A,
        key="duplicate",
        name="Test",
        description="",
        group_by=(custom_reports.GroupByField.CASE_STATUS,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(),
        time_window_hours=None,
        created_by=uuid4(),
        created_at=NOW,
    )
    await repo.add(definition)
    with pytest.raises(ValidationFailed, match="already exists"):
        await repo.add(definition)


@pytest.mark.asyncio
async def test_repository__get_by_key__returns_definition() -> None:
    """get_by_key returns the definition by tenant-scoped key."""
    repo = custom_reports.ReportDefinitionRepository()
    definition = custom_reports.ReportDefinition(
        id=uuid4(),
        tenant_id=TENANT_A,
        key="test-key",
        name="Test",
        description="",
        group_by=(custom_reports.GroupByField.CASE_STATUS,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(),
        time_window_hours=None,
        created_by=uuid4(),
        created_at=NOW,
    )
    await repo.add(definition)
    result = await repo.get_by_key(TENANT_A, "test-key")
    assert result is not None
    assert result.id == definition.id


@pytest.mark.asyncio
async def test_repository__list__returns_tenant_scoped() -> None:
    """list returns only definitions for the given tenant."""
    repo = custom_reports.ReportDefinitionRepository()
    d1 = custom_reports.ReportDefinition(
        id=uuid4(),
        tenant_id=TENANT_A,
        key="a",
        name="A",
        description="",
        group_by=(custom_reports.GroupByField.CASE_STATUS,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(),
        time_window_hours=None,
        created_by=uuid4(),
        created_at=NOW,
    )
    d2 = custom_reports.ReportDefinition(
        id=uuid4(),
        tenant_id=TENANT_B,
        key="b",
        name="B",
        description="",
        group_by=(custom_reports.GroupByField.CASE_STATUS,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(),
        time_window_hours=None,
        created_by=uuid4(),
        created_at=NOW,
    )
    await repo.add(d1)
    await repo.add(d2)
    tenant_a = await repo.list(TENANT_A)
    tenant_b = await repo.list(TENANT_B)
    assert len(tenant_a) == 1
    assert len(tenant_b) == 1
    assert tenant_a[0].key == "a"
    assert tenant_b[0].key == "b"


@pytest.mark.asyncio
async def test_repository__update__persists_changes() -> None:
    """update persists changes to an existing definition."""
    repo = custom_reports.ReportDefinitionRepository()
    definition = custom_reports.ReportDefinition(
        id=uuid4(),
        tenant_id=TENANT_A,
        key="original",
        name="Original",
        description="",
        group_by=(custom_reports.GroupByField.CASE_STATUS,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(),
        time_window_hours=None,
        created_by=uuid4(),
        created_at=NOW,
    )
    await repo.add(definition)
    # ReportDefinition is frozen; create a new instance with updated fields
    updated = custom_reports.ReportDefinition(
        id=definition.id,
        tenant_id=definition.tenant_id,
        key=definition.key,
        name="Updated",
        description=definition.description,
        group_by=definition.group_by,
        aggregations=definition.aggregations,
        filters=definition.filters,
        time_window_hours=definition.time_window_hours,
        created_by=definition.created_by,
        created_at=definition.created_at,
        version_token=2,
    )
    await repo.update(updated)
    result = await repo.get(TENANT_A, definition.id)
    assert result is not None
    assert result.name == "Updated"
    assert result.version_token == 2


@pytest.mark.asyncio
async def test_repository__update_missing__raises_not_found() -> None:
    """update on missing definition raises NotFound."""
    repo = custom_reports.ReportDefinitionRepository()
    definition = custom_reports.ReportDefinition(
        id=uuid4(),
        tenant_id=TENANT_A,
        key="missing",
        name="Missing",
        description="",
        group_by=(custom_reports.GroupByField.CASE_STATUS,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(),
        time_window_hours=None,
        created_by=uuid4(),
        created_at=NOW,
    )
    with pytest.raises(NotFound):
        await repo.update(definition)


@pytest.mark.asyncio
async def test_validate_command__unknown_aggregation__raises_validation_failed() -> None:
    """Unknown aggregation kind raises ValidationFailed."""
    admin = _admin()
    repo = custom_reports.ReportDefinitionRepository()
    cmd = custom_reports.DefineCustomReportCommand(
        tenant_id=admin.tenant_id,
        actor_id=admin.user_id,
        actor_roles=admin.roles,
        key="test",
        name="Test",
        description="",
        group_by=(custom_reports.GroupByField.CASE_STATUS,),
        aggregations=("unknown_agg",),  # type: ignore[arg-type]
        filters=(),
        time_window_hours=None,
    )
    with pytest.raises(ValidationFailed, match="unknown aggregation"):
        await custom_reports.define_custom_report(cmd, repo=repo, clock=_make_clock(NOW))


@pytest.mark.asyncio
async def test_validate_command__time_window_out_of_bounds__raises_validation_failed() -> None:
    """Time window outside [1, 8760] raises ValidationFailed."""
    admin = _admin()
    repo = custom_reports.ReportDefinitionRepository()
    cmd = custom_reports.DefineCustomReportCommand(
        tenant_id=admin.tenant_id,
        actor_id=admin.user_id,
        actor_roles=admin.roles,
        key="test",
        name="Test",
        description="",
        group_by=(custom_reports.GroupByField.CASE_STATUS,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(),
        time_window_hours=0,  # below minimum
    )
    with pytest.raises(ValidationFailed, match="time_window_hours must be in"):
        await custom_reports.define_custom_report(cmd, repo=repo, clock=_make_clock(NOW))


@pytest.mark.asyncio
async def test_validate_command__time_window_too_large__raises_validation_failed() -> None:
    """Time window > 8760 raises ValidationFailed."""
    admin = _admin()
    repo = custom_reports.ReportDefinitionRepository()
    cmd = custom_reports.DefineCustomReportCommand(
        tenant_id=admin.tenant_id,
        actor_id=admin.user_id,
        actor_roles=admin.roles,
        key="test",
        name="Test",
        description="",
        group_by=(custom_reports.GroupByField.CASE_STATUS,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(),
        time_window_hours=8761,  # above maximum
    )
    with pytest.raises(ValidationFailed, match="time_window_hours must be in"):
        await custom_reports.define_custom_report(cmd, repo=repo, clock=_make_clock(NOW))


@pytest.mark.asyncio
async def test_validate_command__unknown_filter_field__raises_validation_failed() -> None:
    """Unknown filter field raises ValidationFailed."""
    admin = _admin()
    repo = custom_reports.ReportDefinitionRepository()
    cmd = custom_reports.DefineCustomReportCommand(
        tenant_id=admin.tenant_id,
        actor_id=admin.user_id,
        actor_roles=admin.roles,
        key="test",
        name="Test",
        description="",
        group_by=(custom_reports.GroupByField.CASE_STATUS,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(
            custom_reports.FilterClause(
                field="unknown.field",
                op=custom_reports.FilterOperator.EQ,
                value="value",
            ),
        ),
        time_window_hours=None,
    )
    with pytest.raises(ValidationFailed, match="unknown filter field"):
        await custom_reports.define_custom_report(cmd, repo=repo, clock=_make_clock(NOW))


@pytest.mark.asyncio
async def test_validate_command__in_operator_empty_tuple__raises_validation_failed() -> None:
    """IN operator with empty tuple raises ValidationFailed."""
    admin = _admin()
    repo = custom_reports.ReportDefinitionRepository()
    cmd = custom_reports.DefineCustomReportCommand(
        tenant_id=admin.tenant_id,
        actor_id=admin.user_id,
        actor_roles=admin.roles,
        key="test",
        name="Test",
        description="",
        group_by=(custom_reports.GroupByField.CASE_STATUS,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(
            custom_reports.FilterClause(
                field="case.status",
                op=custom_reports.FilterOperator.IN,
                value=(),
            ),
        ),
        time_window_hours=None,
    )
    with pytest.raises(ValidationFailed, match="operator 'in' requires a non-empty tuple"):
        await custom_reports.define_custom_report(cmd, repo=repo, clock=_make_clock(NOW))


@pytest.mark.asyncio
async def test_validate_command__in_operator_invalid_entry_type__raises_validation_failed() -> None:
    """IN operator with non-string/UUID entry raises ValidationFailed."""
    admin = _admin()
    repo = custom_reports.ReportDefinitionRepository()
    cmd = custom_reports.DefineCustomReportCommand(
        tenant_id=admin.tenant_id,
        actor_id=admin.user_id,
        actor_roles=admin.roles,
        key="test",
        name="Test",
        description="",
        group_by=(custom_reports.GroupByField.CASE_STATUS,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(
            custom_reports.FilterClause(
                field="case.status",
                op=custom_reports.FilterOperator.IN,
                value=(123,),  # invalid type
            ),
        ),
        time_window_hours=None,
    )
    with pytest.raises(ValidationFailed, match="operator 'in' accepts only string or UUID"):
        await custom_reports.define_custom_report(cmd, repo=repo, clock=_make_clock(NOW))


@pytest.mark.asyncio
async def test_validate_command__scalar_operator_non_scalar_value_raises() -> None:
    """Scalar operator with non-scalar value raises ValidationFailed."""
    admin = _admin()
    repo = custom_reports.ReportDefinitionRepository()
    cmd = custom_reports.DefineCustomReportCommand(
        tenant_id=admin.tenant_id,
        actor_id=admin.user_id,
        actor_roles=admin.roles,
        key="test",
        name="Test",
        description="",
        group_by=(custom_reports.GroupByField.CASE_STATUS,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(
            custom_reports.FilterClause(
                field="case.status",
                op=custom_reports.FilterOperator.EQ,
                value=("a", "b"),  # not scalar
            ),
        ),
        time_window_hours=None,
    )
    with pytest.raises(ValidationFailed, match="requires a scalar value"):
        await custom_reports.define_custom_report(cmd, repo=repo, clock=_make_clock(NOW))


@pytest.mark.asyncio
async def test_match_definition__case_status_filter() -> None:
    """_matches_definition filters by case.status."""
    admin = _admin()
    definition = custom_reports.ReportDefinition(
        id=uuid4(),
        tenant_id=TENANT_A,
        key="test",
        name="Test",
        description="",
        group_by=(custom_reports.GroupByField.CASE_STATUS,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(
            custom_reports.FilterClause(
                field="case.status",
                op=custom_reports.FilterOperator.EQ,
                value="open",
            ),
        ),
        time_window_hours=None,
        created_by=admin.user_id,
        created_at=NOW,
    )
    matching = _build_case(tenant_id=TENANT_A)
    # Use object.__setattr__ to bypass frozen for status
    object.__setattr__(matching, "status", CaseStatus.OPEN)
    non_matching = _build_case(tenant_id=TENANT_A)
    object.__setattr__(non_matching, "status", CaseStatus.CLOSED)

    assert custom_reports._matches_definition(matching, definition, now=NOW) is True
    assert custom_reports._matches_definition(non_matching, definition, now=NOW) is False


@pytest.mark.asyncio
async def test_match_definition__case_priority_filter() -> None:
    """_matches_definition filters by case.priority."""
    admin = _admin()
    definition = custom_reports.ReportDefinition(
        id=uuid4(),
        tenant_id=TENANT_A,
        key="test",
        name="Test",
        description="",
        group_by=(custom_reports.GroupByField.CASE_PRIORITY,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(
            custom_reports.FilterClause(
                field="case.priority",
                op=custom_reports.FilterOperator.EQ,
                value="high",
            ),
        ),
        time_window_hours=None,
        created_by=admin.user_id,
        created_at=NOW,
    )
    matching = _build_case(tenant_id=TENANT_A, priority="high")
    non_matching = _build_case(tenant_id=TENANT_A, priority="normal")

    assert custom_reports._matches_definition(matching, definition, now=NOW) is True
    assert custom_reports._matches_definition(non_matching, definition, now=NOW) is False


@pytest.mark.asyncio
async def test_match_definition__time_window_excludes_stale() -> None:
    """_matches_definition excludes cases outside time window."""
    admin = _admin()
    definition = custom_reports.ReportDefinition(
        id=uuid4(),
        tenant_id=TENANT_A,
        key="test",
        name="Test",
        description="",
        group_by=(custom_reports.GroupByField.CASE_STATUS,),
        aggregations=(custom_reports.AggregationKind.COUNT,),
        filters=(),
        time_window_hours=24,
        created_by=admin.user_id,
        created_at=NOW,
    )
    fresh = _build_case(tenant_id=TENANT_A)
    object.__setattr__(fresh, "updated_at", NOW - timedelta(hours=12))
    stale = _build_case(tenant_id=TENANT_A)
    object.__setattr__(stale, "updated_at", NOW - timedelta(hours=48))

    assert custom_reports._matches_definition(fresh, definition, now=NOW) is True
    assert custom_reports._matches_definition(stale, definition, now=NOW) is False


@pytest.mark.asyncio
async def test_match_clause__eq_ne_in() -> None:
    """_match_clause handles EQ, NE, IN operators."""
    clause_eq = custom_reports.FilterClause(
        field="case.status", op=custom_reports.FilterOperator.EQ, value="open"
    )
    clause_ne = custom_reports.FilterClause(
        field="case.status", op=custom_reports.FilterOperator.NE, value="closed"
    )
    clause_in = custom_reports.FilterClause(
        field="case.priority", op=custom_reports.FilterOperator.IN, value=("high", "urgent")
    )

    assert custom_reports._match_clause("open", clause_eq) is True
    assert custom_reports._match_clause("closed", clause_eq) is False
    assert custom_reports._match_clause("open", clause_ne) is True
    assert custom_reports._match_clause("closed", clause_ne) is False
    assert custom_reports._match_clause("high", clause_in) is True
    assert custom_reports._match_clause("normal", clause_in) is False


@pytest.mark.asyncio
async def test_scalar_eq__uuid_matching() -> None:
    """_scalar_eq handles UUID matching."""
    u = uuid4()
    assert custom_reports._scalar_eq(u, u) is True
    assert custom_reports._scalar_eq(str(u), u) is True
    assert custom_reports._scalar_eq("not-a-uuid", u) is False


@pytest.mark.asyncio
async def test_project_value__unknown_field_returns_empty() -> None:
    """_project_value returns empty string for unknown field."""
    case = _build_case(tenant_id=TENANT_A)
    result = custom_reports._project_value(case, "unknown_field")  # type: ignore[arg-type]
    assert result == ""
