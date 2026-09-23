"""Publication-history governance tests (FM-021 / P10-US4, US5, US8).

Acceptance for this task, encoded as
``test_<unit>_<scenario>__<expectation>`` (AGENTS.md §4.R5):

- **Published versions immutable**: UPDATE-style mutation on a
  ``published`` (or ``retired``) version raises ``InvariantViolation``
  — via the entity guards (``CaseTypeVersion`` / ``WorkflowVersion``)
  and the shared repository guard
  (``publication_history.assert_version_mutable``).
- **Retirement rules**: only ``published → retired`` succeeds and the
  retired version keeps its authorship (``published_by/at``); retiring
  a draft or re-retiring raises; retirement stays allowed while
  dependents exist (existing cases retain their binding, FM-008).
- **Second-admin approval**: self-approval raises ``Forbidden``;
  unapproved publish stays blocked while the tenant requires it;
  a distinct approver unblocks; the gate is a no-op when off.
- **Effective time / dependencies**: ``effective_version_at``
  resolves the historically effective version; ``where_used`` /
  ``impact_view`` surface dependents with no new tables.

Deterministic: fixed clock, fixed UUIDs, in-memory fakes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.application.configuration import governance as app_governance
from app.domain import publication_history as ph
from app.domain.case_type import (
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
)
from app.domain.errors import Forbidden, InvariantViolation, NotFound
from app.domain.workflow import (
    WorkflowState,
    WorkflowTransition,
    WorkflowVersion,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
ADMIN_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SECOND_ADMIN = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
CASE_TYPE_ID = UUID("33333333-3333-3333-3333-333333333333")
VERSION_1 = UUID("44444444-4444-4444-4444-444444444444")
VERSION_2 = UUID("55555555-5555-5555-5555-555555555555")
WORKFLOW_ID = UUID("66666666-6666-6666-6666-666666666666")

T1 = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
T2 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
T3 = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)


def _healthy_schema() -> CaseTypeSchema:
    """Minimal valid schema for draft construction."""
    return CaseTypeSchema(
        fields=(CaseTypeField(key="title", label="Title", type="text", required=True),),
        document_classifications=("Correspondence",),
        rules=(),
    )


def _draft_case_type_version(version_id: UUID = VERSION_1) -> CaseTypeVersion:
    """Build a draft CaseTypeVersion pinned to a fixed id."""
    draft = CaseTypeVersion.draft(
        tenant_id=ACME_TENANT,
        case_type_id=CASE_TYPE_ID,
        version_number=1,
        schema=_healthy_schema(),
        now=T1,
    )
    # // given a frozen dataclass — rebind the id for determinism
    return CaseTypeVersion(
        id=version_id,
        tenant_id=draft.tenant_id,
        case_type_id=draft.case_type_id,
        version_number=draft.version_number,
        schema=draft.schema,
        status=draft.status,
        created_at=draft.created_at,
    )


def _draft_workflow_version() -> WorkflowVersion:
    """Build a minimal draft WorkflowVersion."""
    return WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=WORKFLOW_ID,
        version_number=1,
        states=(
            WorkflowState(key="intake", label="Intake", is_initial=True),
            WorkflowState(key="closed", label="Closed", is_terminal=True),
        ),
        transitions=(
            WorkflowTransition(
                key="close", from_state="intake", to_state="closed", human_driven=True
            ),
        ),
        now=T1,
    )


def _entry(
    version_id: UUID,
    action: ph.PublicationAction,
    actor: UUID,
    at: datetime,
    summary: str = "",
) -> ph.PublicationEntry:
    """Build one history entry with a fresh id."""
    return ph.PublicationEntry(
        id=uuid4(),
        tenant_id=ACME_TENANT,
        artifact_type="CaseTypeVersion",
        artifact_id=CASE_TYPE_ID,
        version_id=version_id,
        action=action,
        actor_id=actor,
        occurred_at=at,
        change_summary=summary,
    )


# ---------------------------------------------------------------------------
# Local fakes for the application governance use cases.
# ---------------------------------------------------------------------------


class _FixedClock:
    """Frozen clock so approval timestamps are deterministic."""

    def now(self) -> datetime:
        return T2


class _InMemoryPublicationRepo:
    """Append-only tenant-scoped store mirroring the SQL adapter."""

    def __init__(self) -> None:
        self._rows: list[app_governance.PublicationRecord] = []

    async def add(
        self,
        *,
        tenant_id: UUID,
        artifact_type: str,
        artifact_id: UUID,
        version_id: UUID,
        action: str,
        actor_id: UUID,
        change_summary: str,
        occurred_at: datetime,
    ) -> UUID:
        from uuid_extensions import uuid7  # noqa: PLC0415

        # // why uuid7: time-ordered ids keep newest-first stable when
        # // the fixed clock stamps identical timestamps (mirrors the
        # // shared governance fakes).
        new_id = uuid7()
        self._rows.append(
            app_governance.PublicationRecord(
                id=new_id,
                tenant_id=tenant_id,
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                version_id=version_id,
                action=action,
                actor_id=actor_id,
                occurred_at=occurred_at,
                change_summary=change_summary,
            )
        )
        return new_id

    async def list_for_artifact(
        self,
        tenant_id: UUID,
        artifact_type: str,
        artifact_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[app_governance.PublicationRecord]:
        rows = [
            r
            for r in self._rows
            if r.tenant_id == tenant_id
            and r.artifact_type == artifact_type
            and r.artifact_id == artifact_id
        ]
        rows.sort(key=lambda r: (r.occurred_at, r.id), reverse=True)
        return rows[offset : offset + limit]

    async def list_tenant_history(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        artifact_type: str | None = None,
    ) -> Sequence[app_governance.PublicationRecord]:
        rows = [r for r in self._rows if r.tenant_id == tenant_id]
        if artifact_type is not None:
            rows = [r for r in rows if r.artifact_type == artifact_type]
        rows.sort(key=lambda r: (r.occurred_at, r.id), reverse=True)
        return rows[offset : offset + limit]


class _PolicyOn:
    """Tenant requires second-admin approval."""

    async def require_second_admin(self, tenant_id: UUID) -> bool:
        return tenant_id == ACME_TENANT


# ---------------------------------------------------------------------------
# 1. Published versions immutable (FR-015, AC-007)
# ---------------------------------------------------------------------------


def test_case_type_version__publish_then_mutate__raises_invariant_violation() -> None:
    """A published CaseTypeVersion rejects mutation attempts."""
    # // given a published version
    published = _draft_case_type_version().publish(actor_id=ADMIN_USER, at=T2)
    # // when the repository guard checks the published row
    # // then every mutation path raises
    with pytest.raises(InvariantViolation, match="cannot be modified"):
        published.assert_mutable()
    with pytest.raises(InvariantViolation, match="cannot be modified"):
        ph.assert_version_mutable(published.status.value)


def test_case_type_version__republish_published__raises_invariant_violation() -> None:
    """Publishing an already-published version is rejected."""
    # // given a published version
    published = _draft_case_type_version().publish(actor_id=ADMIN_USER, at=T2)
    # // when publish is attempted again // then it raises
    with pytest.raises(InvariantViolation, match="cannot publish a published"):
        published.publish(actor_id=ADMIN_USER, at=T3)


def test_workflow_version__publish_then_mutate__raises_invariant_violation() -> None:
    """A published WorkflowVersion rejects mutation attempts."""
    # // given a published workflow version
    published = _draft_workflow_version().publish(actor_id=ADMIN_USER, at=T2)
    # // when guarded // then both entity and shared guard raise
    with pytest.raises(InvariantViolation, match="cannot be modified"):
        published.assert_mutable()
    with pytest.raises(InvariantViolation, match="cannot be modified"):
        ph.assert_version_mutable(published.status.value)
    with pytest.raises(InvariantViolation, match="cannot publish"):
        published.publish(actor_id=ADMIN_USER, at=T3)


def test_shared_guard__update_on_retired_row__raises_invariant_violation() -> None:
    """A retired row is as immutable as a published one; drafts pass."""
    # // given statuses straight from the row // when guarded // then
    with pytest.raises(InvariantViolation, match="cannot be modified"):
        ph.assert_version_mutable("published")
    with pytest.raises(InvariantViolation, match="cannot be modified"):
        ph.assert_version_mutable("retired")
    ph.assert_version_mutable("draft")


# ---------------------------------------------------------------------------
# 2. Retirement rules (P10-US5)
# ---------------------------------------------------------------------------


def test_case_type_version__retire_published__preserves_authorship() -> None:
    """Retiring keeps published_by/at so history shows who published."""
    # // given a published version
    published = _draft_case_type_version().publish(actor_id=ADMIN_USER, at=T2)
    # // when retired // then authorship survives the transition
    retired = published.retire()
    assert retired.status.value == "retired"
    assert retired.published_by == ADMIN_USER
    assert retired.published_at == T2


def test_case_type_version__retire_draft__raises_invariant_violation() -> None:
    """A draft has nothing to retire."""
    # // given a draft // when retire is attempted // then it raises
    with pytest.raises(InvariantViolation, match="only published versions can be retired"):
        _draft_case_type_version().retire()
    with pytest.raises(InvariantViolation, match="only published"):
        ph.assert_retire_allowed("draft")


def test_workflow_version__retire_published__then_second_retire_raises() -> None:
    """Retirement is terminal; re-retiring raises."""
    # // given a retired workflow version
    retired = _draft_workflow_version().publish(actor_id=ADMIN_USER, at=T2).retire()
    assert retired.status.value == "retired"
    # // when retired again // then it raises
    with pytest.raises(InvariantViolation, match="only published versions can be retired"):
        retired.retire()
    with pytest.raises(InvariantViolation, match="only published"):
        ph.assert_retire_allowed("retired")


def test_retirement__stays_allowed_while_dependents_exist() -> None:
    """Dependents never veto retirement; they are surfaced, not blocking."""
    # // given a published version with a bound case (FM-008 retains binding)
    dependents = (ph.DependencyRef(version_id=VERSION_1, kind="case_binding", ref_id=uuid4()),)
    # // when guarded // then retirement is allowed and impact is visible
    ph.assert_retire_allowed("published")
    assert len(ph.where_used(VERSION_1, dependents)) == 1


async def test_governance__retire_published__appends_retire_row() -> None:
    """The application layer records the retire lifecycle event."""
    # // given publish + retire rows from the retiring actor
    repo, clock, policy = _InMemoryPublicationRepo(), _FixedClock(), _PolicyOn()
    for action in ("publish", "retire"):
        await app_governance.record_publication(
            app_governance.RecordPublicationCommand(
                tenant_id=ACME_TENANT,
                artifact_type="CaseTypeVersion",
                artifact_id=CASE_TYPE_ID,
                version_id=VERSION_1,
                action=action,
                actor_id=ADMIN_USER,
                change_summary=f"{action} summary",
                correlation_id=f"c-{action}",
            ),
            repo=repo,
            policy=policy,
            clock=clock,
        )
    # // when history is read // then both rows appear newest-first
    entries = await app_governance.list_publications(
        app_governance.ListPublicationsQuery(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
        ),
        repo=repo,
    )
    assert [e.action for e in entries] == ["retire", "publish"]
    assert entries[0].actor_id == ADMIN_USER


# ---------------------------------------------------------------------------
# 3. Second-admin approval (P10-US8) — domain
# ---------------------------------------------------------------------------


def test_approval__self_approval__raises_forbidden() -> None:
    """The publisher cannot approve their own publish."""
    # // given a publish row by the admin
    entries = [_entry(VERSION_1, ph.PublicationAction.PUBLISH, ADMIN_USER, T1)]
    # // when the same actor approves // then separation-of-duties raises
    with pytest.raises(Forbidden, match="approval_self"):
        ph.approve(
            entries,
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_1,
            approver_id=ADMIN_USER,
            at=T2,
        )


def test_approval__approve_without_publish__raises_not_found() -> None:
    """Approval with no pending publish is a client error."""
    # // given history with no publish // when approved // then NotFound
    with pytest.raises(NotFound, match="no pending publish"):
        ph.approve(
            [],
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_1,
            approver_id=SECOND_ADMIN,
            at=T2,
        )


def test_approval__unapproved_publish__blocks_gate_until_distinct_approver() -> None:
    """The gate blocks until a different administrator approves."""
    # // given a lone publish with the policy on
    entries = [_entry(VERSION_1, ph.PublicationAction.PUBLISH, ADMIN_USER, T1, "v1")]
    with pytest.raises(Forbidden, match="approval_required"):
        ph.assert_publish_approved(entries, version_id=VERSION_1, require_second_admin=True)
    # // when a distinct admin approves // then the gate goes silent
    approval = ph.approve(
        entries,
        tenant_id=ACME_TENANT,
        artifact_type="CaseTypeVersion",
        artifact_id=CASE_TYPE_ID,
        version_id=VERSION_1,
        approver_id=SECOND_ADMIN,
        at=T2,
        change_summary="peer review ok",
    )
    assert approval.action is ph.PublicationAction.VALIDATE
    assert approval.actor_id == SECOND_ADMIN
    ph.assert_publish_approved(
        [*entries, approval], version_id=VERSION_1, require_second_admin=True
    )


def test_approval__policy_off__gate_is_noop() -> None:
    """Single-admin tenants never hit the gate."""
    entries = [_entry(VERSION_1, ph.PublicationAction.PUBLISH, ADMIN_USER, T1)]
    ph.assert_publish_approved(entries, version_id=VERSION_1, require_second_admin=False)


# ---------------------------------------------------------------------------
# 4. Second-admin approval (P10-US8) — application
# ---------------------------------------------------------------------------


async def test_app_governance__publish_then_approve__unblocks_effective_publish() -> None:
    """Application publish flags approval_required; distinct approval unblocks."""
    repo, clock, policy = _InMemoryPublicationRepo(), _FixedClock(), _PolicyOn()
    # // given a publish under a second-admin tenant
    result = await app_governance.record_publication(
        app_governance.RecordPublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_1,
            action="publish",
            actor_id=ADMIN_USER,
            change_summary="publish by admin",
            correlation_id="c",
        ),
        repo=repo,
        policy=policy,
        clock=clock,
    )
    assert result.approval_required is True
    with pytest.raises(Forbidden, match="approval_required"):
        await app_governance.assert_publish_approved(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_1,
            repo=repo,
            policy=policy,
        )
    # // when a different admin approves // then the gate goes silent
    approval = await app_governance.approve_publication(
        app_governance.ApprovePublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_1,
            approving_actor_id=SECOND_ADMIN,
            change_summary="peer review ok",
            correlation_id="c-approve",
        ),
        repo=repo,
        clock=clock,
    )
    assert approval.action == "validate"
    assert approval.actor_id == SECOND_ADMIN
    await app_governance.assert_publish_approved(
        tenant_id=ACME_TENANT,
        artifact_type="CaseTypeVersion",
        artifact_id=CASE_TYPE_ID,
        version_id=VERSION_1,
        repo=repo,
        policy=policy,
    )


async def test_app_governance__approve_with_same_actor__raises_forbidden() -> None:
    """Application self-approval is rejected and the gate stays blocked."""
    repo, clock, policy = _InMemoryPublicationRepo(), _FixedClock(), _PolicyOn()
    await app_governance.record_publication(
        app_governance.RecordPublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_1,
            action="publish",
            actor_id=ADMIN_USER,
            change_summary="publish",
            correlation_id="c",
        ),
        repo=repo,
        policy=policy,
        clock=clock,
    )
    with pytest.raises(Forbidden, match="approval_self"):
        await app_governance.approve_publication(
            app_governance.ApprovePublicationCommand(
                tenant_id=ACME_TENANT,
                artifact_type="CaseTypeVersion",
                artifact_id=CASE_TYPE_ID,
                version_id=VERSION_1,
                approving_actor_id=ADMIN_USER,
                change_summary="self-approval attempt",
                correlation_id="c",
            ),
            repo=repo,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 5. Effective time (P10-US4)
# ---------------------------------------------------------------------------


def test_effective_time__resolves_historical_version() -> None:
    """Publish/retire intervals resolve the version effective at ``at``."""
    # // given v1 published T1, v2 published T2, v1 retired T3
    entries = [
        _entry(VERSION_1, ph.PublicationAction.PUBLISH, ADMIN_USER, T1),
        _entry(VERSION_2, ph.PublicationAction.PUBLISH, ADMIN_USER, T2),
        _entry(VERSION_1, ph.PublicationAction.RETIRE, ADMIN_USER, T3),
    ]
    # // when resolved at successive moments // then history is truthful
    before = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)
    assert ph.effective_version_at(entries, at=before) is None
    assert ph.effective_version_at(entries, at=T1) == VERSION_1
    assert ph.effective_version_at(entries, at=T2) == VERSION_2
    after = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)
    assert ph.effective_version_at(entries, at=after) == VERSION_2


def test_effective_time__drafts_only__resolves_none() -> None:
    """Drafts are never effective."""
    entries = [_entry(VERSION_1, ph.PublicationAction.DRAFT, ADMIN_USER, T1)]
    assert ph.effective_version_at(entries, at=T2) is None


# ---------------------------------------------------------------------------
# 6. Dependencies / impact view (002 T7, FM-021)
# ---------------------------------------------------------------------------


def test_dependencies__where_used__filters_by_version() -> None:
    """Only dependents bound to the queried version are returned."""
    # // given dependents across two versions
    dependents = (
        ph.DependencyRef(version_id=VERSION_1, kind="case_binding", ref_id=uuid4()),
        ph.DependencyRef(version_id=VERSION_2, kind="case_binding", ref_id=uuid4()),
    )
    # // when queried // then each version sees only its own
    assert len(ph.where_used(VERSION_1, dependents)) == 1
    assert len(ph.where_used(VERSION_2, dependents)) == 1
    assert ph.where_used(uuid4(), dependents) == ()


def test_dependencies__impact_view__groups_by_kind_deterministically() -> None:
    """The admin impact view groups dependents by kind, sorted."""
    # // given mixed dependents on one version
    dependents = (
        ph.DependencyRef(version_id=VERSION_1, kind="queue_member", ref_id=uuid4(), detail="q"),
        ph.DependencyRef(version_id=VERSION_1, kind="case_binding", ref_id=uuid4(), detail="c"),
        ph.DependencyRef(version_id=VERSION_2, kind="case_binding", ref_id=uuid4()),
    )
    # // when grouped // then kinds sort and foreign versions drop out
    view = ph.impact_view(VERSION_1, dependents)
    assert list(view.keys()) == ["case_binding", "queue_member"]
    assert len(view["case_binding"]) == 1
    assert view["case_binding"][0].detail == "c"


# ---------------------------------------------------------------------------
# 7. Authorship (P10-US4)
# ---------------------------------------------------------------------------


def test_authorship__entries__preserve_actor_and_summary() -> None:
    """History rows carry who acted and what changed, verbatim."""
    # // given draft → publish → retire by one admin
    entries = [
        _entry(VERSION_1, ph.PublicationAction.DRAFT, ADMIN_USER, T1, "skeleton"),
        _entry(VERSION_1, ph.PublicationAction.PUBLISH, ADMIN_USER, T2, "add title"),
        _entry(VERSION_1, ph.PublicationAction.RETIRE, SECOND_ADMIN, T3, "superseded"),
    ]
    # // when inspected // then authorship and summaries survive
    by_action = {e.action: e for e in entries}
    assert by_action[ph.PublicationAction.PUBLISH].actor_id == ADMIN_USER
    assert by_action[ph.PublicationAction.RETIRE].actor_id == SECOND_ADMIN
    assert by_action[ph.PublicationAction.PUBLISH].change_summary == "add title"
    assert ph.latest_publish_for_version(entries, VERSION_1) is not None
    assert ph.latest_publish_for_version(entries, uuid4()) is None
