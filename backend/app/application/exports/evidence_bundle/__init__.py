"""Portable case evidence bundle use case package (P14-US8 / FM-060).

# Why a package (R2 structural split)

Formerly a single ``evidence_bundle.py`` module of 753 lines — past the
R2 600-line soft limit. Split by responsibility into :mod:`constants`,
:mod:`types`, :mod:`helpers`, :mod:`archive`, and :mod:`use_case`. This
``__init__`` is a pure re-export shim: ``__all__`` and every previously
visible attribute are re-exported unchanged, so no consumer import line
needs editing (the package re-export in
``app.application.exports``, the HTTP router
``app.interfaces.http.routers.exports``, and
``tests/exports/test_bundle.py`` included).

# The bundle

A bundle is the offline-reviewable archive a process administrator
exports when a case enters legal hold or a formal review. The
archive contains:

- ``manifest.json`` — top-level metadata: case id, reference, export
  actor, export timestamp, schema version, and the SHA-256 of every
  other entry so a downstream tool can verify integrity.
- ``case.json`` — the case aggregate (with restricted fields redacted
  per the case-type schema, mirroring the timeline allowlist).
- ``documents/`` — one folder per document, with ``metadata.json``
  (Document + DocumentVersion shape, classification preserved,
  sensitivity preserved) and ``content.bin`` (the bytes the
  ``DocumentStore`` returns, content-addressed by SHA-256).
- ``audit.json`` — the full per-case timeline, including the
  ``hash`` / ``prev_hash`` anchors and the chain-verification result
  so a downstream auditor can replay the chain off-system.

The bundle is **itself an audit event** (``EvidenceBundleExported``):
running the export appends a row to the chain so the action is
visible alongside the case it documents. The event payload carries
the bundle's manifest hash, the document count, the audit row count,
and the reason the export was pulled.

# Authorization (Constitution V / AGENTS.md §4.R12)

The use case requires ``Action.CASE_READ`` for the case itself and
``Action.DOCUMENT_READ`` for every document in the result. Both
checks go through :func:`app.application.authorization.authorize` so
the live role-assignment table is consulted. A denied action raises
:class:`Forbidden` and the HTTP layer maps it to 403; a missing or
cross-tenant case raises :class:`NotFound` and the HTTP layer maps
it to 404. The two are intentionally distinct (Constitution V — a
forbidden actor must not be able to probe for case existence).

# Redaction (P11-US6 / P05-US3 / FM-029)

Two redaction layers, matching the existing surfaces:

1. The case's ``data`` field is redacted through
   :func:`app.application.cases.redactor.redact_case_data` using the
   case-type schema's sensitive-field allowlist. The redactor
   requires ``CASE_READ_SENSITIVE`` to be granted; the use case only
   grants it when ``can_see_sensitive=True`` is requested explicitly
   (the v1 default is ``False`` because the bundle travels off the
   platform and the export action is its own authorization
   boundary).
2. The audit payload is redacted through the same
   ``PAYLOAD_SAFE_KEYS`` allowlist the timeline view uses
   (``app.application.audit.view_timeline``). The chain anchors
   (``hash`` / ``prev_hash``) are NOT redacted — they are integrity
   metadata, not payload content.

# Failure semantics

- Auth denial on the case -> ``Forbidden`` (HTTP 403).
- Auth denial on a document -> ``Forbidden`` (HTTP 403); the bundle
  refuses to ship a partial archive.
- Missing case (incl. cross-tenant) -> ``NotFound`` (HTTP 404).
- Document store failure -> propagated unchanged; the use case does
  not catch ``DependencyUnavailable`` so a partial bundle surfaces
  as a 503 at the HTTP boundary.

# INTEGRATION_SEAM: document store

The bytes for every document in the archive are read through the
``DocumentStore`` port (``app.domain.ports.DocumentStore``). The
local filesystem adapter satisfies the port in development; an
Azure Blob adapter replaces it in production without code changes
(AGENTS.md §12).
"""

from __future__ import annotations

from .archive import (  # noqa: F401
    _build_archive,
    _build_entry_list,
    _manifest_entry,
    io,
    zipfile,
)
from .constants import (  # noqa: F401
    AUDIT_EVIDENCE_BUNDLE_EXPORTED,
    AUDIT_EXPORTED,
    BUNDLE_SCHEMA_VERSION,
    EXPORT_KEY_AUDIT_ROW_COUNT,
    EXPORT_KEY_BUNDLE,
    EXPORT_KEY_CASE_ID,
    EXPORT_KEY_DOCUMENT_COUNT,
    EXPORT_KEY_MANIFEST_HASH,
    EXPORT_KEY_REASON,
    Final,
)
from .helpers import (  # noqa: F401
    UTC,
    UUID,
    Any,
    Case,
    Document,
    DocumentRepository,
    DocumentSensitivity,
    ValidationFailed,
    _ActorProxy,
    _audit_row_to_dict,
    _canonical_json,
    _case_to_dict,
    _document_to_dict,
    _list_documents_for_case,
    _now,
    _now_iso,
    _redact_audit_payload,
    _redact_case_data,
    _sha256_hex,
    _validate_command,
    dataclass,
    datetime,
    hashlib,
    json,
    redact_case_data,
    view_timeline_uc,
)
from .types import (  # noqa: F401
    BuildEvidenceBundleCommand,
    EvidenceBundle,
    EvidenceBundleEntry,
)
from .use_case import (  # noqa: F401
    Action,
    AuditEventStore,
    CaseRepository,
    CaseTypeVersionRepository,
    DocumentStore,
    NotFound,
    RecordAuditCommand,
    Resource,
    _payload_hash,
    authorize,
    build_evidence_bundle,
    handle,
    record_event,
    uuid4,
)

__all__: list[str] = [
    "AUDIT_EVIDENCE_BUNDLE_EXPORTED",
    "BUNDLE_SCHEMA_VERSION",
    "BuildEvidenceBundleCommand",
    "EvidenceBundle",
    "EvidenceBundleEntry",
    "build_evidence_bundle",
    "handle",
]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, but the pre-split single-file module exposed no such
# attributes. Popping them keeps ``dir(app.application.exports.evidence_bundle)``
# identical to the old module's attribute surface (``__path__`` aside) — the
# R2 refactor must be invisible. The submodules stay importable
# (``...evidence_bundle.helpers`` resolves through ``sys.modules``, which is
# untouched); only the redundant package-level attributes go away.
for _submodule in (
    "archive",
    "constants",
    "helpers",
    "types",
    "use_case",
):
    globals().pop(_submodule, None)
del _submodule
