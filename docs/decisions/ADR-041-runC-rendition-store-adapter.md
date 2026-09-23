# ADR-041: Rendition bytes reuse the DocumentStore port (Run C)

- Status: Accepted (2026-09-14)
- Scope: `burn_rendition` (FM-025 / T5-US1), `Rendition.storage_ref` rows,
  `GET /documents/{id}/content?rendition=` download path (FM-023 ext).
- Supersedes: nothing (answers `plan.md §7`-adjacent seam question for the
  viewer pipeline: new byte port vs reuse).
- Related: AGENTS.md R1/R10/R12, `seams.md` row 2 (Document store),
  ADR-037 (redaction boundary), `specs/002-leftovers/architecture-runC.md`
  §3 (redaction-version sequence), `app/domain/ports/infrastructure.py:13`
  (`DocumentStore`), `app/domain/annotations.py:360`
  (`Redaction.burn_as_rendition`), `app/application/documents/viewer.py:574`
  (`burn_rendition`), `app/infrastructure/storage/local_document_store.py:3`.

## Context

Run C burns approved redaction marks as new `REDACTED` rendition bytes
(T5 acceptance: "redaction creates a new version; original retrievable
under stricter right; both audited"). The domain shapes the rendition
descriptor (`Rendition` carries `sha256` + `size` + `storage_ref` +
`content_type` + `redaction_id`) but never touches bytes (R11). The
application layer must put the burned bytes somewhere durable *before*
staging the row, so a row always points at stored content. The question
is whether renditions need their own byte port (`RenditionStore`) or
reuse the existing `DocumentStore` port behind `seams.md` row 2.

## Options considered

1. **New `RenditionStore` port with its own adapters.**
   - Pros: renditions and originals could scale/lifecycle independently
     (e.g. thumbnails on a CDN, originals in immutable blob tier).
   - Cons: doubles the storage seam (two local adapters, two Azure Blob
     placeholders, two `DOCUMENTS_PATH`-style configs, two health entries)
     for bytes with identical durability needs; every future Azure swap
     must implement two contracts in lockstep; no caller today needs a
     different durability tier for a redacted burn vs a filed version.
     Rejected for H1.
2. **Reuse `DocumentStore.put` for rendition bytes (chosen).**
   - Pros: one seam, one swap (local FS today, `AzureBlobDocumentStore`
     tomorrow); content addressing (`sha256`) already dedupes identical
     burns at the store layer; the *row* (`document_renditions`) still
     distinguishes kinds (`RenditionKind.REDACTED` + `redaction_id` vs
     `ORIGINAL` without one), so the revision/rendition history view
     and the per-rendition `DocumentAccessed` audit rows keep full
     provenance without a second byte contract.
   - Cons: store-level lifecycle (e.g. "purge derived thumbnails but
     never originals") cannot be expressed as a port capability —
     acceptable because no retention rule targets renditions in H1
     (T6 owns retention; renditions inherit the parent document's hold
     via the document rows, never via store deletion).

## Decision

Option 2. `burn_rendition` calls `DocumentStore.put` with the burned
bytes and shapes the returned opaque `storage_ref` into the `Rendition`
row via the domain propose/apply/burn flow (`Redaction.propose` →
`apply` → `burn_as_rendition`, so the "only applied marks burn"
invariant is exercised on every burn, not just in domain unit tests).
`DocumentStore.put` raising `DependencyUnavailable` rolls the whole
transaction back with no audit row appended. Derived
thumbnail/preview kinds (`THUMBNAIL`/`PREVIEW`) are resolver-only
preferences in `resolve_viewer_rendition` — no bytes are fanned out
for them in H1.

## Consequences

- `test_restrict_rendition.py` + `test_viewer.py` pin the contract:
  burn stores new bytes, source `DocumentVersion` rows are never
  modified, original download requires the stricter right, both views
  audit independently.
- Per-rendition content types are preserved (the redacted bytes carry
  their own stored type; the download router serves it instead of the
  parent's — `routers/documents.py` rendition branch).
- Identical burns share store content by digest while each burn keeps
  its own auditable row (`redaction_id` links the bytes to the mark
  that authorized them).

## Azure-seam note

No new seam. `seams.md` row 2 covers rendition bytes: the local
`LocalDocumentStore` serves burns today; `AzureBlobDocumentStore`
serves them tomorrow with no domain/application change. A future
CDN-tier split for derived thumbnails would add a *read-through*
cache in front of the same port, not a second port.
