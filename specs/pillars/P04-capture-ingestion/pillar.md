# Pillar P04 — Capture & Ingestion

**Status**: Draft · **Owner**: Platform team · **Updated**: 2026-08-31

> Inbound material (upload, scan, email, API, import) becomes a case, a document, or a participant, with provenance.

## 1. Pillar statement

This pillar owns the **inbound channels** by which material enters the platform. Capture is the act of converting an inbound signal into a first-class domain object with provenance — who/what sent it, when, through which channel, and what validation was applied.

The pillar is the seam where external systems meet the platform. Everything that flows in through a channel must:

- be authenticated and authorized at the channel boundary,
- carry provenance metadata,
- land in the correct pillar (P01 for case creation, P03 for document upload, P14 for bulk import),
- be auditable as a `Captured` event,
- be replayable when safe (idempotent by source identifier),
- fail safely (no partial ingestion, no silent loss).

## 2. Scope

### In scope
- Channel interfaces: file upload, API, import package, (later) email, scan.
- Provenance metadata: source system, source identifier, channel, capture timestamp, uploader, source payload hash.
- Idempotency keys for capture requests.
- Validation at the channel boundary (schema, file type, size, scope).
- Quarantine state for malformed or suspicious captures.
- Connectors behind the `MessageBus` and `DocumentStore` ports.

### Out of scope (deferred)
- Real-time document scanning (virus, OCR, PII). The seam is here; the implementations are P14 integrations.
- Inbound email (POP3, IMAP, Graph API). First slice: outbox-driven outbound + manual upload.
- Inbound fax. P3.
- Drag-and-drop multi-file upload UI. P3.

### Explicitly not building
- A "universal connector" that auto-detects formats. Each channel has a defined schema and validation.
- A "smart inbox" that uses AI to triage. The pillar is deterministic.

## 3. Primary entities

| Entity | Purpose | First slice |
|---|---|---|
| `CaptureChannel` (port) | A defined inbound channel. | Yes |
| `CaptureRequest` | A unit of inbound material. | Yes |
| `CaptureResult` | Outcome: created case / created document / quarantined / rejected. | Yes |
| `Provenance` | Source system, source id, channel, timestamps, hash. | Yes |
| `QuarantineRecord` | A captured item held for operator review. | Deferred |

## 4. User stories (priority-tiered)

### P1 — Critical
- **P04-US1**: As an authorized API client, I can submit a case-creation request with an idempotency key. Duplicate submissions do not create duplicate cases.
- **P04-US2**: As an authorized user, I can upload a document to a case via the API; the upload is validated, the content is stored, and the document is associated with the case.

### P2 — Important
- **P04-US3**: As an operator, I can submit a validated import package from an approved source, see dry-run results before finalizing, and reconcile outcomes against the source.
- **P04-US4**: As a process administrator, I can configure which channels are active for a tenant and what validation applies.
- **P04-US5**: As a system, when a captured item fails validation, the item is held in quarantine with a reason and the operator can release or reject it.

### P3 — Deferred
- **P04-US6**: As an operator, I can configure an inbound email channel; messages become documents or case notes with provenance.
- **P04-US7**: As an operator, I can configure a scan-channel; a document is held in quarantine until scan results are available.
- **P04-US8**: As an operator, I can configure a webhook channel; external systems can push events that become case state changes.

## 5. Cross-cuts

- **P01 (Case)**: a capture may result in case creation.
- **P03 (Document)**: a capture may result in a document.
- **P09 (Audit)**: every capture is an audit event.
- **P11 (Security)**: every channel is authenticated and authorized; the `IdentityProvider` port is used.
- **P13 (Events)**: captures may emit domain events via the outbox.
- **P14 (Integrations & Migration)**: the actual channel adapters (email, scan, OnBase) live in P14; this pillar is the contract.

## 6. Acceptance scenarios (representative)

- D1: An API client submits a case creation with `Idempotency-Key: abc123` and request body X. The same client retries with the same key and body. The case is created once; the second call returns the prior result.
- D2: An API client submits the same key with a different body. The system rejects the second call with `conflict` (idempotency violation).
- D3: An import package is submitted in dry-run mode. The system reports expected creates, updates, rejects, duplicates, and missing references without writing any case data.
- D4: A captured item fails validation. The system creates a quarantine record with reason and metadata; the case is not falsely reported as complete; an operator can find and inspect the record.

## 7. Planned slices

| Slice | Status | Notes |
|---|---|---|
| `001-case-management-platform` | Active | First slice. Implements P04-US1, US2. Import pipeline is a stub. |
| Future slice 2 | Planned | Import pipeline (US3), quarantine (US5). |
| Future slice 3 | Planned | Email / scan / webhook channels. |

## 8. Open questions

- Q1: How long are quarantined items retained before automatic deletion? P08 will own the retention policy; P04 stores the seam.
- Q2: For inbound channels, is "release from quarantine" an admin action or a workflow action? (Current intent: admin action with audit; workflow integration later.)
- Q3: What is the maximum import package size? Affects the storage adapter's streaming support.
