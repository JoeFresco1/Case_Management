"""Bundle manifest schema version and export audit-payload keys (P14-US8 / FM-060).

Named constants for the portable evidence bundle's wire contract: the
manifest schema version a downstream tool branches on, the audit event
type recorded when an operator pulls a bundle, and the reserved payload
keys of that event. Split out of the former single ``evidence_bundle``
module by the R2 structural refactor; everything here is re-exported
from ``app.application.exports.evidence_bundle`` unchanged.
"""

from __future__ import annotations

from typing import Final

from app.domain.audit import (
    AUDIT_EXPORTED,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Manifest schema version. Bumping it is a wire-contract change;
# downstream tools branch on the value to know which fields to expect.
BUNDLE_SCHEMA_VERSION: Final[str] = "1.0"

# Event type recorded by the export action. The first slice ships a
# dedicated event so the timeline shows "operator pulled bundle X for
# case Y" as its own row. The ``AUDIT_EXPORTED`` constant is re-used
# so the audit log vocabulary stays compact: the payload's
# ``bundle=True`` flag is what tells a consumer "this is a bundle,
# not a structured audit export" (FM-044).
AUDIT_EVIDENCE_BUNDLE_EXPORTED: Final[str] = AUDIT_EXPORTED

# Payload keys for the audit event.
EXPORT_KEY_REASON: Final[str] = "reason"
EXPORT_KEY_CASE_ID: Final[str] = "case_id"
EXPORT_KEY_DOCUMENT_COUNT: Final[str] = "document_count"
EXPORT_KEY_AUDIT_ROW_COUNT: Final[str] = "audit_row_count"
EXPORT_KEY_MANIFEST_HASH: Final[str] = "manifest_hash"
EXPORT_KEY_BUNDLE: Final[str] = "bundle"

__all__ = [
    "AUDIT_EVIDENCE_BUNDLE_EXPORTED",
    "AUDIT_EXPORTED",
    "BUNDLE_SCHEMA_VERSION",
    "EXPORT_KEY_AUDIT_ROW_COUNT",
    "EXPORT_KEY_BUNDLE",
    "EXPORT_KEY_CASE_ID",
    "EXPORT_KEY_DOCUMENT_COUNT",
    "EXPORT_KEY_MANIFEST_HASH",
    "EXPORT_KEY_REASON",
    "Final",
]
