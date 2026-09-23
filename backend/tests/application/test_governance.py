"""Application tests for configuration governance (FM-021 / P10-US4, US5, US8).

Acceptance scenarios (encoded as ``test_<unit>_<scenario>__<expectation>``
per AGENTS.md §4.R5):

1. **Publish history lists actor / change_summary** (P10-US4): a draft
   row, a publish row, and a retire row all appear with the right
   actor and change summary, newest-first.
2. **Retiring a published version succeeds** (P10-US5): a ``retire``
   action appends a publication row carrying the retiring actor's
   id and the supplied change summary.
3. **Draft mutation allowed** (P10-US4): an artifact that is still
   in ``draft`` status may receive a second ``draft`` row (e.g. when
   the admin saves another iteration); the use case does not block
   it. The companion domain invariant (``CaseTypeVersion.assert_mutable``)
   is exercised in the case-type / workflow tests.
4. **Second-admin approval gate** (P10-US8):
   - When the tenant does **not** require second-admin approval, a
     single publish succeeds with ``approval_required=False``.
   - When the tenant **does** require second-admin approval, a
     single publish succeeds with ``approval_required=True`` and
     ``assert_publish_approved`` raises ``Forbidden`` until a
     different administrator approves.
   - A same-actor approval attempt raises ``Forbidden``.

Naming follows AGENTS.md §4.R5; tests are deterministic (fixed
clock, fresh repo per test).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.application.configuration import governance
from app.domain.errors import Forbidden, NotFound

from ._governance_fakes import (
    FixedClock,
    InMemoryGovernancePolicy,
    InMemoryPublicationRepository,
)

# ---------------------------------------------------------------------------
# Constants — two distinct tenants
# ---------------------------------------------------------------------------


ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
BETA_TENANT = UUID("22222222-2222-2222-2222-222222222222")

ADMIN_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SECOND_ADMIN = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

CASE_TYPE_ID = UUID("33333333-3333-3333-3333-333333333333")
VERSION_ID = UUID("44444444-4444-4444-4444-444444444444")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


@pytest.fixture
def repo() -> InMemoryPublicationRepository:
    return InMemoryPublicationRepository()


# ---------------------------------------------------------------------------
# 1. Publish history lists actor / change_summary (P10-US4)
# ---------------------------------------------------------------------------


async def test_governance__list_publications__shows_actor_and_change_summary_newest_first(
    clock: FixedClock, repo: InMemoryPublicationRepository
) -> None:
    """Draft → publish → retire yields three rows, newest-first, with
    actor and change summary visible. This is what the admin timeline
    UI binds to (P10-US4)."""
    policy = InMemoryGovernancePolicy()

    await governance.record_publication(
        governance.RecordPublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_ID,
            action="draft",
            actor_id=ADMIN_USER,
            change_summary="initial schema skeleton",
            correlation_id="c-draft",
        ),
        repo=repo,
        policy=policy,
        clock=clock,
    )

    await governance.record_publication(
        governance.RecordPublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_ID,
            action="publish",
            actor_id=ADMIN_USER,
            change_summary="add priority enum; mark title required",
            correlation_id="c-publish",
        ),
        repo=repo,
        policy=policy,
        clock=clock,
    )

    await governance.record_publication(
        governance.RecordPublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_ID,
            action="retire",
            actor_id=ADMIN_USER,
            change_summary="superseded by v2; existing cases retain binding",
            correlation_id="c-retire",
        ),
        repo=repo,
        policy=policy,
        clock=clock,
    )

    entries = await governance.list_publications(
        governance.ListPublicationsQuery(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
        ),
        repo=repo,
    )

    assert [e.action for e in entries] == ["retire", "publish", "draft"]
    for entry in entries:
        assert entry.actor_id == ADMIN_USER
        assert entry.tenant_id == ACME_TENANT
        assert entry.artifact_type == "CaseTypeVersion"
        assert entry.artifact_id == CASE_TYPE_ID
        assert entry.version_id == VERSION_ID

    # The change_summary is preserved verbatim on every row so the
    # admin timeline can render "what changed" without a join.
    summaries = {e.action: e.change_summary for e in entries}
    assert summaries["draft"] == "initial schema skeleton"
    assert summaries["publish"] == "add priority enum; mark title required"
    assert "superseded by v2" in summaries["retire"]


async def test_governance__list_publications__cross_tenant_returns_only_own_rows(
    clock: FixedClock, repo: InMemoryPublicationRepository
) -> None:
    """Tenant isolation: a tenant B read never sees tenant A rows even
    though both tenants share the artifact id namespace."""
    policy = InMemoryGovernancePolicy()

    await governance.record_publication(
        governance.RecordPublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_ID,
            action="publish",
            actor_id=ADMIN_USER,
            change_summary="acme publish",
            correlation_id="c",
        ),
        repo=repo,
        policy=policy,
        clock=clock,
    )

    entries = await governance.list_publications(
        governance.ListPublicationsQuery(
            tenant_id=BETA_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
        ),
        repo=repo,
    )
    assert entries == []


# ---------------------------------------------------------------------------
# 2. Retiring a published version succeeds (P10-US5)
# ---------------------------------------------------------------------------


async def test_governance__retire_published_version__appends_retire_row(
    clock: FixedClock, repo: InMemoryPublicationRepository
) -> None:
    """A retire action appends a publication row carrying the retiring
    actor's id and the supplied change summary. The status of the
    version row itself is the case-type domain's responsibility
    (``CaseTypeVersion.retire``); the governance surface only records
    the lifecycle event."""
    policy = InMemoryGovernancePolicy()

    await governance.record_publication(
        governance.RecordPublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_ID,
            action="publish",
            actor_id=ADMIN_USER,
            change_summary="initial publish",
            correlation_id="c",
        ),
        repo=repo,
        policy=policy,
        clock=clock,
    )

    await governance.record_publication(
        governance.RecordPublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_ID,
            action="retire",
            actor_id=ADMIN_USER,
            change_summary="retired in favor of v2",
            correlation_id="c-retire",
        ),
        repo=repo,
        policy=policy,
        clock=clock,
    )

    entries = await governance.list_publications(
        governance.ListPublicationsQuery(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
        ),
        repo=repo,
    )

    assert [e.action for e in entries] == ["retire", "publish"]
    assert entries[0].change_summary == "retired in favor of v2"
    assert entries[0].actor_id == ADMIN_USER


# ---------------------------------------------------------------------------
# 3. Draft mutation allowed (P10-US4)
# ---------------------------------------------------------------------------


async def test_governance__draft_iteration__records_subsequent_draft_rows(
    clock: FixedClock, repo: InMemoryPublicationRepository
) -> None:
    """An admin may record multiple draft rows against the same
    artifact + version (each iteration of the schema). The use case
    does not block the second draft append. The companion domain
    invariant (``CaseTypeVersion.assert_mutable``) blocks schema
    edits on a non-draft version; here the publication timeline just
    records the metadata."""
    policy = InMemoryGovernancePolicy()

    first = await governance.record_publication(
        governance.RecordPublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_ID,
            action="draft",
            actor_id=ADMIN_USER,
            change_summary="first draft",
            correlation_id="c1",
        ),
        repo=repo,
        policy=policy,
        clock=clock,
    )

    second = await governance.record_publication(
        governance.RecordPublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_ID,
            action="draft",
            actor_id=ADMIN_USER,
            change_summary="second draft after review",
            correlation_id="c2",
        ),
        repo=repo,
        policy=policy,
        clock=clock,
    )

    assert first.publication_id != second.publication_id
    assert second.approval_required is False

    entries = await governance.list_publications(
        governance.ListPublicationsQuery(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
        ),
        repo=repo,
    )

    drafts = [e for e in entries if e.action == "draft"]
    assert len(drafts) == 2
    assert drafts[0].change_summary == "second draft after review"
    assert drafts[1].change_summary == "first draft"


async def test_governance__record_publication__unknown_action__raises_value_error(
    clock: FixedClock, repo: InMemoryPublicationRepository
) -> None:
    """An unrecognized action string is a programmer error: the use
    case raises ``ValueError`` so the HTTP layer can map it to 422.
    The publication table is append-only; an unknown action would
    silently corrupt the timeline if the use case accepted it."""
    policy = InMemoryGovernancePolicy()

    with pytest.raises(ValueError, match="."):
        await governance.record_publication(
            governance.RecordPublicationCommand(
                tenant_id=ACME_TENANT,
                artifact_type="CaseTypeVersion",
                artifact_id=CASE_TYPE_ID,
                version_id=VERSION_ID,
                action="archive",
                actor_id=ADMIN_USER,
                change_summary="",
                correlation_id="c",
            ),
            repo=repo,
            policy=policy,
            clock=clock,
        )

    # No row was appended on the failed call.
    assert repo._rows == []  # noqa: SLF001 — test inspects internal state


# ---------------------------------------------------------------------------
# 4. Second-admin approval gate (P10-US8)
# ---------------------------------------------------------------------------


async def test_governance__publish_single_admin_policy_off__succeeds_with_no_approval_required(
    clock: FixedClock, repo: InMemoryPublicationRepository
) -> None:
    """When the tenant does NOT require second-admin approval, a
    single publish succeeds with ``approval_required=False`` and the
    approval gate (``assert_publish_approved``) is a no-op."""
    policy = InMemoryGovernancePolicy()  # no tenants enabled

    result = await governance.record_publication(
        governance.RecordPublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_ID,
            action="publish",
            actor_id=ADMIN_USER,
            change_summary="publish",
            correlation_id="c",
        ),
        repo=repo,
        policy=policy,
        clock=clock,
    )
    assert result.approval_required is False

    # The approval gate is silent when the policy does not require it.
    await governance.assert_publish_approved(
        tenant_id=ACME_TENANT,
        artifact_type="CaseTypeVersion",
        artifact_id=CASE_TYPE_ID,
        version_id=VERSION_ID,
        repo=repo,
        policy=policy,
    )


async def test_governance__publish_single_admin_policy_on__blocks_effective_publish(
    clock: FixedClock, repo: InMemoryPublicationRepository
) -> None:
    """When the tenant DOES require second-admin approval, a single
    publish sets ``approval_required=True`` AND
    ``assert_publish_approved`` raises ``Forbidden`` because no
    approval row exists. The publish row is durable on the timeline;
    the gate is read by downstream consumers (e.g. the
    case-creation flow that binds a case to the version)."""
    policy = InMemoryGovernancePolicy(require_second_admin_for={ACME_TENANT})

    result = await governance.record_publication(
        governance.RecordPublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_ID,
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

    with pytest.raises(Forbidden) as excinfo:
        await governance.assert_publish_approved(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_ID,
            repo=repo,
            policy=policy,
        )
    assert "approval_required" in str(excinfo.value)


async def test_governance__publish_then_approve_by_different_admin__unblocks_publish(
    clock: FixedClock, repo: InMemoryPublicationRepository
) -> None:
    """When a second administrator approves the publish (different
    actor), the gate raises no exception. The approval row carries
    ``action='validate'`` and the approving actor's id."""
    policy = InMemoryGovernancePolicy(require_second_admin_for={ACME_TENANT})

    await governance.record_publication(
        governance.RecordPublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_ID,
            action="publish",
            actor_id=ADMIN_USER,
            change_summary="publish by admin",
            correlation_id="c",
        ),
        repo=repo,
        policy=policy,
        clock=clock,
    )

    approval = await governance.approve_publication(
        governance.ApprovePublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_ID,
            approving_actor_id=SECOND_ADMIN,
            change_summary="peer review ok",
            correlation_id="c-approve",
        ),
        repo=repo,
        clock=clock,
    )
    assert approval.action == "validate"
    assert approval.actor_id == SECOND_ADMIN

    # Gate is now silent.
    await governance.assert_publish_approved(
        tenant_id=ACME_TENANT,
        artifact_type="CaseTypeVersion",
        artifact_id=CASE_TYPE_ID,
        version_id=VERSION_ID,
        repo=repo,
        policy=policy,
    )

    # Timeline shows publish + validate in newest-first order.
    entries = await governance.list_publications(
        governance.ListPublicationsQuery(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
        ),
        repo=repo,
    )
    assert [e.action for e in entries] == ["validate", "publish"]
    assert entries[0].actor_id == SECOND_ADMIN
    assert entries[1].actor_id == ADMIN_USER


async def test_governance__approve_with_same_actor__raises_forbidden(
    clock: FixedClock, repo: InMemoryPublicationRepository
) -> None:
    """Approval by the same actor who published is separation-of-
    duties violation; the use case raises ``Forbidden`` with a
    stable ``code='approval_self'`` string so the HTTP layer can map
    it to a 403 with a structured ``ErrorResponse``."""
    policy = InMemoryGovernancePolicy(require_second_admin_for={ACME_TENANT})

    await governance.record_publication(
        governance.RecordPublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_ID,
            action="publish",
            actor_id=ADMIN_USER,
            change_summary="publish",
            correlation_id="c",
        ),
        repo=repo,
        policy=policy,
        clock=clock,
    )

    with pytest.raises(Forbidden) as excinfo:
        await governance.approve_publication(
            governance.ApprovePublicationCommand(
                tenant_id=ACME_TENANT,
                artifact_type="CaseTypeVersion",
                artifact_id=CASE_TYPE_ID,
                version_id=VERSION_ID,
                approving_actor_id=ADMIN_USER,
                change_summary="self-approval attempt",
                correlation_id="c",
            ),
            repo=repo,
            clock=clock,
        )
    assert "approval_self" in str(excinfo.value)

    # Gate is still blocking (the self-approval attempt was rejected).
    with pytest.raises(Forbidden) as excinfo:
        await governance.assert_publish_approved(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_ID,
            repo=repo,
            policy=policy,
        )
    assert "approval_required" in str(excinfo.value)


async def test_governance__approve_without_publish__raises_not_found(
    clock: FixedClock, repo: InMemoryPublicationRepository
) -> None:
    """Approval on an artifact that has no publish row yet is a
    client error: the use case raises ``NotFound`` so the HTTP layer
    returns 404. Without this check, a client could probe for
    artifacts by issuing approval calls and inferring their
    existence."""
    InMemoryGovernancePolicy(require_second_admin_for={ACME_TENANT})

    with pytest.raises(NotFound):
        await governance.approve_publication(
            governance.ApprovePublicationCommand(
                tenant_id=ACME_TENANT,
                artifact_type="CaseTypeVersion",
                artifact_id=CASE_TYPE_ID,
                version_id=VERSION_ID,
                approving_actor_id=SECOND_ADMIN,
                change_summary="",
                correlation_id="c",
            ),
            repo=repo,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 5. Tenant-wide history filtering
# ---------------------------------------------------------------------------


async def test_governance__tenant_wide_history__filters_by_artifact_type(
    clock: FixedClock, repo: InMemoryPublicationRepository
) -> None:
    """The tenant-wide timeline filter narrows the result to the
    requested ``artifact_type``. This is the read path the admin UI
    uses for the "Activity" tab when the admin has not picked an
    artifact yet."""
    policy = InMemoryGovernancePolicy()

    await governance.record_publication(
        governance.RecordPublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="CaseTypeVersion",
            artifact_id=CASE_TYPE_ID,
            version_id=VERSION_ID,
            action="publish",
            actor_id=ADMIN_USER,
            change_summary="ct publish",
            correlation_id="c",
        ),
        repo=repo,
        policy=policy,
        clock=clock,
    )
    await governance.record_publication(
        governance.RecordPublicationCommand(
            tenant_id=ACME_TENANT,
            artifact_type="WorkflowVersion",
            artifact_id=UUID("55555555-5555-5555-5555-555555555555"),
            version_id=UUID("66666666-6666-6666-6666-666666666666"),
            action="publish",
            actor_id=ADMIN_USER,
            change_summary="wf publish",
            correlation_id="c",
        ),
        repo=repo,
        policy=policy,
        clock=clock,
    )

    only_workflow = await governance.list_publications(
        governance.ListPublicationsQuery(
            tenant_id=ACME_TENANT,
            artifact_type="WorkflowVersion",
        ),
        repo=repo,
    )
    assert len(only_workflow) == 1
    assert only_workflow[0].artifact_type == "WorkflowVersion"
    assert only_workflow[0].change_summary == "wf publish"

    all_rows = await governance.list_publications(
        governance.ListPublicationsQuery(tenant_id=ACME_TENANT),
        repo=repo,
    )
    assert len(all_rows) == 2
