"""Document aggregate (P03-US1, US2, US4 / FM-022).

A document is a first-class metadata + content-addressable object
attached to a case. Identity, classification, sensitivity, and history
are domain concerns; the bytes live behind the ``DocumentStore`` port
(``app.domain.ports.DocumentStore``) so the domain stays storage-agnostic.

# INTEGRATION_SEAM: DocumentStore

The domain knows what a document IS. It does not know whether bytes are
on disk, in Azure Blob, or in any other adapter. A failed
``DocumentStore.put`` raises ``DependencyUnavailable`` from the
application layer; the domain just refuses to record a document whose
content was not durably stored.

# Content addressing

A document is content-addressed by SHA-256: two uploads of the same
bytes produce the same ``sha256`` and the same ``storage_ref``. The
metadata layer (``documents`` row) records identity, classification,
sensitivity, size, and the current version number. ``document_versions``
records the immutable per-version bytes (sha256 + size + storage_ref +
uploader + uploaded_at). Prior versions are preserved when a new version
is added (P03-US2 / FR-006); the first slice adds the *initial* version
on upload only — versioning-by-add (FM-023) is a later slice.

# Validation policy (P03-US4)

A CaseType declares:

- ``document_classifications`` — the allowed labels (e.g. "Contract").
- ``document_max_size_bytes`` — the largest allowed upload.
- ``document_allowed_content_types`` — the allowed media types.

The domain aggregate only owns the per-document invariants (sha256
shape, size non-negative, version monotonic). The application use case
reads the case-type policy and applies it before constructing the
document so the domain stays oblivious of the policy vocabulary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from app.domain.errors import DocumentLocked

# ---------------------------------------------------------------------------
# Constants and shapes (Constitution X / AGENTS.md §16.1)
# ---------------------------------------------------------------------------


# SHA-256 in lowercase hex. 64 chars.
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class DocumentSensitivity(str, Enum):  # noqa: UP042
    """A sensitivity label for a document (P11 / P05-US3)."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"
    PHI = "phi"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_valid_sha256(value: str) -> bool:
    """True iff ``value`` is a 64-char lowercase hex SHA-256 digest.

    The domain accepts only lowercase hex (matches the canonical hash
    from ``hashlib.sha256().hexdigest()``). A non-conforming value is a
    construction bug, not a runtime condition.
    """
    return bool(value) and bool(_SHA256_PATTERN.match(value))


# ---------------------------------------------------------------------------
# DocumentRedaction (P03-US6 / FM-025)
#
# A redaction is a separate, versioned artifact that points at a
# specific version of a document and records a region description.
# The redacted bytes are written through ``DocumentStore.put`` (same
# content-addressing as a fresh upload); the original ``Document``
# aggregate and its ``DocumentVersion`` rows remain untouched so
# the original is still retrievable for audit and re-review.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DocumentRedaction:
    """A versioned redaction of a document.

    The redaction references:

    - the ``document_id`` it applies to,
    - the ``base_version_number`` it was derived from (so the audit
      can prove "redacted from v3", not "redacted from v2 of an
      unrelated edit"),
    - a short ``reason`` (free text, <= 500 chars; no PII / secrets,
      the value is reproduced in the audit payload),
    - the storage_ref of the redacted bytes (content-addressable, so
      identical redacted payloads share storage_ref),
    - the actor + timestamps that produced it.

    Redactions are append-only; there is no UPDATE/DELETE path on the
    aggregate. The status field carries ``ACTIVE`` for the current
    redaction and ``SUPERSEDED`` when a later redaction supersedes a
    prior one on the same document (P03-US6 — "versioned redactions
    preserving original").
    """

    id: UUID
    tenant_id: UUID
    document_id: UUID
    base_version_number: int
    reason: str
    sha256: str
    size: int
    storage_ref: str
    content_type: str
    region: str | None
    status: str  # ACTIVE | SUPERSEDED
    created_by: UUID
    created_at: datetime
    superseded_at: datetime | None
    superseded_by: UUID | None

    def __post_init__(self) -> None:
        if self.base_version_number < 1:
            raise ValueError(f"base_version_number must be >= 1, got {self.base_version_number}")
        if not self.reason or len(self.reason) > 500:
            raise ValueError("reason must be 1..500 chars")
        if not is_valid_sha256(self.sha256):
            raise ValueError(f"invalid sha256 digest: {self.sha256!r}")
        if self.size < 0:
            raise ValueError(f"size must be >= 0, got {self.size}")
        if not self.storage_ref:
            raise ValueError("storage_ref is required")
        if not self.content_type:
            raise ValueError("content_type is required")
        if self.status not in {"ACTIVE", "SUPERSEDED"}:
            raise ValueError(f"status must be ACTIVE or SUPERSEDED, got {self.status!r}")
        if self.region is not None and len(self.region) > 500:
            raise ValueError("region must be <= 500 chars")

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        document_id: UUID,
        base_version_number: int,
        reason: str,
        sha256: str,
        size: int,
        storage_ref: str,
        content_type: str,
        region: str | None,
        actor_id: UUID,
        now: datetime,
    ) -> DocumentRedaction:
        """Construct a fresh ACTIVE redaction row.

        Supersession is a separate transition (the repository marks a
        prior row SUPERSEDED in the same transaction the new row is
        inserted).
        """
        return DocumentRedaction(
            id=uuid4(),
            tenant_id=tenant_id,
            document_id=document_id,
            base_version_number=base_version_number,
            reason=reason,
            sha256=sha256,
            size=size,
            storage_ref=storage_ref,
            content_type=content_type,
            region=region,
            status="ACTIVE",
            created_by=actor_id,
            created_at=now,
            superseded_at=None,
            superseded_by=None,
        )


# ---------------------------------------------------------------------------
# Document + DocumentVersion aggregates
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DocumentVersion:
    """One immutable version of a document's bytes.

    Versions are append-only: a new version never replaces an old one.
    The current version is identified by ``Document.current_version``.
    The domain does not know how the bytes are stored; it carries the
    ``storage_ref`` the storage adapter returned.
    """

    id: UUID
    tenant_id: UUID
    document_id: UUID
    version_number: int
    sha256: str
    size: int
    storage_ref: str
    content_type: str
    uploaded_by: UUID
    uploaded_at: datetime

    def __post_init__(self) -> None:
        if self.version_number < 1:
            raise ValueError("version_number must be >= 1")
        if not is_valid_sha256(self.sha256):
            raise ValueError(f"invalid sha256 digest: {self.sha256!r}")
        if self.size < 0:
            raise ValueError(f"size must be >= 0, got {self.size}")
        if not self.storage_ref:
            raise ValueError("storage_ref is required")
        if not self.content_type:
            raise ValueError("content_type is required")


@dataclass(frozen=True, slots=True)
class Document:
    """The metadata aggregate for a case-attached document.

    The first-slice aggregate covers P03-US1 (upload + metadata +
    DocumentAdded audit) and is the substrate for P03-US2 (versioning)
    and P03-US3 (search). Sensitivity and classification follow
    P05-US5 (document classification taxonomy). Search flags and
    retention flags land in later slices.
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    file_name: str
    content_type: str
    classification: str  # one of the case-type's document_classifications
    sensitivity: DocumentSensitivity
    sha256: str
    size: int
    current_version: int
    storage_ref: str
    uploaded_by: UUID
    created_at: datetime
    updated_at: datetime
    # P03-US8: when ``True`` the document refuses any mutating operation
    # (new version, redaction, deletion). The flag is set by
    # ``Document.with_lock`` and is irreversible in the first slice.
    locked: bool = False
    locked_at: datetime | None = None
    locked_by: UUID | None = None

    def __post_init__(self) -> None:
        if not self.file_name:
            raise ValueError("file_name is required")
        if not self.content_type:
            raise ValueError("content_type is required")
        if not self.classification:
            raise ValueError("classification is required")
        if not is_valid_sha256(self.sha256):
            raise ValueError(f"invalid sha256 digest: {self.sha256!r}")
        if self.size < 0:
            raise ValueError(f"size must be >= 0, got {self.size}")
        if self.current_version < 1:
            raise ValueError("current_version must be >= 1")
        if not self.storage_ref:
            raise ValueError("storage_ref is required")

    # ----- factories ---------------------------------------------------------

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        case_id: UUID,
        file_name: str,
        content_type: str,
        classification: str,
        sensitivity: DocumentSensitivity,
        sha256: str,
        size: int,
        storage_ref: str,
        uploaded_by: UUID,
        now: datetime,
    ) -> Document:
        """Construct the metadata for a freshly uploaded document.

        The first version is implicit: ``current_version=1`` is the
        initial content. The corresponding ``DocumentVersion`` row is
        added by the application use case.
        """
        return Document(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=case_id,
            file_name=file_name,
            content_type=content_type,
            classification=classification,
            sensitivity=sensitivity,
            sha256=sha256,
            size=size,
            current_version=1,
            storage_ref=storage_ref,
            uploaded_by=uploaded_by,
            created_at=now,
            updated_at=now,
        )

    # ----- transitions -------------------------------------------------------

    def with_new_version(
        self,
        *,
        new_sha256: str,
        new_size: int,
        new_storage_ref: str,
        now: datetime,
    ) -> Document:
        """Return a copy whose current_version pointer is bumped.

        The prior ``DocumentVersion`` row remains; the caller appends
        a new row to ``document_versions``. P03-US2 / FR-006 — prior
        content is preserved and the current version is identified.

        Locked documents refuse new versions (P03-US8): the error
        surfaces before any bytes are stored so a locked document
        cannot be silently overwritten.
        """
        if self.locked:
            raise DocumentLocked(f"document {self.id} is locked; new versions are refused")
        if not is_valid_sha256(new_sha256):
            raise ValueError(f"invalid sha256 digest: {new_sha256!r}")
        if new_size < 0:
            raise ValueError(f"size must be >= 0, got {new_size}")
        if not new_storage_ref:
            raise ValueError("storage_ref is required")
        return replace(
            self,
            sha256=new_sha256,
            size=new_size,
            storage_ref=new_storage_ref,
            current_version=self.current_version + 1,
            updated_at=now,
        )

    def with_lock(self, *, actor_id: UUID, now: datetime) -> Document:
        """Return a copy of this document with the lock flag enabled.

        Locking is idempotent in the sense that the resulting
        ``locked`` flag is the same regardless of how many times it is
        applied, but the operation itself records the actor + timestamp
        of the **first** lock event. The repository persists a
        ``DocumentLocked`` audit row so the history shows who performed
        the action.

        Raises ``DocumentLocked`` if the document is already locked —
        the second attempt is a distinct auditable event in the
        ``DocumentLockRefused`` row, not a silent no-op.
        """
        if self.locked:
            raise DocumentLocked(f"document {self.id} is already locked")
        return replace(
            self,
            locked=True,
            locked_at=now,
            locked_by=actor_id,
            updated_at=now,
        )

    def with_sensitivity(self, *, sensitivity: DocumentSensitivity, now: datetime) -> Document:
        """Return a copy with an updated sensitivity label (FM-025 restrict).

        Restriction is the filing-safety primitive: raising a document
        to ``restricted`` / ``phi`` moves all future reads under the
        stricter ``document.read_restricted`` right (see
        ``app.domain.document_protection``). The transition refuses on
        a filed (``locked``) document so a filed record cannot be
        silently reclassified.

        Args:
            sensitivity: The new label. Callers validate membership;
                this method enforces the lock gate only.
            now: The authoritative timestamp for ``updated_at``.

        Raises:
            DocumentLocked: When the document is filed.
        """
        if self.locked:
            raise DocumentLocked(f"document {self.id} is filed; sensitivity change refused")
        return replace(self, sensitivity=sensitivity, updated_at=now)


__all__ = [
    "Document",
    "DocumentRedaction",
    "DocumentSensitivity",
    "DocumentVersion",
    "is_valid_sha256",
]
