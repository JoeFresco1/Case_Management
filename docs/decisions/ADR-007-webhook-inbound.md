# ADR-007 — Inbound Webhook Commands Idempotency and HMAC Model (P04-US8 / FM-030)

**Status:** Accepted (first-slice implementation; ORM model + migration reserved for follow-up)
**Date:** 2026-09-02
**Owner:** Platform team
**Related Pillars:** P04 Capture & Ingestion, P01 Case & Business Object Management, P11 Security & Identity, P13 API/Events/Extensibility, P14 Integrations & Migration
**Pillar Question:** `specs/pillars/P04-capture-ingestion/pillar.md:64` US8
**Tasks/Features Affected:** FM-030 (`task_1db1a5d27c7b`), `backend/app/application/capture/webhook_commands.py:1`, `backend/app/interfaces/http/routers/webhooks.py:1`, `backend/app/domain/webhook_capture.py:1`, `backend/app/domain/ports.py` (`CapturedWebhookRepository`), `backend/app/domain/channel.py` (`ChannelKind.WEBHOOK`), `backend/app/infrastructure/persistence/repositories/captured_webhook_repository.py:1`, `backend/tests/capture/test_webhook_commands.py:1`, AGENTS.md §4.R8 + §12.

## Context

Pillar P04 US8 (`specs/pillars/P04-capture-ingestion/pillar.md:64`) and FM-030 (`specs/pillars/01-deduplicated-feature-map.md:79`) call for an inbound webhook channel whose authorized events become explicit case commands or state changes with idempotency and provenance. The seam is reserved for a P14 connector; the first slice accepts JSON-shaped events over HTTP so the operator UI can replay synthetic producers without a live external system.

Two related surfaces already exist on the platform:

1. **HTTP `Idempotency-Key` middleware** (`app/interfaces/http/idempotency.py:1`, AGENTS.md §10) — replay by SHA-256 of the canonical JSON body. Already applied to every mutating endpoint under `/api/v1`.
2. **Resource-key idempotency** — `captured_emails` is keyed on `(tenant_id, channel_id, message_id)` for FM-028 (ADR-005); case `external_ref` follows the same pattern (ADR-024). The pattern is: a **durable row on the resource's own table** is the dedup ledger, keyed by a stable, resource-level identifier.

Inbound webhooks raise the same question: what is the resource-level key for a *re-delivered* event? Two candidates exist:

- The HTTP `Idempotency-Key` header. Request-scoped; rotates between clients; cannot survive a connector-level retry that uses a fresh HTTP request.
- The producer's `X-Webhook-Id`. Stable across retries; assigned by the external system (e.g. `evt_2026_09_02_0001`); the only id the platform can rely on regardless of transport.

The same answer as FM-028 applies: **content idempotency must live on the resource's own table, keyed by `X-Webhook-Id`**, mirroring `captured_emails`. The HTTP `Idempotency-Key` middleware is the request-level layer; the `captured_webhooks` unique constraint is the resource-level layer. Both compose.

### Inbound (FM-030) vs Outbound (FM-056) webhooks

`01-deduplicated-feature-map.md:189` is explicit: the two webhooks are *not* duplicates. Inbound (this ADR) and outbound (FM-056) webhooks have opposite trust directions and failure semantics:

- The platform is the **consumer** of inbound webhooks and the **producer** of outbound webhooks.
- Authorization is the inverse: inbound must verify the producer's identity; outbound must identify itself to the consumer.
- Idempotency is the inverse: inbound dedupes on the producer's id; outbound dedupes on the platform's own event id.
- Failure semantics are the inverse: inbound must reject unauthorized deliveries at the boundary; outbound must retry authorized deliveries.

Merging them would conflate two unrelated contracts. This ADR concerns only the inbound surface.

## Decision

The first slice implements inbound webhook commands as `POST /api/v1/webhooks/{channel}` with the following contract:

1. **Idempotency anchor**: the producer's `X-Webhook-Id` header (length 1..998, visible ASCII). The application layer refuses empty / whitespace / overlong / non-ASCII values via `is_valid_webhook_id`.
2. **Durable dedup ledger**: a new `captured_webhooks` table keyed by `UNIQUE (tenant_id, channel_id, webhook_id)`. The constraint is the database-level idempotency anchor; the application layer catches `IntegrityError` on insert and returns the prior row via a follow-up `get_by_webhook_id` lookup. The lookup is tenant-scoped (`WHERE tenant_id = :tenant_id`) so a cross-tenant `X-Webhook-Id` is never observable (Constitution V).
3. **HMAC transport trust**: the producer signs the request with `X-Webhook-Signature: sha256=<hex>` over the canonical string `f"{timestamp}.{body}"` using the channel's shared secret. The secret is resolved through the `SecretProvider` port (`AGENTS.md §12`) as `webhook_secret_{channel_key}`. The local adapter reads from `./.secrets.json`; the Azure adapter will read from Key Vault. The use case does NOT see the secret — HMAC verification is the router's responsibility (transport-level trust).
4. **Replay window**: the signature's `X-Webhook-Timestamp` must be within 5 minutes of the local clock. A signature older than the window is refused with `401`. The window is a conservative default; the channel's policy can override in a future slice.
5. **Constant-time signature comparison**: the router uses `hmac.compare_digest` so a probing caller cannot infer the secret byte-by-byte through timing.
6. **Authorization (application-level trust)**: the use case delegates to `Action.CASE_TRANSITION` on the supplied case. The same gate that guards an in-app transition guards a webhook-driven transition; there is no "webhook bypass" path. The source_system claim is **provenance, not authority** — a webhook cannot elevate an actor.
7. **Two idempotency layers compose**: the HTTP `Idempotency-Key` middleware handles client-driven retries; the `captured_webhooks` unique constraint handles server-driven retries (connector redelivery, network re-fetch). A retried POST with the same `Idempotency-Key` AND same `X-Webhook-Id` hits the same captured row.
8. **Command vocabulary is closed**: `case.transition`, `case.comment`, `case.note`. Unknown values are refused with `422` at the router before any DB write. Adding a new command kind requires a superseding ADR — the vocabulary is not extensible at runtime (Constitution XIV — bounded extensibility).
9. **Atomic with audit + outbox**: `CapturedWebhook` + `WebhookCaptured` audit + `CaseTransitioned` audit + `CaseTransitioned` outbox rows commit in the same transaction. A partial commit would leave a command without provenance or a provenance row without a command, which is explicitly forbidden (Constitution V, P09-US1).
10. **Multi-tenant posture**: every repository method takes `tenant_id` first; the SQL implementation will include `WHERE tenant_id = :tenant_id`. The actor's tenant is taken from the authenticated caller (`X-Dev-User` / `X-Dev-Tenant` in `local` posture) — not the request body.

## Idempotency Behavior

- **First submission of a new webhook id**: a fresh `CapturedWebhook` + audit + outbox row are written in one transaction. Returns `201`.
- **Duplicate webhook id from the same tenant**: the use case's pre-check (`CapturedWebhookRepository.get_by_webhook_id`) finds the prior row and returns it. No audit / outbox row is written. Returns `200` and `replayed: true`. The HTTP `Idempotency-Key` middleware sees the second request and replays the same body so a client that uses both layers sees a stable response.
- **Concurrent submissions of the same webhook id**: the unique constraint fires on the loser's `INSERT`; the loser's `IntegrityError` is translated to `Conflict` and the use case re-reads via `get_by_webhook_id`. Net result: one captured row, one applied command, two HTTP `200` responses.
- **Cross-tenant webhook id**: the `WHERE tenant_id = :tenant_id` filter on `get_by_webhook_id` returns `None` so a foreign tenant's webhook id is never observable. Tenant isolation is mandatory (AGENTS.md §3 R1).

## HMAC Failure Behavior

- **Missing `X-Webhook-Signature` / `X-Webhook-Timestamp`**: `401 missing_webhook_signature`. The platform never confirms the channel's existence to a non-authenticated caller.
- **Stale timestamp** (outside the 5-minute window): `401 webhook_timestamp_out_of_window`. Captured signatures cannot be replayed indefinitely.
- **Bad signature**: `401 invalid_webhook_signature`. The same code is returned for "unknown channel secret" so a probing caller cannot enumerate channel keys.
- **All 401 responses carry a coarse error code** — the platform never reveals which check failed beyond the code.

## Schema Implications

- New `captured_webhooks` table (reserved for follow-up slice; not part of `task_1db1a5d27c7b`). All column types are SQLAlchemy generic types (Uuid, JSON, DateTime(timezone=True), String, Text) — no `JSONB`, no `gen_random_uuid()`, portable across SQLite + PostgreSQL + Azure SQL (`AGENTS.md §3 R1`).
- New `UniqueConstraint` on `(tenant_id, channel_id, webhook_id)` named `uq_captured_webhooks_tenant_channel_webhook`. Migration is reversible; `downgrade` drops the indexes then the table.
- New model `CapturedWebhook` in `app/infrastructure/persistence/models.py` and a new port `CapturedWebhookRepository` in `app/domain/ports.py` (mirrors the `CapturedEmailRepository` / `ChannelRepository` split).
- New SQL adapter `SqlCapturedWebhookRepository` in `backend/app/infrastructure/persistence/repositories/captured_webhook_repository.py:1`. The adapter's `add` and `get_by_webhook_id` currently raise `NotImplementedError` / `NotFound` so a misconfigured deployment fails closed at the first request rather than silently no-op'ing a write. The router is **not mounted in `app/main.py`** in this slice, so no live traffic hits this adapter.
- The router accepts JSON-shaped events over HTTP today; the production connector (P14) will feed the same use case directly. The router is the **operator-UI / synthetic-producer** surface; the connector is the production path.

## Authorization Implications

- No new `Action` is required. The use case delegates to `Action.CASE_TRANSITION` on the supplied case's tenant. `intake` is denied; `reviewer` / `supervisor` / `process_admin` are granted through the existing role matrix.
- The actor cannot impersonate a system actor: the use case builds a `UserContext` proxy with `is_system=False` explicitly. A webhook is never system authority; an integration that needs bypass must be wired through the role-assignment table (P11-US4), not the channel configuration.
- `SYSTEM_BYPASS` is unchanged; no webhook action has been added.

## Azure Seam

- The `SecretProvider` port already has an Azure Key Vault adapter reserved (`AGENTS.md §12`). The production HMAC adapter resolves `webhook_secret_{channel_key}` from Key Vault; the local adapter reads from `./.secrets.json`. The use case is unchanged.
- The P14 connector for inbound webhooks (FM-030 production) feeds the same use case directly: when the connector surfaces a verified event, it calls `apply_webhook_command`. The HTTP `/api/v1/webhooks/{channel}` endpoint is for operator-UI replay / synthetic producers only.

## Migration Strategy

1. **First slice (`task_1db1a5d27c7b` — this ADR)**: use case, port, domain aggregate, router, test, ADR. ORM model, migration, and unique constraint are **reserved for follow-up**. The router is not mounted in `app/main.py`, so no live traffic hits the unimplemented SQL adapter.
2. **Follow-up slice**: add the `captured_webhooks` table + migration + unique constraint. Implement `SqlCapturedWebhookRepository.add` and `get_by_webhook_id`. Mount the router in `app/main.py`. Update the synthetic seed to publish a sample webhook channel and store its secret in `./.secrets.json`.
3. **P14 production slice**: replace the HTTP router with a connector that reads from the source system (Graph / Stripe / GitHub / etc.) and feeds `apply_webhook_command` directly. The HMAC verification moves to the connector; the router remains for operator-UI replay.

## Tasks/Features Affected

- `task_1db1a5d27c7b` (this slice): application service, router, schemas, domain aggregate, port, stub SQL adapter, test, ADR. No main.py mount, no migration.
- Future P04-US8 follow-ups: ORM model + migration + unique constraint, main.py mount, production connector, retention policy, fan-out to multiple commands per webhook.
- Future P14 connectors: Graph, Stripe, GitHub, etc. — feed the same use case directly.

## Approval Gate

This ADR is implementation-aligned (no design-review gate required for the first slice). Any future change in idempotency model (e.g. switching to `(tenant_id, source_system, webhook_id)`, or supporting multiple commands per webhook) requires a superseding ADR.

## Addendum — Run D follow-up landed (2026-09-16)

The "reserved for follow-up" items in §Schema Implications / §Migration Strategy
have landed: `captured_webhooks` ORM row + `UNIQUE (tenant_id, channel_id,
webhook_id)` + `0030_capture_quarantine_webhook` migration (reversible,
round-trip verified), implemented `SqlCapturedWebhookRepository`, and the
`POST /api/v1/webhooks/{channel}` router mounted in `app/main.py`. The
fail-closed posture is unchanged (HMAC via `SecretProvider` + `CASE_TRANSITION`
gate); FM-030 remains Gated — see `specs/002-leftovers/architecture-runD.md §5`.
