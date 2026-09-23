"""Document operations kernel: x-refs, packages, hash-dedup, CSV export (T5).

This module is the **pure-domain** kernel for the T5 document-operations
rule from ``specs/002-leftovers/spec.md`` §3.T5 ("cross-document
references (configurable display/sort), separation workspace
(split/join/merge preserving audit), document package generation
(multi-doc export with manifest), duplicate-by-hash detection, CSV
export of result lists where authorized"):

1. **DocXref** — a directed cross-document reference (``source`` cites
   ``target``) with an operator label and a sort key. Display order is
   ``(sort_order, created_at, id)`` so listing is deterministic without
   depending on database row order. X-refs never confer read access:
   reading through a reference still requires ``document.read`` on the
   target (enforced by the application use case, which resolves both
   ends in the actor's tenant).
2. **DocPackage** — a multi-document export envelope. ``build_package``
   assembles member descriptors plus a **manifest** (package id,
   tenant, name, entry count, per-entry ``(document_id, version,
   sha256, size)``, package SHA-256 over the sorted member digests,
   creator, timestamp). The manifest is the acceptance artifact: a
   package without a manifest is not a package.
3. **Hash-dedup** — :func:`find_duplicate_groups` clusters document
   descriptors by ``sha256`` so accidental re-uploads are detected
   without reading bytes. Comparison is exact-digest only; there is no
   fuzzy matching (a near-duplicate is a new document).
4. **CSV export** — :func:`build_csv_export` renders an allowlisted
   column projection of a result list. Columns are a closed vocabulary
   (``CSV_COLUMNS``); unknown columns are rejected at build time so a
   caller cannot smuggle a sensitive field into an export by naming
   it. Restricted-sensitivity rows are dropped unless the caller
   asserts ``include_restricted`` (which the application use case only
   sets after a ``document.read_restricted`` grant check).

# Why this module is pure (no I/O)

All inputs are caller-supplied descriptors (ids, digests, sizes);
membership/authorization checks need repositories and live in the
application use case (``app.application.documents.doc_ops``). The
separation workspace itself lives in ``app.domain.separation``; this
module only owns the T5 operations around it.

# INTEGRATION_SEAM: none

No Azure seam: these are deterministic local computations over
metadata the platform already stores.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

from app.domain.errors import ValidationFailed

# Maximum operator label length for an x-ref. Labels render in the
# viewer citation strip; the cap keeps the row (reproduced in audit
# payloads) bounded.
MAX_XREF_LABEL_CHARS = 120

# Maximum members per package. A package is a curated export, not a
# bulk dump (bulk movement is the T8 evidence-bundle path with its own
# policy version + actor + timestamp audit). The cap keeps the manifest
# — reproduced in the audit row — bounded.
MAX_PACKAGE_MEMBERS = 500

# Maximum rows per CSV export. Exports are operator result lists, not
# table dumps; the cap keeps the response bounded and the audit row
# (which records the row count) honest.
MAX_CSV_ROWS = 5000

# Closed column vocabulary for CSV export. ``sha256`` is included (it
# is a content address, not content); ``storage_ref`` is excluded (it
# is an infrastructure address that must never leave the system
# boundary).
CSV_COLUMNS: Final[tuple[str, ...]] = (
    "document_id",
    "case_id",
    "file_name",
    "classification",
    "sensitivity",
    "sha256",
    "size",
    "current_version",
)

# Columns that are safe to sort an x-ref list by from the API. The
# application layer maps the request value onto this vocabulary and
# rejects anything else (fail-closed so a caller cannot order by a
# sensitive or non-existent attribute).
XREF_SORT_KEYS: Final[tuple[str, ...]] = ("sort_order", "created_at", "label")


@dataclass(frozen=True, slots=True)
class DocXref:
    """A directed cross-document reference (T5 doc ops).

    ``source_document_id`` cites ``target_document_id``. Both ends must
    belong to the same tenant (checked by the application use case,
    which loads the documents); the domain pins the self-reference and
    label rules here.
    """

    id: UUID
    tenant_id: UUID
    source_document_id: UUID
    target_document_id: UUID
    label: str
    sort_order: int
    created_by: UUID
    created_at: datetime

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        source_document_id: UUID,
        target_document_id: UUID,
        label: str,
        sort_order: int,
        created_by: UUID,
        now: datetime,
    ) -> DocXref:
        """Construct a validated cross-document reference.

        Raises:
            ValidationFailed: On a self-reference (a document citing
                itself would fork citation navigation), a blank or
                over-long label, or a negative sort key.
        """
        if source_document_id == target_document_id:
            raise ValidationFailed(
                "a document cannot reference itself",
                field="target_document_id",
            )
        stripped = label.strip() if isinstance(label, str) else ""
        if not stripped or len(stripped) > MAX_XREF_LABEL_CHARS:
            raise ValidationFailed(
                f"xref label is required (1..{MAX_XREF_LABEL_CHARS} chars)",
                field="label",
            )
        if sort_order < 0:
            raise ValidationFailed(
                "sort_order must be >= 0",
                field="sort_order",
            )
        return DocXref(
            id=uuid4(),
            tenant_id=tenant_id,
            source_document_id=source_document_id,
            target_document_id=target_document_id,
            label=stripped,
            sort_order=sort_order,
            created_by=created_by,
            created_at=now,
        )


def sort_xrefs(xrefs: tuple[DocXref, ...], *, sort_by: str) -> tuple[DocXref, ...]:
    """Return x-refs in deterministic display order for ``sort_by``.

    The primary key is the requested attribute; ties break on
    ``(sort_order, created_at, id)`` so pagination is stable across
    calls. Unknown keys raise (fail-closed: the caller cannot order by
    an attribute the contract does not expose).
    """
    if sort_by not in XREF_SORT_KEYS:
        raise ValidationFailed(
            f"unknown xref sort key: {sort_by!r}",
            field="sort_by",
        )
    return tuple(
        sorted(
            xrefs,
            key=lambda x: (
                getattr(x, sort_by),
                x.sort_order,
                x.created_at,
                str(x.id),
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class PackageMember:
    """One document pinned into a package (descriptor only, no bytes)."""

    document_id: UUID
    version_number: int
    sha256: str
    size: int

    def __post_init__(self) -> None:
        if self.version_number < 1:
            raise ValueError(f"version_number must be >= 1, got {self.version_number}")
        if self.size < 0:
            raise ValueError(f"size must be >= 0, got {self.size}")
        if not self.sha256:
            raise ValueError("sha256 is required")


@dataclass(frozen=True, slots=True)
class DocPackage:
    """A multi-document export envelope with a manifest (T5 doc ops).

    The manifest (see :meth:`manifest`) is the acceptance artifact:
    entry count, per-entry identity + content address, and a package
    digest over the sorted member digests so a consumer can verify
    "this manifest describes exactly these bytes" without trusting
    transport order.
    """

    id: UUID
    tenant_id: UUID
    name: str
    members: tuple[PackageMember, ...]
    created_by: UUID
    created_at: datetime

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        name: str,
        members: tuple[PackageMember, ...],
        created_by: UUID,
        now: datetime,
    ) -> DocPackage:
        """Construct a validated package (1..MAX members, deduped).

        Raises:
            ValidationFailed: On a blank name, an empty member list,
                an over-cap member list, or a repeated document id (a
                repeated member would double-count bytes in the
                manifest; the caller lists each document once).
        """
        stripped = name.strip() if isinstance(name, str) else ""
        if not stripped or len(stripped) > 200:
            raise ValidationFailed(
                "package name is required (1..200 chars)",
                field="name",
            )
        if not members:
            raise ValidationFailed(
                "a package needs at least one member",
                field="members",
            )
        if len(members) > MAX_PACKAGE_MEMBERS:
            raise ValidationFailed(
                f"package supports at most {MAX_PACKAGE_MEMBERS} members, got {len(members)}",
                field="members",
            )
        seen: set[UUID] = set()
        for member in members:
            if member.document_id in seen:
                raise ValidationFailed(
                    f"document {member.document_id} is listed twice",
                    field="members",
                )
            seen.add(member.document_id)
        return DocPackage(
            id=uuid4(),
            tenant_id=tenant_id,
            name=stripped,
            members=members,
            created_by=created_by,
            created_at=now,
        )

    @property
    def package_sha256(self) -> str:
        """Digest over the sorted member digests (order-independent).

        Sorting before hashing means two packages built from the same
        member set in different insertion orders share the same digest
        — the digest identifies the *set*, not the assembly order.
        """
        joined = "\n".join(sorted(m.sha256 for m in self.members))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def manifest(self) -> dict[str, object]:
        """Return the export manifest (acceptance artifact).

        Carries ids, versions, content addresses, sizes, and the
        package digest — never file bytes or storage refs — so the
        manifest can travel ahead of (or without) the payload.
        """
        return {
            "package_id": str(self.id),
            "tenant_id": str(self.tenant_id),
            "name": self.name,
            "entry_count": len(self.members),
            "package_sha256": self.package_sha256,
            "entries": [
                {
                    "document_id": str(m.document_id),
                    "version_number": m.version_number,
                    "sha256": m.sha256,
                    "size": m.size,
                }
                for m in self.members
            ],
            "created_by": str(self.created_by),
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class DedupCandidate:
    """A document descriptor for duplicate-by-hash grouping."""

    document_id: UUID
    sha256: str


def find_duplicate_groups(
    candidates: tuple[DedupCandidate, ...],
) -> tuple[tuple[UUID, ...], ...]:
    """Group document ids sharing the same ``sha256`` (T5 hash-dedup).

    Returns one tuple per digest observed more than once, each sorted
    by id string for determinism; groups are ordered by first
    occurrence so output is stable across calls. Singletons are not
    duplicates and are omitted. Exact-digest equality only — there is
    no fuzzy matching.
    """
    by_digest: dict[str, list[UUID]] = {}
    order: list[str] = []
    for candidate in candidates:
        bucket = by_digest.get(candidate.sha256)
        if bucket is None:
            by_digest[candidate.sha256] = [candidate.document_id]
            order.append(candidate.sha256)
        elif candidate.document_id not in bucket:
            bucket.append(candidate.document_id)
    groups: list[tuple[UUID, ...]] = []
    for digest in order:
        ids = by_digest[digest]
        if len(ids) > 1:
            groups.append(tuple(sorted(ids, key=str)))
    return tuple(groups)


@dataclass(frozen=True, slots=True)
class CsvRow:
    """One allowlisted result row for CSV export."""

    values: dict[str, str]
    sensitivity: str = "internal"


@dataclass(frozen=True, slots=True)
class CsvExport:
    """A validated CSV export (header + rows + row count)."""

    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...] = field(default=())

    @property
    def row_count(self) -> int:
        """Return the number of data rows (excludes the header)."""
        return len(self.rows)

    def to_text(self) -> str:
        """Render the export as CSV text (header + rows)."""
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(list(self.columns))
        for row in self.rows:
            writer.writerow(list(row))
        return buffer.getvalue()


def build_csv_export(
    *,
    columns: tuple[str, ...],
    rows: tuple[CsvRow, ...],
    include_restricted: bool,
) -> CsvExport:
    """Build an authorized CSV export over allowlisted columns.

    Args:
        columns: The requested projection; every entry must name a
            member of ``CSV_COLUMNS`` (unknown columns raise so a
            caller cannot smuggle a sensitive field into an export).
        rows: Candidate rows; rows whose ``sensitivity`` is
            ``restricted`` or ``phi`` are dropped unless
            ``include_restricted`` is set (the application use case
            only sets it after a ``document.read_restricted`` grant
            check — bundle membership never sets it).
        include_restricted: Whether restricted rows may be emitted.

    Raises:
        ValidationFailed: On an empty/unknown column list, an
            over-cap row list, or a row missing a requested column.
    """
    if not columns:
        raise ValidationFailed("at least one column is required", field="columns")
    for column in columns:
        if column not in CSV_COLUMNS:
            raise ValidationFailed(
                f"unknown csv column: {column!r}",
                field="columns",
            )
    if len(rows) > MAX_CSV_ROWS:
        raise ValidationFailed(
            f"csv export supports at most {MAX_CSV_ROWS} rows, got {len(rows)}",
            field="rows",
        )
    restricted = {"restricted", "phi"}
    rendered: list[tuple[str, ...]] = []
    for row in rows:
        # Why drop (not null): a restricted row must be
        # indistinguishable from "no such row" to an unauthorized
        # exporter (Constitution V) — the same omission rule as
        # ``list_documents`` classification filtering.
        if row.sensitivity in restricted and not include_restricted:
            continue
        try:
            rendered.append(tuple(row.values[column] for column in columns))
        except KeyError as exc:
            raise ValidationFailed(
                f"row is missing column: {exc.args[0]!r}",
                field="rows",
            ) from exc
    return CsvExport(columns=columns, rows=tuple(rendered))


__all__ = [
    "CSV_COLUMNS",
    "MAX_CSV_ROWS",
    "MAX_PACKAGE_MEMBERS",
    "MAX_XREF_LABEL_CHARS",
    "XREF_SORT_KEYS",
    "CsvExport",
    "CsvRow",
    "DedupCandidate",
    "DocPackage",
    "DocXref",
    "PackageMember",
    "build_csv_export",
    "find_duplicate_groups",
    "sort_xrefs",
]
