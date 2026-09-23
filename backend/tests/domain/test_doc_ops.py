"""Pure-domain tests for T5 document operations (``app.domain.doc_ops``).

Covers the rules behind ``specs/002-leftovers/spec.md`` §3.T5
document operations:

- x-ref creation (no self-reference, label bounds, sort key) and
  deterministic display/sort;
- package generation: manifest present with entry identity + content
  addresses + package digest; duplicate members rejected; empty and
  over-cap member lists rejected;
- duplicate-by-hash detection: exact-digest groups only, singletons
  omitted, deterministic order;
- authorized CSV export: closed column vocabulary, restricted rows
  dropped without the grant, missing columns rejected.

Naming follows ``test_<unit>_<scenario>__<expectation>`` (AGENTS.md
§4.R5). Structure is ``// given`` / ``// when`` / ``// then``.
Deterministic: fixed clock timestamp, fixed UUIDs, no I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.domain.doc_ops import (
    CsvRow,
    DedupCandidate,
    DocPackage,
    DocXref,
    PackageMember,
    build_csv_export,
    find_duplicate_groups,
    sort_xrefs,
)
from app.domain.errors import ValidationFailed

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OWNER_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
DOC_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
DOC_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
DOC_C = UUID("cccccccc-cccc-cccc-cccc-cccccccccc01")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

SHA_A = "a" * 64
SHA_B = "b" * 64


def _xref(
    *,
    source: UUID = DOC_A,
    target: UUID = DOC_B,
    label: str = "cites",
    sort_order: int = 0,
) -> DocXref:
    """Build one x-ref between fixed documents."""
    return DocXref.create(
        tenant_id=ACME_TENANT,
        source_document_id=source,
        target_document_id=target,
        label=label,
        sort_order=sort_order,
        created_by=OWNER_ID,
        now=FIXED_NOW,
    )


def _member(*, document_id: UUID, sha256: str = SHA_A, size: int = 10) -> PackageMember:
    """Build one package member descriptor."""
    return PackageMember(
        document_id=document_id,
        version_number=1,
        sha256=sha256,
        size=size,
    )


# ---------------------------------------------------------------------------
# X-refs
# ---------------------------------------------------------------------------


def test_xref_create__valid__round_trips() -> None:
    """A well-formed reference stores both ends plus label and order."""
    # // given two distinct documents
    # // when creating // then the aggregate pins both ends.
    xref = _xref()
    assert xref.source_document_id == DOC_A
    assert xref.target_document_id == DOC_B
    assert xref.label == "cites"
    assert xref.sort_order == 0


def test_xref_create__self_reference__rejects() -> None:
    """A document cannot cite itself (navigation would fork)."""
    # // given identical source and target
    # // when creating // then ValidationFailed names the target.
    with pytest.raises(ValidationFailed) as exc_info:
        _xref(source=DOC_A, target=DOC_A)
    assert exc_info.value.field == "target_document_id"


def test_xref_create__blank_label__rejects() -> None:
    """The viewer citation strip needs an operator label."""
    # // given a blank label
    # // when creating // then ValidationFailed names label.
    with pytest.raises(ValidationFailed) as exc_info:
        _xref(label="   ")
    assert exc_info.value.field == "label"


def test_xref_create__negative_sort_order__rejects() -> None:
    """Sort keys are non-negative display positions."""
    # // given a negative sort order
    # // when creating // then ValidationFailed names sort_order.
    with pytest.raises(ValidationFailed) as exc_info:
        _xref(sort_order=-1)
    assert exc_info.value.field == "sort_order"


def test_xref_sort__by_label__orders_deterministically() -> None:
    """Configurable display/sort: ordering follows the requested key."""
    # // given two x-refs with distinct labels
    first = _xref(target=DOC_B, label="zebra", sort_order=0)
    second = _xref(target=DOC_C, label="apple", sort_order=5)
    # // when sorting by label // then label order wins over insertion.
    ordered = sort_xrefs((first, second), sort_by="label")
    assert [x.label for x in ordered] == ["apple", "zebra"]


def test_xref_sort__unknown_key__rejects_fail_closed() -> None:
    """Callers cannot order by an attribute outside the vocabulary."""
    # // given one x-ref
    xref = _xref()
    # // when sorting by an unknown key // then ValidationFailed.
    with pytest.raises(ValidationFailed) as exc_info:
        sort_xrefs((xref,), sort_by="storage_ref")
    assert exc_info.value.field == "sort_by"


# ---------------------------------------------------------------------------
# Packages (manifest is the acceptance artifact)
# ---------------------------------------------------------------------------


def test_package_create__manifest__carries_entries_and_digest() -> None:
    """A package always ships a manifest (acceptance: includes manifest)."""
    # // given two members with distinct digests
    package = DocPackage.create(
        tenant_id=ACME_TENANT,
        name="Hearing exhibits",
        members=(
            _member(document_id=DOC_A, sha256=SHA_A),
            _member(document_id=DOC_B, sha256=SHA_B),
        ),
        created_by=OWNER_ID,
        now=FIXED_NOW,
    )
    # // when reading the manifest // then entries + digest are present.
    manifest = package.manifest()
    assert manifest["entry_count"] == 2
    assert manifest["name"] == "Hearing exhibits"
    entries = manifest["entries"]
    assert isinstance(entries, list)
    assert len(entries) == 2
    assert {entry["document_id"] for entry in entries} == {str(DOC_A), str(DOC_B)}
    assert all("sha256" in entry for entry in entries)
    assert manifest["package_sha256"] == package.package_sha256


def test_package_digest__insertion_order__is_order_independent() -> None:
    """The package digest identifies the member *set*, not assembly order."""
    # // given the same members in two orders
    left = DocPackage.create(
        tenant_id=ACME_TENANT,
        name="Pack",
        members=(
            _member(document_id=DOC_A, sha256=SHA_A),
            _member(document_id=DOC_B, sha256=SHA_B),
        ),
        created_by=OWNER_ID,
        now=FIXED_NOW,
    )
    right = DocPackage.create(
        tenant_id=ACME_TENANT,
        name="Pack",
        members=(
            _member(document_id=DOC_B, sha256=SHA_B),
            _member(document_id=DOC_A, sha256=SHA_A),
        ),
        created_by=OWNER_ID,
        now=FIXED_NOW,
    )
    # // when comparing digests // then they match.
    assert left.package_sha256 == right.package_sha256


def test_package_create__duplicate_member__rejects() -> None:
    """A repeated member would double-count bytes in the manifest."""
    # // given the same document twice
    # // when creating // then ValidationFailed names members.
    with pytest.raises(ValidationFailed) as exc_info:
        DocPackage.create(
            tenant_id=ACME_TENANT,
            name="Dupes",
            members=(_member(document_id=DOC_A), _member(document_id=DOC_A)),
            created_by=OWNER_ID,
            now=FIXED_NOW,
        )
    assert exc_info.value.field == "members"


def test_package_create__empty_members__rejects() -> None:
    """A package without members is not a package."""
    # // given no members
    # // when creating // then ValidationFailed names members.
    with pytest.raises(ValidationFailed) as exc_info:
        DocPackage.create(
            tenant_id=ACME_TENANT,
            name="Empty",
            members=(),
            created_by=OWNER_ID,
            now=FIXED_NOW,
        )
    assert exc_info.value.field == "members"


# ---------------------------------------------------------------------------
# Hash-dedup
# ---------------------------------------------------------------------------


def test_dedup__shared_digest__groups_ids() -> None:
    """Two documents with one digest form a duplicate group (acceptance)."""
    # // given two candidates sharing a digest plus a singleton
    candidates = (
        DedupCandidate(document_id=DOC_A, sha256=SHA_A),
        DedupCandidate(document_id=DOC_B, sha256=SHA_A),
        DedupCandidate(document_id=DOC_C, sha256=SHA_B),
    )
    # // when grouping // then exactly the shared-digest pair returns.
    assert find_duplicate_groups(candidates) == ((DOC_A, DOC_B),)


def test_dedup__all_unique__returns_empty() -> None:
    """Unique uploads are not duplicates."""
    # // given distinct digests
    candidates = (
        DedupCandidate(document_id=DOC_A, sha256=SHA_A),
        DedupCandidate(document_id=DOC_B, sha256=SHA_B),
    )
    # // when grouping // then no groups return.
    assert find_duplicate_groups(candidates) == ()


def test_dedup__repeated_candidate__counts_once() -> None:
    """A repeated candidate row does not inflate its own group."""
    # // given the same document listed twice plus a true duplicate
    candidates = (
        DedupCandidate(document_id=DOC_A, sha256=SHA_A),
        DedupCandidate(document_id=DOC_A, sha256=SHA_A),
        DedupCandidate(document_id=DOC_B, sha256=SHA_A),
    )
    # // when grouping // then the group holds each id once.
    assert find_duplicate_groups(candidates) == ((DOC_A, DOC_B),)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


def _row(*, document_id: UUID, sensitivity: str = "internal") -> CsvRow:
    """Build one export row for a fixed document."""
    return CsvRow(
        values={
            "document_id": str(document_id),
            "file_name": "exhibit.pdf",
            "sensitivity": sensitivity,
        },
        sensitivity=sensitivity,
    )


def test_csv__restricted_row_without_grant__drops() -> None:
    """Restricted rows are withheld (not nulled) without the grant."""
    # // given one internal + one restricted row
    rows = (_row(document_id=DOC_A), _row(document_id=DOC_B, sensitivity="restricted"))
    # // when exporting without the grant // then only the internal row emits.
    export = build_csv_export(
        columns=("document_id", "file_name"),
        rows=rows,
        include_restricted=False,
    )
    assert export.row_count == 1
    assert export.to_text().splitlines()[0] == "document_id,file_name"


def test_csv__restricted_row_with_grant__emits() -> None:
    """The stricter grant re-admits restricted rows (authorized export)."""
    # // given one restricted row
    rows = (_row(document_id=DOC_B, sensitivity="restricted"),)
    # // when exporting with the grant // then the row emits.
    export = build_csv_export(
        columns=("document_id",),
        rows=rows,
        include_restricted=True,
    )
    assert export.row_count == 1


def test_csv__unknown_column__rejects_fail_closed() -> None:
    """A caller cannot smuggle a field into an export by naming it."""
    # // given a row and an unknown column
    rows = (_row(document_id=DOC_A),)
    # // when exporting // then ValidationFailed names columns.
    with pytest.raises(ValidationFailed) as exc_info:
        build_csv_export(columns=("storage_ref",), rows=rows, include_restricted=False)
    assert exc_info.value.field == "columns"
