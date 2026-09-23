"""Tests for case-relationship use cases (P01-US5, US7, US8 / FM-010).

Acceptance scenarios:

1. **Relate two cases** (P01-US5): the relate use case creates a
   bidirectional ``related`` edge; both cases can navigate to the
   other via ``list_for_case``; two audit rows are written (one per
   case) and one outbox event describing the link.
2. **Split preserves documents** (P01-US7): splitting a case that has
   documents produces a new case whose ``Document`` rows share the
   source's ``storage_ref`` and ``sha256``. The split edge is recorded
   and the source's audit chain is preserved.
3. **Merge retains history** (P01-US8): merging two cases marks the
   retired case ``CANCELLED``, preserves both audit chains, and
   records a ``merged_into`` edge from the retired case to the
   survivor.
4. **Tenant isolation**: an actor from tenant B cannot relate two of
   tenant A's cases; a cross-tenant ``target_case_id`` surfaces as
   ``NotFound`` (Constitution V).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.application.cases.merge_cases import MergeCasesCommand
from app.application.cases.merge_cases import handle as merge_cases
from app.application.cases.relate_cases import RelateCasesCommand
from app.application.cases.relate_cases import handle as relate_cases
from app.application.cases.split_case import (
    SplitCaseCommand,
)
from app.application.cases.split_case import (
    handle as split_case,
)
from app.domain.case import Case, CaseReference
from app.domain.case_relationship import RelationshipKind
from app.domain.document import Document, DocumentSensitivity
from app.domain.errors import Forbidden, InvariantViolation, NotFound

from ._fakes import (
    FixedClock,
    InMemoryAuditStore,
    InMemoryCaseRelationshipRepository,
    InMemoryCaseRepository,
    InMemoryCaseTypeRepository,
    InMemoryCaseTypeVersionRepository,
    InMemoryDocumentRepository,
    InMemoryOutbox,
    make_case_type,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")

INTAKE_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
REVIEWER_USER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

FIXED_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


@pytest.fixture
def seeded() -> dict[str, Any]:
    """Seed tenant ACME with a published case type and two open cases.

    Returns a dict of in-memory fakes so each test can drive the
    relationship use cases and inspect the staged rows.
    """
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case_repo.set_tenant_short(OTHER_TENANT, "OTHER")

    case_type_repo = InMemoryCaseTypeRepository()
    version_repo = InMemoryCaseTypeVersionRepository()
    case_type, version = make_case_type(
        tenant_id=ACME_TENANT,
        required_keys=("title", "summary"),
    )
    case_type_repo.add(case_type)
    version_repo.add(version)

    left = Case.create(
        tenant_id=ACME_TENANT,
        reference=CaseReference("C-ACME-2026-000001"),
        case_type_version=version,
        title="Left case",
        description="",
        data={"title": "Left", "summary": "left summary"},
        priority="normal",
        created_by=INTAKE_USER,
        now=FIXED_NOW,
    )
    right = Case.create(
        tenant_id=ACME_TENANT,
        reference=CaseReference("C-ACME-2026-000002"),
        case_type_version=version,
        title="Right case",
        description="",
        data={"title": "Right", "summary": "right summary"},
        priority="normal",
        created_by=INTAKE_USER,
        now=FIXED_NOW,
    )
    case_repo._cases.append(left)
    case_repo._cases.append(right)

    # Seed one tenant-B case to exercise cross-tenant isolation paths.
    other_type, other_version = make_case_type(
        tenant_id=OTHER_TENANT, key="OtherType", required_keys=("title", "summary")
    )
    case_type_repo.add(other_type)
    version_repo.add(other_version)
    other = Case.create(
        tenant_id=OTHER_TENANT,
        reference=CaseReference("C-OTHER-2026-000001"),
        case_type_version=other_version,
        title="Other tenant case",
        description="",
        data={"title": "Other", "summary": "other summary"},
        priority="normal",
        created_by=REVIEWER_USER,
        now=FIXED_NOW,
    )
    case_repo._cases.append(other)

    return {
        "case_repo": case_repo,
        "case_type_repo": case_type_repo,
        "version_repo": version_repo,
        "relationship_repo": InMemoryCaseRelationshipRepository(),
        "document_repo": InMemoryDocumentRepository(),
        "audit": InMemoryAuditStore(),
        "outbox": InMemoryOutbox(),
        "left": left,
        "right": right,
        "other": other,
        "version": version,
    }


def _seed_doc(
    document_repo: InMemoryDocumentRepository,
    *,
    tenant_id: UUID,
    case_id: UUID,
) -> Document:
    """Stage one document on the source case for the split tests."""
    doc = Document.create(
        tenant_id=tenant_id,
        case_id=case_id,
        file_name="contract.pdf",
        content_type="application/pdf",
        classification="Contract",
        sensitivity=DocumentSensitivity.INTERNAL,
        sha256="a" * 64,
        size=1024,
        storage_ref="local://documents/a" * 8 + ".pdf",
        uploaded_by=INTAKE_USER,
        now=FIXED_NOW,
    )
    document_repo._documents.append(doc)
    return doc


# ---------------------------------------------------------------------------
# 1. Relate (P01-US5)
# ---------------------------------------------------------------------------


async def test_relate_cases__two_open_cases__creates_bidirectional_pair(
    seeded: dict[str, Any],
    clock: FixedClock,
) -> None:
    """The relate use case creates two edges (one per direction) that
    share a ``relationship_id`` and lets each case navigate to the
    other via ``list_for_case``.
    """
    result = await relate_cases(
        RelateCasesCommand(
            tenant_id=ACME_TENANT,
            actor_id=REVIEWER_USER,
            actor_roles=("reviewer",),
            source_case_id=seeded["left"].id,
            target_case_id=seeded["right"].id,
            reason="related intake",
            correlation_id="rel-corr",
        ),
        case_repo=seeded["case_repo"],
        relationship_repo=seeded["relationship_repo"],
        audit=seeded["audit"],
        outbox=seeded["outbox"],
        clock=clock,
    )

    # Both halves of the pair share a relationship_id.
    assert result.source_edge.relationship_id == result.target_edge.relationship_id
    assert result.source_edge.source_case_id == seeded["left"].id
    assert result.source_edge.target_case_id == seeded["right"].id
    assert result.target_edge.source_case_id == seeded["right"].id
    assert result.target_edge.target_case_id == seeded["left"].id

    # Repository state: two edges persisted (the pair).
    edges = seeded["relationship_repo"].edges
    assert len(edges) == 2
    assert {e.source_case_id for e in edges} == {seeded["left"].id, seeded["right"].id}

    # Each case can navigate to the other via list_for_case.
    left_edges = await seeded["relationship_repo"].list_for_case(
        ACME_TENANT, seeded["left"].id, kinds=[RelationshipKind.RELATED]
    )
    right_edges = await seeded["relationship_repo"].list_for_case(
        ACME_TENANT, seeded["right"].id, kinds=[RelationshipKind.RELATED]
    )
    assert len(left_edges) == 1
    assert left_edges[0].target_case_id == seeded["right"].id
    assert len(right_edges) == 1
    assert right_edges[0].target_case_id == seeded["left"].id

    # Two audit rows (one per case) + one outbox event.
    assert len(seeded["audit"].events) == 2
    assert all(e.event_type == "CaseRelationshipCreated" for e in seeded["audit"].events)
    subject_ids = {e.subject_id for e in seeded["audit"].events}
    assert subject_ids == {seeded["left"].id, seeded["right"].id}
    # Both audit rows carry the relationship_id so a reader can
    # reconstruct the pair from any side.
    rel_ids = {e.payload["relationship_id"] for e in seeded["audit"].events}
    assert len(rel_ids) == 1
    assert len(seeded["outbox"].events) == 1
    outbox_payload = seeded["outbox"].events[0].payload
    assert outbox_payload["source_case_id"] == str(seeded["left"].id)
    assert outbox_payload["target_case_id"] == str(seeded["right"].id)


async def test_relate_cases__same_pair_twice__returns_existing_edges_without_duplicating(
    seeded: dict[str, Any],
    clock: FixedClock,
) -> None:
    """A second call with the same pair is idempotent: it returns the
    prior edges without staging a second pair or extra audit/outbox rows.
    """
    cmd = RelateCasesCommand(
        tenant_id=ACME_TENANT,
        actor_id=REVIEWER_USER,
        actor_roles=("reviewer",),
        source_case_id=seeded["left"].id,
        target_case_id=seeded["right"].id,
    )
    first = await relate_cases(
        cmd,
        case_repo=seeded["case_repo"],
        relationship_repo=seeded["relationship_repo"],
        audit=seeded["audit"],
        outbox=seeded["outbox"],
        clock=clock,
    )
    second = await relate_cases(
        cmd,
        case_repo=seeded["case_repo"],
        relationship_repo=seeded["relationship_repo"],
        audit=seeded["audit"],
        outbox=seeded["outbox"],
        clock=clock,
    )

    assert first.source_edge.id == second.source_edge.id
    assert first.target_edge.id == second.target_edge.id
    assert len(seeded["relationship_repo"].edges) == 2
    assert len(seeded["outbox"].events) == 1


async def test_relate_cases__actor_lacks_update__raises_forbidden(
    seeded: dict[str, Any],
    clock: FixedClock,
) -> None:
    """A user without ``case.update`` is denied before any DB write."""
    cmd = RelateCasesCommand(
        tenant_id=ACME_TENANT,
        actor_id=INTAKE_USER,
        actor_roles=("intake",),  # intake does NOT have case.update
        source_case_id=seeded["left"].id,
        target_case_id=seeded["right"].id,
    )
    with pytest.raises(Forbidden):
        await relate_cases(
            cmd,
            case_repo=seeded["case_repo"],
            relationship_repo=seeded["relationship_repo"],
            audit=seeded["audit"],
            outbox=seeded["outbox"],
            clock=clock,
        )
    assert seeded["relationship_repo"].edges == []
    assert seeded["audit"].events == []
    assert seeded["outbox"].events == []


async def test_relate_cases__cross_tenant_target__raises_not_found(
    seeded: dict[str, Any],
    clock: FixedClock,
) -> None:
    """An actor in tenant A cannot relate their case to a tenant B case.

    The cross-tenant ``target_case_id`` returns ``NotFound`` so the API
    never confirms cross-tenant existence (Constitution V).
    """
    cmd = RelateCasesCommand(
        tenant_id=ACME_TENANT,
        actor_id=REVIEWER_USER,
        actor_roles=("reviewer",),
        source_case_id=seeded["left"].id,
        target_case_id=seeded["other"].id,
    )
    with pytest.raises(NotFound):
        await relate_cases(
            cmd,
            case_repo=seeded["case_repo"],
            relationship_repo=seeded["relationship_repo"],
            audit=seeded["audit"],
            outbox=seeded["outbox"],
            clock=clock,
        )
    assert seeded["relationship_repo"].edges == []


async def test_relate_cases__self_relation__raises_invariant_violation(
    seeded: dict[str, Any],
    clock: FixedClock,
) -> None:
    """Relating a case to itself is rejected before any DB write."""
    cmd = RelateCasesCommand(
        tenant_id=ACME_TENANT,
        actor_id=REVIEWER_USER,
        actor_roles=("reviewer",),
        source_case_id=seeded["left"].id,
        target_case_id=seeded["left"].id,
    )
    with pytest.raises(InvariantViolation):
        await relate_cases(
            cmd,
            case_repo=seeded["case_repo"],
            relationship_repo=seeded["relationship_repo"],
            audit=seeded["audit"],
            outbox=seeded["outbox"],
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 2. Split (P01-US7)
# ---------------------------------------------------------------------------


async def test_split_case__preserves_documents_and_provenance(
    seeded: dict[str, Any],
    clock: FixedClock,
) -> None:
    """Splitting a case with documents produces a new case whose
    ``Document`` rows share the source's ``storage_ref`` and ``sha256``,
    and the new case carries a ``split_from`` edge back to the source.
    """
    source = seeded["left"]
    _seed_doc(seeded["document_repo"], tenant_id=ACME_TENANT, case_id=source.id)

    result = await split_case(
        SplitCaseCommand(
            tenant_id=ACME_TENANT,
            actor_id=REVIEWER_USER,
            actor_roles=("reviewer",),
            source_case_id=source.id,
            new_title="Split-off case",
            correlation_id="split-corr",
        ),
        case_repo=seeded["case_repo"],
        case_type_version_repo=seeded["version_repo"],
        document_repo=seeded["document_repo"],
        relationship_repo=seeded["relationship_repo"],
        audit=seeded["audit"],
        outbox=seeded["outbox"],
        clock=clock,
    )

    # New case created with a fresh reference and the source's case type.
    assert result.new_case.tenant_id == ACME_TENANT
    assert result.new_case.id != source.id
    assert result.new_case.title == "Split-off case"
    assert result.new_case.case_type_version_id == source.case_type_version_id

    # Documents carried over with the same sha256 + storage_ref.
    assert len(result.carried_document_ids) == 1
    carried = next(
        d for d in seeded["document_repo"].documents if d.id == result.carried_document_ids[0]
    )
    source_doc = next(d for d in seeded["document_repo"].documents if d.case_id == source.id)
    assert carried.sha256 == source_doc.sha256
    assert carried.storage_ref == source_doc.storage_ref
    assert carried.case_id == result.new_case.id
    assert carried.id != source_doc.id

    # Split edge recorded.
    edges = seeded["relationship_repo"].edges
    assert len(edges) == 1
    assert edges[0].kind == RelationshipKind.SPLIT_FROM
    assert edges[0].source_case_id == source.id
    assert edges[0].target_case_id == result.new_case.id

    # Source audit chain preserved (the source row was not mutated).
    audit_subject_ids = {e.subject_id for e in seeded["audit"].events}
    assert source.id in audit_subject_ids
    assert result.new_case.id in audit_subject_ids

    # Outbox event published with both ids.
    assert len(seeded["outbox"].events) == 1
    assert seeded["outbox"].events[0].payload["source_case_id"] == str(source.id)
    assert seeded["outbox"].events[0].payload["new_case_id"] == str(result.new_case.id)


async def test_split_case__no_documents__still_creates_new_case_and_edge(
    seeded: dict[str, Any],
    clock: FixedClock,
) -> None:
    """A split on a case without attached documents still creates the
    new case + edge and writes the audit/outbox rows."""
    source = seeded["left"]
    result = await split_case(
        SplitCaseCommand(
            tenant_id=ACME_TENANT,
            actor_id=REVIEWER_USER,
            actor_roles=("reviewer",),
            source_case_id=source.id,
            new_title="Split-off (no docs)",
        ),
        case_repo=seeded["case_repo"],
        case_type_version_repo=seeded["version_repo"],
        document_repo=seeded["document_repo"],
        relationship_repo=seeded["relationship_repo"],
        audit=seeded["audit"],
        outbox=seeded["outbox"],
        clock=clock,
    )
    assert result.carried_document_ids == ()
    assert len(result.new_case.id.__str__()) == 36  # UUID hex
    assert len(seeded["relationship_repo"].edges) == 1


# ---------------------------------------------------------------------------
# 3. Merge (P01-US8)
# ---------------------------------------------------------------------------


async def test_merge_cases__retired_case_cancelled_and_history_preserved(
    seeded: dict[str, Any],
    clock: FixedClock,
) -> None:
    """Merging two cases marks the retired case CANCELLED, preserves
    both audit chains, and records a ``merged_into`` edge.
    """
    survivor = seeded["left"]
    retired = seeded["right"]

    result = await merge_cases(
        MergeCasesCommand(
            tenant_id=ACME_TENANT,
            actor_id=REVIEWER_USER,
            actor_roles=("reviewer",),
            survivor_case_id=survivor.id,
            retired_case_id=retired.id,
            reason="duplicate intake",
            correlation_id="merge-corr",
        ),
        case_repo=seeded["case_repo"],
        relationship_repo=seeded["relationship_repo"],
        audit=seeded["audit"],
        outbox=seeded["outbox"],
        clock=clock,
    )

    # Retired case status flipped to CANCELLED.
    refreshed_retired = await seeded["case_repo"].get(ACME_TENANT, retired.id)
    assert refreshed_retired is not None
    assert refreshed_retired.status.value == "cancelled"

    # Survivor status unchanged.
    refreshed_survivor = await seeded["case_repo"].get(ACME_TENANT, survivor.id)
    assert refreshed_survivor is not None
    assert refreshed_survivor.status.value == "open"

    # Merge edge recorded (retired -> survivor).
    assert len(seeded["relationship_repo"].edges) == 1
    edge = seeded["relationship_repo"].edges[0]
    assert edge.kind == RelationshipKind.MERGED_INTO
    assert edge.source_case_id == retired.id
    assert edge.target_case_id == survivor.id
    assert edge.relationship_id == result.merge_edge.relationship_id

    # Both audit chains preserved — the merge wrote two rows, one per
    # case, in the same transaction.
    audit_subjects = {e.subject_id for e in seeded["audit"].events}
    assert {survivor.id, retired.id}.issubset(audit_subjects)
    event_types = [e.event_type for e in seeded["audit"].events]
    assert "CaseMerged" in event_types
    assert "CaseMergedInto" in event_types

    # Outbox event published with the merged reference.
    assert len(seeded["outbox"].events) == 1
    assert seeded["outbox"].events[0].payload["survivor_case_id"] == str(survivor.id)
    assert seeded["outbox"].events[0].payload["retired_case_id"] == str(retired.id)


async def test_merge_cases__survivor_equals_retired__raises_invariant(
    seeded: dict[str, Any],
    clock: FixedClock,
) -> None:
    """Merging a case into itself is rejected before any DB write."""
    case = seeded["left"]
    with pytest.raises(InvariantViolation):
        await merge_cases(
            MergeCasesCommand(
                tenant_id=ACME_TENANT,
                actor_id=REVIEWER_USER,
                actor_roles=("reviewer",),
                survivor_case_id=case.id,
                retired_case_id=case.id,
            ),
            case_repo=seeded["case_repo"],
            relationship_repo=seeded["relationship_repo"],
            audit=seeded["audit"],
            outbox=seeded["outbox"],
            clock=clock,
        )
    assert seeded["relationship_repo"].edges == []


async def test_merge_cases__cross_tenant_retired__raises_not_found(
    seeded: dict[str, Any],
    clock: FixedClock,
) -> None:
    """An actor cannot merge another tenant's case into their own
    (Constitution V)."""
    with pytest.raises(NotFound):
        await merge_cases(
            MergeCasesCommand(
                tenant_id=ACME_TENANT,
                actor_id=REVIEWER_USER,
                actor_roles=("reviewer",),
                survivor_case_id=seeded["left"].id,
                retired_case_id=seeded["other"].id,
            ),
            case_repo=seeded["case_repo"],
            relationship_repo=seeded["relationship_repo"],
            audit=seeded["audit"],
            outbox=seeded["outbox"],
            clock=clock,
        )
    assert seeded["relationship_repo"].edges == []


__all__ = [
    "ACME_TENANT",
]
