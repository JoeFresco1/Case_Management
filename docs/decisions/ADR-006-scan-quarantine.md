# ADR-006 — Scan Capture & Safety Quarantine (P04-US7 / FM-029, **Gated**)

**Status:** Accepted
**Date:** 2026-09-02
**Owner:** Platform team
**Related Pillars:** P04 Capture & Ingestion, P01 Case & Business Object Management, P03 Document Management, P09 Audit & Evidence, P11 Security & Identity, P14 Integrations & Migration
**Pillar Question:** `specs/pillars/P04-capture-ingestion/pillar.md:63` US7
**Tasks/Features Affected:** FM-029 (`specs/pillars/01-deduplicated-feature-map.md:78`), `backend/app/application/capture/scan_capture.py:1`, `backend/app/interfaces/http/routers/capture.py` (`POST /api/v1/capture/scan`), `backend/app/interfaces/http/schemas/capture.py` (`CaptureScanRequest`/`CaptureScanResponse`), `backend/app/domain/channel.py` (`ChannelKind.SCAN`), `backend/app/config.py` (`scan_capture_enabled`), `backend/tests/capture/test_scan_capture.py:1`. AGENTS.md §3 (Issue #3 — no live Azure) + §4.R11 + §12 + §18.

## Context

Pillar P04 US7 (`specs/pillars/P04-capture-ingestion/pillar.md:63`) and FM-029 (`specs/pillars/01-deduplicated-feature-map.md:78`) call for a scan channel where scanned material enters quarantine, retains device/source provenance, and is released only after the configured scan result. FM-029 is marked **Gated** in the deduplicated feature map because implementing a real virus / OCR / PII scanner is a deliberate operational decision; the platform must not silently enable a scan channel on a fresh deployment.

The seam mirrors the inbound email channel (FM-028) but introduces three new constraints that require explicit choices:

1. **Gated feature toggle** — the operator must opt in before the platform accepts scan bytes. The seam is `app.application.capture.scan_capture.ScanAuthorizationProvider`; the local adapter reads `Settings.scan_capture_enabled` (default `False`). A misconfigured deployment surfaces as `Forbidden`, never as a silent acceptance.
2. **Quarantine-before-document ordering** — the platform must hold the quarantine record *before* producing a document so a viewer never sees a scanned page before the verdict. The clean path transitions the record to `RELEASED` in the same transaction; the infected path leaves it `HELD` with `error_code="scan_infected"` and writes no document.
3. **Device + source provenance** — every scan must carry a `device_id` (the scanning device) and a `source_label` (the operator UI's bucket) on the quarantine record and the audit payload. The two are the operator's primary diagnostic when the scan verdict is infected.

## Decision

The first slice implements scan capture as `POST /api/v1/capture/scan` with the following contract:

1. **Authorization is two-gated.** The use case first consults `ScanAuthorizationProvider.is_enabled()`. If disabled, the call raises `Forbidden` before any DB write — no quarantine, no audit, no outbox, no storage. If enabled, the actor is then authorized against `ADMIN_TENANT_CONFIG` (same gate as `/api/v1/admin/channels`) so non-admin actors are refused even on an enabled deployment.
2. **Channel contract.** The channel must be of kind `ChannelKind.SCAN` (new kind added to `app/domain/channel.py:_SUPPORTED_CHANNEL_KINDS`) and `enabled=True`. A disabled / wrong-kind channel raises `ValidationFailed` (mirrors `email_capture`).
3. **Hash + store before quarantine.** The bytes are hashed + stored through the existing `DocumentStore` port so the quarantine record's `context.sha256` and the (clean) `Document.sha256` agree. A `DependencyUnavailable` propagates; nothing is committed.
4. **Quarantine always written.** The `QuarantineRecord` is the durable audit-receipt for every scan, clean or infected. Clean scans release the record (status → `RELEASED`) and additionally write `Document` + `DocumentVersion` + `ScanReleased` audit + outbox in the same transaction. Infected / inconclusive scans keep the record `HELD` and write only `ScanCaptured` audit + outbox.
5. **Error codes.** `scan_infected`, `scan_inconclusive`, and `clean_scan` (the release tag) are the stable `error_code` values on the quarantine row. Operators bucket on them; the detail field carries the free-text finding (virus name, OCR confidence) without payload data.
6. **Multi-tenant posture.** Every method takes `tenant_id` from `UserContext`; `ChannelRepository.get` and `CaseRepository.get` return `None` for cross-tenant ids so the API never confirms cross-tenant existence (Constitution V).
7. **Synthetic posture.** All fixture bytes use synthetic payloads (e.g. `%PDF-1.4\n% synthetic scan bytes\n`); the `SYNTHETIC_ENVIRONMENT` marker from `main.py:95` is unaffected.

## Scan Verdict Lifecycle

| Verdict | Quarantine status | Document written? | Audit events | Outbox events |
|---|---|---|---|---|
| `clean` | `HELD` then `RELEASED` (same txn) | Yes — `Document` + initial `DocumentVersion` on the supplied case | `ScanCaptured`, `ScanReleased` | `ScanCaptured`, `ScanReleased` |
| `infected` | `HELD` (terminal for this capture) | No | `ScanCaptured` (`error_code="scan_infected"`) | `ScanCaptured` |
| `inconclusive` | `HELD` | No | `ScanCaptured` (`error_code="scan_inconclusive"`) | `ScanCaptured` |

The operator resolves a held record via the existing `/api/v1/admin/quarantine/{id}/release|reject` endpoints (FM-027). The scan-capture use case never releases an infected record itself — that is the operator's decision.

## Schema Implications

- New `ChannelKind.SCAN` value added to `app/domain/channel.py:_SUPPORTED_CHANNEL_KINDS`. No migration is required (the `channels.kind` column is a portable `String` / `Text` column, not an enum at the DB level — `AGENTS.md §4.R1` + §16.1).
- `Settings.scan_capture_enabled: bool = False` added to `app/config.py`. Default `False` — the operator must set `SCAN_CAPTURE_ENABLED=1` to enable. The flag is read at request time by the router's `_SettingsScanAuthorization` adapter; a future Azure adapter may consult Azure App Configuration.
- No new SQL tables. Scan capture reuses `quarantine_records`, `documents`, `document_versions`, `audit_events`, and `outbox`. The P14 connector (future) supplies the scan verdict — there is no separate scan_results table in this slice.

## Authorization Implications

- New seam: `ScanAuthorizationProvider` (Protocol) in `app/application/capture/scan_capture.py`. The application layer depends on the Protocol only; the adapter is wired in the router (`_SettingsScanAuthorization`) so the use case stays free of `app.config` and FastAPI.
- New flag: `Settings.scan_capture_enabled` (default `False`). The use case refuses with `Forbidden` when disabled.
- No new `Action`. The actor still requires `ADMIN_TENANT_CONFIG` (FM-002 / P11-US2). `intake` / `reviewer` / `supervisor` are denied; `process_admin` is granted through the existing role matrix.

## Failure Behavior

- **Gated off**: `Forbidden` before any DB write — no quarantine, audit, outbox, document, or storage.
- **Auth / validation / not-found errors**: raised before any DB write.
- **Storage `DependencyUnavailable`**: propagates; caller's transaction rollback keeps partial state out of the database.
- **Disabled / wrong-kind channel**: `ValidationFailed` (422).
- **`Idempotency-Key` header missing**: 400 by the middleware; required because scan capture is a mutation.
- **Cross-tenant channel or case**: `NotFound` (no leak, Constitution V).
- **Empty bytes, missing `device_id` or `source_label`**: `ValidationFailed` (422) before storage.
- **Infected scan**: quarantine held; no document; the audit timeline shows the `scan_infected` block and the operator resolves via `/admin/quarantine/{id}/release|reject`.

## Azure Seam

The `scan_result` field on the command is the seam to the P14 scan connector (AGENTS.md §12). The connector (a future vendor / in-house adapter — ClamAV, Defender for Endpoint, Azure Defender for Storage, OCR service) reports `clean` / `infected` / `inconclusive` plus a free-text `scan_detail`. The platform never scans bytes itself. A future Azure adapter may swap `_SettingsScanAuthorization` for a `FeatureManagement`-backed provider without touching the application layer.

The `DocumentStore` port already has the Azure Blob adapter reserved; the `ScanAuthorizationProvider` Protocol is the new seam this slice adds.

## Migration Strategy

1. Deploy the slice — no Alembic migration is required.
2. Wire the router (already done in this change).
3. Operator opt-in: set `SCAN_CAPTURE_ENABLED=1` and create at least one `Channel` of kind `SCAN` per tenant that wants to receive scans.
4. Synthetic verification: `pytest -q tests/capture/test_scan_capture.py` against sqlite (AGENTS.md §4.R1 — sqlite path never needs Docker).
5. Manual smoke: `curl -H "X-Dev-User: <admin-id>" -H "X-Dev-Tenant: <tenant-id>" -H "Idempotency-Key: <uuid>" -H "Content-Type: application/json" -d '{"channel_id":"...","device_id":"scanner-1","source_label":"counter-1","scan_result":"clean","case_id":"...","content_b64":"..."}' http://localhost:8000/api/v1/capture/scan | jq .`

## Tasks/Features Affected

- `task_c8155d70cc6e` (this slice): application service, router, schemas, settings flag, tests, ADR.
- Future P04-US7 follow-ups: scan-result webhook ingestion (P14 connector), scanner-specific findings (`scan_detail` schema), retention policy for `HELD` quarantine rows (FM-040).
- Future P14 connectors: ClamAV / Defender for Endpoint / OCR service feed the same use case.

## Approval Gate

This ADR is implementation-aligned (no design-review gate required for the first slice). The Gated posture is enforced by `Settings.scan_capture_enabled` defaulting to `False` and the use case reading the seam at the very first step — a misconfigured deployment surfaces immediately as `Forbidden`, never as silent acceptance. Any change that loosens the gate (defaulting the flag to `True`, or removing the seam) requires a superseding ADR with an explicit operational approval (FM-029 is H3 / Gated, AGENTS.md §3 Issue #3).