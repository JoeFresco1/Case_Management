"""Application use cases for portable case evidence bundles (P14-US8 / FM-060).

A bundle is a self-contained, offline-reviewable archive of one case:
the case aggregate, every document attached to it, the full audit
timeline, and a top-level manifest. The archive is portable (ZIP), the
redaction policy is the same as the timeline view, and the bundle is
itself an audit event so the action is visible on the chain.

# INTEGRATION_SEAM: evidence bundle

The use case in :mod:`app.application.exports.evidence_bundle` is the
boundary that decides **what leaves the platform**. The HTTP layer
adapts the request to the use case's command shape; nothing in the
domain or application layer knows about FastAPI or HTTP. The
persistence adapter (``SqlAuditEventStore``) is the durable
counterpart of the in-memory fake used in tests.
"""

from __future__ import annotations

from app.application.exports.evidence_bundle import (
    AUDIT_EVIDENCE_BUNDLE_EXPORTED,
    BuildEvidenceBundleCommand,
    EvidenceBundle,
    EvidenceBundleEntry,
)
from app.application.exports.evidence_bundle import (
    handle as build_evidence_bundle,
)

__all__ = [
    "AUDIT_EVIDENCE_BUNDLE_EXPORTED",
    "BuildEvidenceBundleCommand",
    "EvidenceBundle",
    "EvidenceBundleEntry",
    "build_evidence_bundle",
]
