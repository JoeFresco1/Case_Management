# ADR-048: Review remediation boundaries for local identity and durable mutations

- Status: Accepted (2026-09-26)
- Scope: Local synthetic identity, live role authority, mutation idempotency, audit privacy, and outbound webhook delivery.
- Related: ADR-027 (role authority), ADR-039 (search audit), ADR-013 (outbound webhooks), ADR-030 (queues).

## Context

The platform review found that process-local seed identities were unavailable to the API server, built-in role claims could override live revocation, several mutating routes lacked replay protection, and an empty message bus could acknowledge outbound events without creating deliveries. Audit dedupe also treated an asynchronous SQL lookup as a truthy object, while search audit shapes retained free text.

## Options

1. Keep static role grants, per-process identity registration, and method-only middleware rules. This preserves the defects and makes revocation and retry behavior depend on the caller.
2. Use persisted local identities and role assignments, classify exact query-only and bearer routes, and require keys for every other mutating HTTP method. Register the webhook catalog consumer before outbox dispatch and let enqueue errors propagate. Redact query operands in audit projections while retaining saved-search execution fields separately.

## Decision

- In local mode, the API loads synthetic users from the local database at startup. The dev proxy sends the selected seeded user key (`intake` by default); tenant-scoped aliases and UUIDs resolve to persisted identities. Dev headers remain rejected outside local mode.
- Live role assignments and permission rows decide authorization whenever the role repository is wired. Seeding bootstraps reserved roles and initial assignments, including `process_admin`, but reseeding preserves subsequent revocations and permission edits.
- `POST`, `PATCH`, `PUT`, and `DELETE` mutations require `Idempotency-Key`. Exact read-only POST paths for search and workflow simulation, and bearer share-link redemption, bypass actor-bound idempotency middleware. The browser client supplies keys on protected mutations and preserves caller-supplied keys.
- SQL audit dedupe is awaited before append. Search audit projections redact free text and filter operands; saved-search execution columns remain available to the owner for replay.
- The outbound webhook catalog subscriber is registered before the outbox starts. Enqueue failure aborts the subscriber transaction and reaches the outbox retry path; a durable delivery row is handled by the delivery dispatcher.
- The SQL case repository normalizes case-type slugs to uppercase underscore tokens when rendering a reference. This lets the documented `ServiceRequest` seed and every intake path use the same reference contract without weakening domain validation.

## Consequences and Azure seam

Local startup requires a reachable migrated database when loading seeded identities. The seed supports a one-time bootstrap of users created before live roles existed. The middleware exceptions are exact paths so adding a future mutation requires its normal key. Azure identity, event transport, storage, and webhook adapters remain behind their existing ports; this decision does not enable a live Azure connection.

SQL-backed HTTP tests must seed role and assignment rows for authorized actors. A role name in `UserContext` alone is intentionally insufficient once the live repository is wired.
