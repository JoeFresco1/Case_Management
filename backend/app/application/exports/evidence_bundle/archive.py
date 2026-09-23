"""ZIP archive and streaming-entry builders for the evidence bundle.

Assembles the two archive representations of a bundle: the pre-built
in-memory ZIP blob for callers that want one byte string, and the
ordered per-entry list the HTTP router streams without buffering the
full archive. Both share the manifest/entry projection so the streamed
response and the ZIP carry identical bytes. Split out of the former
single ``evidence_bundle`` module by the R2 structural refactor;
everything here is re-exported from
``app.application.exports.evidence_bundle`` unchanged.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

from .helpers import _sha256_hex
from .types import EvidenceBundleEntry

# ---------------------------------------------------------------------------
# Archive helpers
# ---------------------------------------------------------------------------


def _manifest_entry(name: str, body: bytes) -> dict[str, Any]:
    """A single manifest entry: name + sha256 + size."""
    return {
        "name": name,
        "sha256": _sha256_hex(body),
        "size": len(body),
    }


def _build_archive(
    *,
    manifest_bytes: bytes,
    case_bytes: bytes,
    document_metadata_bytes: dict[str, bytes],
    document_content_bytes: dict[str, bytes],
    audit_bytes: bytes,
) -> bytes:
    """Serialize the bundle to a single ZIP blob.

    The manifest sits at the archive root so a downstream tool can
    read it first and decide which other entries to verify. The
    case and audit entries are JSON; the document bytes are
    written verbatim under ``documents/<id>/content.bin``.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", manifest_bytes)
        zf.writestr("case.json", case_bytes)
        for name, body in document_metadata_bytes.items():
            zf.writestr(name, body)
        for name, body in document_content_bytes.items():
            zf.writestr(name, body)
        zf.writestr("audit.json", audit_bytes)
    return buffer.getvalue()


def _build_entry_list(
    *,
    manifest_bytes: bytes,
    case_bytes: bytes,
    document_metadata_bytes: dict[str, bytes],
    document_content_bytes: dict[str, bytes],
    audit_bytes: bytes,
) -> list[EvidenceBundleEntry]:
    """Return the per-entry list for callers that want streaming.

    The router prefers this form so the response can stream the
    ZIP without buffering the full archive in memory beyond what
    ``zipfile`` needs to flush.
    """
    entries: list[EvidenceBundleEntry] = [
        EvidenceBundleEntry(
            name="manifest.json", data=manifest_bytes, content_type="application/json"
        ),
        EvidenceBundleEntry(name="case.json", data=case_bytes, content_type="application/json"),
    ]
    for name, body in document_metadata_bytes.items():
        entries.append(EvidenceBundleEntry(name=name, data=body, content_type="application/json"))
    for name, body in document_content_bytes.items():
        entries.append(
            EvidenceBundleEntry(
                name=name,
                data=body,
                content_type="application/octet-stream",
            )
        )
    entries.append(
        EvidenceBundleEntry(name="audit.json", data=audit_bytes, content_type="application/json")
    )
    return entries


__all__ = [
    "_build_archive",
    "_build_entry_list",
    "_manifest_entry",
    "io",
    "zipfile",
]
