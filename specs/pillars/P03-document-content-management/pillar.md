# Pillar P03 — Document & Content Management

**Status**: Draft · **Owner**: Platform team · **Updated**: 2026-08-31

> Documents are first-class objects associated with cases. Storage is abstracted behind a port.

## 1. Pillar statement

This pillar owns the **document** as a first-class domain object: identity, metadata, version history, content addressing, classification, sensitivity, access. It does **not** own how bytes are stored on disk or in blob storage — that is behind the `DocumentStore` port (Constitution X). The domain knows documents; the infrastructure knows disks.

A document is associated with one case and optionally a task, decision, or workflow activity. The pillar guarantees:

- content addressing by SHA-256 (same content → same content ref; no accidental duplicates),
- per-document version history with the current version identified,
- separation of identity/metadata from physical storage,
- access control at the document level (delegated to P11),
- audit of every access (delegated to P09),
- safe handling of untrusted file content (Constitution V.7).

## 2. Scope

### In scope
- Document metadata: file name, media type, classification, sensitivity, hash, size, version, uploader, timestamps.
- Document version history.
- Content-addressable storage with a pluggable `DocumentStore` port.
- File type and size validation.
- Quarantine state for failed scan / failed validation.
- Safe filename handling; protected download authorization.
- Per-document access control; per-document access audit.
- Detecting accidental duplicate uploads via content hash.

### Out of scope (deferred)
- Real-time document scanning (e.g., virus scanning, OCR, PII detection). Pillar P04 owns capture; the scan seam is here but the actual scanner is a P14 integration.
- Document-level collaboration (comments, annotations, redactions).
- Document-level workflows (e.g., "this document needs two approvals before it is final").
- eSignature.

### Explicitly not building
- Direct filesystem paths exposed in the API. Content is addressed; URLs are expiring and authorization-checked.
- A document store that depends on a particular vendor's SDK in the domain layer.

## 3. Primary entities

| Entity | Purpose | First slice |
|---|---|---|
| `Document` | A file or document-like artifact associated with a case. | Yes |
| `DocumentVersion` | One version of a document. | Yes |
| `DocumentClassification` | Business-level label (e.g., "Contract", "LabResult", "Correspondence"). | Yes |
| `DocumentStore` (port) | Pluggable storage adapter. | Yes (local FS adapter) |
| `ScanResult` | The outcome of a content scan (stub for v1). | Deferred |

## 4. User stories (priority-tiered)

### P1 — Critical
- **P03-US1**: As a case worker, I can upload a document to a case. The system validates type and size, computes a content hash, stores the content through the configured adapter, records metadata, and writes a `DocumentAdded` audit event in the same transaction.
- **P03-US2**: As a case worker, I can add a new version of a document. The prior version is preserved; the current version is identified; the version transition is audited.

### P2 — Important
- **P03-US3**: As a case worker, I can find documents within a case by metadata (file name, classification, uploader, date range) and where configured by content. Search results enforce the same access controls as direct viewing.
- **P03-US4**: As a process administrator, I can configure which file types and size limits apply to a case type, and the system rejects uploads that violate these.
- **P03-US5**: As a process administrator, I can configure a required-document rule for a case type: a case cannot transition past a state unless the required documents are present.

### P3 — Deferred
- **P03-US6**: As a case worker, I can request a redaction on a document. Redactions are themselves audited and versioned.
- **P03-US7**: As a records administrator, I can configure a retention policy on a document classification (e.g., destroy after 7 years unless legal hold).
- **P03-US8**: As a case worker, I can lock a document so it cannot be modified or deleted (e.g., once filed).

## 5. Cross-cuts

- **P01 (Case)**: a Document is associated with a Case; the case shows document references.
- **P04 (Capture & Ingestion)**: scan integration lives in P04; the quarantine state lives here.
- **P05 (Metadata)**: classification and sensitivity are metadata, validated against the case type's document configuration.
- **P08 (Records & Retention)**: retention and disposition are P08; this pillar provides the seams (status, immutable flag).
- **P09 (Audit)**: every access and every version transition is audited.
- **P11 (Security)**: per-document access control; the download URL is expiring and authorization-checked.

## 6. Acceptance scenarios (representative)

- C1: An authorized user uploads a 2 MB PDF to an existing case. The document is stored behind the `DocumentStore` adapter, the metadata row is created, a `DocumentAdded` event is emitted, and the document appears in the case workspace.
- C2: The same content is uploaded twice to the same case. The second upload is detected by hash and the user is informed; the metadata is not duplicated.
- C3: An upload exceeds the configured size limit. The system rejects the upload, no partial content is stored, and a `DocumentRejected` audit event is recorded.
- C4: A user without permission to view a sensitive document attempts to download it via a guessed URL. The system returns `forbidden`; no content is disclosed.
- C5: A new version of a document is uploaded. The prior version remains retrievable; the current version pointer updates; a `DocumentVersionAdded` event is emitted.

## 7. Planned slices

| Slice | Status | Notes |
|---|---|---|
| `001-case-management-platform` | Active | First slice. Implements P03-US1, US2, US4, US5 (basic). Scan stub only. |
| `002-leftovers` Run C | Draft | Content depth (FM-025, FM-023 ext): restriction/redaction/file-lock policy kernel, rendition burns via the existing `DocumentStore` port (original preserved, both views audited), annotation/checkout/re-index viewer pipeline, x-refs/separation-plans/packages/hash-dedup/CSV, permissioned bundles + share-by-link, worker home under admin policy. See `specs/002-leftovers/architecture-runC.md`, `docs/decisions/ADR-041-runC-rendition-store-adapter.md`, `docs/decisions/ADR-042-runC-bundle-permission-model.md`. |
| Future slice 2 | Planned | Search within a case (US3), quarantine UI, scan integration. |
| Future slice 3 | Planned | Redaction (US6), retention (US7), lock (US8). |

## 8. Open questions

- Q1: What is the default maximum file size and which file types are allowed by default? Should be configurable per case type.
- Q2: When a document's classification changes after upload, is the existing content reclassified retroactively, or only going forward? (Current intent: going forward only; historical metadata preserved.)
- Q3: Is a document a single file, or can it be a structured artifact (a folder, a multi-file package)? (Current intent: single file in v1; multi-file packages are a P3 extension.)
