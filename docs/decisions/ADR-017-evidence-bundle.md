# ADR-017 — Portable Case Evidence Bundle (FM-060 / P14-US8)

**Status:** Accepted
**Date:** 2026-09-02
**Owner:** Platform team
**Related Pillars:** P14 Integrations & Migration, P01 Case & Workspace, P03 Document, P09 Audit & Evidence, P11 Security & Identity
**Pillar Questions:** `specs/pillars/P14-integrations-migration/pillar.md` US8, §2 (Out of scope — "smart migrator" / "marketplace"); `specs/pillars/01-deduplicated-feature-map.md:124` (FM-060).
**Tasks/Features Affected:** FM-060; `backend/app/application/exports/evidence_bundle.py:1`; `backend/app/interfaces/http/routers/exports.py:1`; `backend/app/interfaces/http/schemas/exports.py:1`; `backend/tests/exports/test_bundle.py:1`; AGENTS.md §3 R1, §4.R7, §4.R12, §12.

## Context

Pillar P14 (`specs/pillars/P14-integrations-migration/pillar.md`) commits to an "incremental migration from legacy platforms" without cloning or depending on legacy. The pillar's H3 story is P14-US8: **"As an operator, I can export a case bundle (case, documents, audit timeline) to a portable archive for offline review or legal hold."** The user story is P3 in the priority-tiered section, but FM-060 (`specs/pillars/01-deduplicated-feature-map.md:124`) lifts it to a first-slice deliverable because the legal-hold workflow cannot wait for the named connector authorization.

The hard rule is constitutional, not aspirational:

- **Constitution V** (server-side enforcement, fail-closed, deny-by-default) — the bundle is the most exportable surface the platform has; a misconfigured role grant must not silently ship PHI to a USB stick.
- **Constitution IX** (audit immutability) — the bundle is a snapshot; the audit chain anchors it points to must remain valid after the bundle is unpacked. The bundle's audit section is therefore a *copy* of the chain, not a *reference* to it.
- **Constitution XII** (no legacy clone) — the bundle is a one-way pull, never a push. There is no reverse channel; a downstream tool cannot push changes back into the platform through the bundle.

Four questions are not answered by the pillar and must be settled here:

1. **What is in the archive?** The pillar says "case, documents, audit timeline." That is necessary but not sufficient: a portable archive must be *self-describing* (manifest with the SHA-256 of every entry) and *self-verifying* (chain anchors that an off-system auditor can replay). Without a manifest the bundle is a tarball; without chain anchors the audit section is untrustworthy.
2. **How is redaction decided?** The pillar is silent. The redactor today (P11-US6 / FM-029) is a pure function over the case-type schema; the audit timeline has its own allowlist (`PAYLOAD_SAFE_KEYS` in `app.application.audit.view_timeline`). The bundle must use *both* so an export is no more revealing than the inline read path.
3. **What authorization gates the export?** The pillar's "operator" vocabulary does not name an action. The choice is between `case.read` (the same gate as the inline case view) and a new `bundle.export` action. The latter would prevent an operator who can read a case from also pulling the case for legal hold — but it also adds an action the platform must audit and grant.
4. **What does the export itself do to the audit chain?** The structured audit export (FM-044) appends an `AuditExported` row. The bundle is *another* flavor of export, but it is heavier (it ships documents), so conflating the two would degrade the timeline's signal-to-noise.

## Decision

The evidence bundle is implemented as four layers plus two authorization surfaces plus one audit-event type:

### 1. Application use case (`backend/app/application/exports/evidence_bundle.py`)

`build_evidence_bundle.handle(BuildEvidenceBundleCommand, ...)` is the canonical entry point. The use case:

1. **Validates** the command (non-empty `reason`; the reason lands on the chain verbatim, so an empty reason is a structured 422).
2. **Authorizes** the case read via `Action.CASE_READ` (Constitution V — server-side enforcement).
3. **Resolves** the case (cross-tenant probes surface as `NotFound`; the API never confirms cross-tenant existence).
4. **Resolves** the case-type version to read the redaction allowlist (`schema.sensitive_field_keys()`).
5. **Authorizes** the document read in aggregate via `Action.DOCUMENT_READ` on the case resource (the first-slice policy does not carry a per-document read action; the case scope is sufficient because every document on a case shares the same classification policy).
6. **Fetches** every document through `DocumentRepository.list_for_case` (the same hook the GraphQL governed surface uses — the application layer never invents its own tenant-wide listing).
7. **Reads** the bytes through the `DocumentStore` port (`content_ref` keyed); a storage failure propagates as `DependencyUnavailable` so a partial bundle surfaces as 503.
8. **Fetches** the audit timeline through `AuditEventStore.list_for_case` and redaction-strips every payload through `PAYLOAD_SAFE_KEYS` (the same allowlist the timeline view uses).
9. **Builds** the manifest, the ZIP archive, and the audit event that records the export action.

The use case returns an `EvidenceBundle` with both a pre-built ZIP blob (CLI, tests) and an `entries` list (HTTP streaming). The router uses the blob; a future CLI uses the entries.

### 2. HTTP router (`backend/app/interfaces/http/routers/exports.py`)

`POST /api/v1/exports/bundle` is the only entry point. The handler:

- Translates the request body into a `BuildEvidenceBundleCommand`.
- Calls the use case with the in-memory and SQL adapters wired from the request's session.
- Streams the pre-built ZIP archive as `application/zip` with a deterministic filename (`case-<id>-evidence-bundle-<manifest-prefix>.zip`) and three response headers: `X-Bundle-Manifest-Sha256`, `X-Document-Count`, `X-Audit-Row-Count`.
- Commits the audit row in the same transaction as the read so a failure rolls back both.

Errors are translated through the standard `DomainError` handler: `Forbidden` → 403, `NotFound` → 404, `ValidationFailed` → 422, `DependencyUnavailable` → 503.

### 3. Redaction contract

Two redaction layers, matching the existing surfaces:

| Surface | Redactor | Source of truth | Default posture |
|---|---|---|---|
| Case `data` | `app.application.cases.redactor.redact_case_data` | `CaseTypeSchema.sensitive_field_keys()` | `can_see_sensitive=False` (the bundle's default) |
| Audit payload | `app.application.audit.view_timeline.PAYLOAD_SAFE_KEYS` | the timeline allowlist (P09-US1) | every non-allowlist key replaced with `"***"` |
| Chain anchors | none | `prev_hash`, `hash` are integrity metadata, not payload | preserved verbatim |

The redactor is **pure** and is shared with the inline case view; a field that is not redacted on the workspace is not redacted in the bundle, and a field that is redacted on the workspace is redacted in the bundle. The two surfaces are equivalent by construction.

### 4. Archive shape

```
case-<id>-evidence-bundle-<hash>.zip
├── manifest.json          # schema_version, case id+ref, exported_at/by, reason, entries[...]
├── case.json              # Case aggregate with redacted data + redacted_fields
├── audit.json             # per-case timeline with chain anchors + redacted payloads
└── documents/
    └── <document_id>/
        ├── metadata.json  # Document + DocumentVersion shape
        └── content.bin    # DocumentStore.get(content_ref) bytes
```

The manifest is at the archive root so a downstream tool reads it first and decides which entries to verify. The manifest carries `sha256` and `size` for every other entry; the integrity contract is "every entry's bytes hash to the value listed in the manifest." A `manifest_hash` SHA-256 over the canonical JSON `manifest.json` is included in the response header and the audit event payload so a third-party auditor can verify the manifest itself without unpacking the archive.

### 5. Authorization: `case.read` + `document.read`

The bundle requires `case.read` on the case and `document.read` on the case resource. There is **no** separate `bundle.export` action. The rationale: legal-hold is an extension of "I can read this case" — adding a new action would mean operators with `case.read` could not pull a legal-hold bundle without a separate role grant, which is exactly the wrong failure mode for the use case. The denial is fail-closed: an actor with `case.read` but without `document.read` (a hypothetical future role) is refused at the document step with `Forbidden`. The use case never builds a partial archive.

A future Gated action (`bundle.export` for cross-tenant legal-hold by a security administrator) is documented but not implemented. The Authorization `Action` enum is the right place to add it when the product decision lands.

### 6. Audit event: `AuditExported` with `bundle=true`

The export appends an `AuditExported` row with `payload.bundle=true`. The choice to re-use `AuditExported` rather than introduce `EvidenceBundleExported` is deliberate: the audit log vocabulary stays compact, the per-case timeline keeps one row per export action, and the `bundle` flag is the discriminator downstream consumers key on. The payload carries `reason`, `case_id`, `document_count`, `audit_row_count`, and `manifest_hash` so a security administrator reading the timeline can answer "what did this operator pull and when" without unpacking the archive. The chain anchor (the row's `hash` / `prev_hash`) lands on the same per-tenant chain as the events it documents — the export is itself a security-relevant event (P09-US5).

## Consequences

### Positive

- The legal-hold workflow ships in the first slice. An operator can pull a self-contained, self-verifying archive for any case they can read; a downstream auditor can verify the case aggregate, the documents, and the audit chain without contacting the platform.
- The redaction contract is shared with the inline read path. The two surfaces cannot drift: a field that is redacted on the workspace is redacted in the bundle.
- The bundle is **append-only by construction**. A new bundle is a new audit row; the original audit chain is never mutated. The bundle is therefore legal-hold-safe: a court order that pulls the bundle does not change the chain the bundle documents.
- The archive is portable. A downstream tool needs nothing more than `unzip` and the canonical JSON codec; the manifest is the only required reading.

### Negative

- The bundle ships with the default `can_see_sensitive=False`. An operator who needs the unredacted view must pass `can_see_sensitive=true` and the use case re-evaluates `case.read_sensitive` — a misconfigured role cannot silently bypass the redaction, but the wire shape now has an opt-in flag that must be documented in the operator console.
- The use case does not stream the ZIP. A case with thousands of documents or a million-row audit timeline would build the archive in memory. The first slice is acceptable because both counts are tenant-scoped and reasonable; a future slice can stream through `zipfile.ZipFile` when the bound stops being reasonable.
- The audit event re-uses `AuditExported` rather than introducing `EvidenceBundleExported`. Downstream consumers that already filter `AuditExported` for the structured audit export will see the bundle rows too; the `bundle=true` flag is the only discriminator. The trade-off is "smaller vocabulary, more filter logic"; a future ADR may add a dedicated event type if the timeline signal degrades.

### Neutral

- The archive is a ZIP. ZIP is the lowest-common-denominator portable format; the manifest's `schema_version` field is the seam where a future format (tar.gz, signed C2PA, content-addressed IPLD) plugs in without changing the use case.
- The export commits the audit row in the caller's transaction. A future "export without an audit row" (e.g. for a security administrator's dry-run) would need a separate command flag; today the audit row is unconditional because legal-hold is a security-relevant action that must be visible on the chain.

## Azure-seam note

The Azure production target inherits this model verbatim. The `DocumentStore` port's Azure Blob adapter (`app/infrastructure/storage/azure_blob_document_store.py`, future) reads the same `content_ref` value the bundle's use case passes; the bundle's wire shape does not change. The audit event's `AuditExported` row is the same append-only chain anchor whether the document store is local-FS or Azure Blob. The `manifest_hash` is the same SHA-256 over the canonical JSON `manifest.json`; an Azure Blob-stored document and a local-FS-stored document produce the same bundle shape.

A future Azure-specific decision (e.g. shipping the bundle directly to an Azure Blob legal-hold container) is a separate ADR. The current scope is the local-first slice; the bundle's wire contract is the boundary the Azure target will preserve.
