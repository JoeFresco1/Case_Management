# ADR-013 — Outbound Webhook Subscriptions and Retry-Safe Delivery (P13-US10 / FM-056)

**Status:** Accepted (first-slice implementation; delivery dispatcher loop wired at startup in a follow-up)
**Date:** 2026-09-02
**Owner:** Platform team
**Related Pillars:** P13 API/Events/Extensibility, P09 Audit & Provenance, P11 Security & Identity, P14 Integrations & Migration
**Pillar Question:** `specs/pillars/P13-api-events-extensibility/pillar.md:77` US10
**Tasks/Features Affected:** FM-056 (`task_...`), `backend/app/application/webhooks/outbound.py`, `backend/app/interfaces/http/routers/webhooks.py:...` (outbound half), `backend/app/domain/outbound_webhook.py`, `backend/app/domain/ports.py` (`WebhookSubscriptionRepository`, `OutboundDeliveryRepository`, `OutboundDeliveryTransport`), `backend/app/infrastructure/persistence/models.py` (`WebhookSubscription`, `OutboundDelivery`), `backend/app/infrastructure/persistence/repositories/outbound_webhook_repository.py`, `backend/app/infrastructure/webhooks/httpx_transport.py`, `backend/alembic/versions/0011_create_outbound_webhooks.py`, AGENTS.md §4.R8 + §12.

## Context

Pillar P13 US10 (`specs/pillars/P13-api-events-extensibility/pillar.md:77`) and FM-056 (`specs/pillars/01-deduplicated-feature-map.md:120`) call for authenticated, retry-safe outbound webhook deliveries. The seam is the production-side counterpart of the inbound capture path (FM-030 / ADR-007): the platform emits domain events to authorized subscribers as they are dispatched from the outbox, with HMAC authentication and bounded retry.

The first slice already publishes events to the local in-process bus (`app.infrastructure.messaging.local_message_bus.py`) and the outbox dispatcher drains `outbox` rows on a tick (`backend/app/infrastructure/messaging/outbox_dispatcher.py`). The bus has subscriber callbacks but no durable per-subscriber delivery ledger and no operator-facing subscription lifecycle. The gap is exactly what FM-056 fills: a tenant-scoped subscription registry, a durable per-delivery ledger with bounded retry, and a thin HTTP transport that signs each request.

### Why outbound is separate from inbound (FM-030 / ADR-007)

`01-deduplicated-feature-map.md:189` is explicit: the two webhooks are *not* duplicates. Inbound and outbound have opposite trust directions and failure semantics (ADR-007 §"Inbound vs Outbound"):

- The platform is the **consumer** of inbound webhooks and the **producer** of outbound webhooks.
- Trust direction is the inverse. Inbound verifies the producer's HMAC; outbound **signs** with the platform's secret so the subscriber can verify the platform's identity.
- Idempotency is the inverse. Inbound dedupes on the producer's `X-Webhook-Id`; outbound dedupes on the platform's own `event_id` (the platform's id is what makes a delivery retry-safe — a subscriber seeing the same `event_id` knows it is a replay, not a new event).
- Failure semantics are the inverse. Inbound **rejects** unauthorized deliveries at the boundary; outbound **retries** authorized deliveries until they succeed or exhaust the cap.

Merging them would conflate two unrelated contracts. This ADR concerns only the outbound surface; it explicitly references ADR-007 for the inbound side and uses the same canonical signing string `f"{timestamp}.{body}"` so a subscriber that already implements the inbound verify path can reuse the same code.

### The signing problem

The two reasonable authentication schemes for outbound are HMAC and JWT bearer. HMAC is the convention (GitHub, Stripe, etc.); JWT is the right answer for high-trust consumers that want a token they can introspect. The decision is to support **both**, and to send the HMAC *in addition to* the JWT when a subscriber opts in to `require_bearer=True` — the HMAC still verifies the body, the JWT carries the proof-of-delivery claims (`sub_id`, `dlv_id`, `evt_id`, `exp`).

### The idempotency problem

A retry is, by definition, a duplicate delivery. The subscriber needs to know "this is a retry of a prior delivery" without parsing the body. Two complementary mechanisms:

- **Wire-level** — every delivery carries `X-Webhook-Event-Id: <uuid>`. The subscriber stores recently-seen ids; a duplicate is dropped. The id is the platform's own `event_id` from the outbox envelope (FM-055).
- **Database-level** — `UNIQUE (tenant_id, subscription_id, event_id)` on `outbound_deliveries` ensures the platform itself does not enqueue twice. The application layer catches the `IntegrityError` and returns the prior row.

Both layers compose: a replay that re-issues the same event id from the outbox hits the same delivery row, and the same `X-Webhook-Event-Id` reaches the subscriber.

### The retry problem

Bounded retry is non-negotiable: a subscriber that hangs would otherwise pin the dispatcher loop indefinitely. The decision mirrors the outbox dispatcher (`outbox_dispatcher.py:51`): `MAX_ATTEMPTS = 5` with a `DEAD_LETTERED` terminal state. The operator console surfaces dead-lettered rows for triage; the replay endpoint (`POST /api/v1/webhooks/subscriptions/{id}/replay`) resets the attempt counter.

Permanent 4xx failures (other than 408/429) are **not** retried: a 400/401/403/404/422 will not get better on retry. The dispatcher dead-letters on the first permanent failure to avoid burning the retry budget.

## Decision

The first slice implements outbound webhook delivery as `POST /api/v1/webhooks/subscriptions` (CRUD) + a per-delivery loop driven by the existing outbox dispatcher with the following contract:

1. **Subscription aggregate** (`WebhookSubscription`) — tenant-scoped, optimistic-concurrency via `version_token`, lifecycle `ACTIVE` → `DISABLED` → `RETIRED`. The signing secret is a logical name resolved through the `SecretProvider` port (AGENTS.md §12); no secret material lives on the row.

2. **Delivery aggregate** (`OutboundDelivery`) — one row per `(subscription, event)` pair, durably deduped by `UNIQUE (tenant_id, subscription_id, event_id)`. The row is the retry anchor: state machine `PENDING` → `SUCCEEDED` or `PENDING` → `DEAD_LETTERED`.

3. **Catalog subscriber** (`make_catalog_subscriber`) — a `MessageBus` subscriber that mirrors domain events to outbound subscribers. For each dispatched event, it looks up subscriptions whose filter set includes the event_type and enqueues a delivery row per match. The enqueue is idempotent; a replay hits the same row.

4. **HMAC-SHA256 signing** — the canonical string is `f"{timestamp}.{body}"` (same as the inbound surface, ADR-007). The signature header is `X-Webhook-Signature: sha256=<hex>`. The timestamp header is `X-Webhook-Timestamp: <unix-epoch>`.

5. **Optional JWT bearer** — when `require_bearer=True`, the platform attaches `Authorization: Bearer <jwt>` with HS256 claims `sub`, `sub_id`, `dlv_id`, `evt_id`, `iat`, `exp`, `jti`. The JWT TTL is 5 minutes so a captured bearer cannot be replayed indefinitely. The HMAC is still computed and sent.

6. **Two idempotency layers compose**:
   - `UNIQUE (tenant_id, subscription_id, event_id)` on `outbound_deliveries` is the resource-level dedup.
   - `X-Webhook-Event-Id` header is the wire-level dedup anchor for the subscriber.
   - HTTP `Idempotency-Key` middleware (AGENTS.md §10) handles operator-driven client retries on the subscription CRUD endpoints.

7. **Bounded retry** — `MAX_ATTEMPTS = 5` with a per-attempt wall-clock timeout of 10s. Permanent 4xx (except 408/429) is dead-lettered on the first occurrence. The dispatcher loop is the existing `OutboxDispatcher`; a follow-up wires the `DeliveryService` to the same tick.

8. **Transport seam** — `OutboundDeliveryTransport` Protocol in `app.domain.ports`. Local adapter: `HttpXOutboundTransport` in `app.infrastructure.webhooks.httpx_transport` (thin `httpx.AsyncClient` wrapper). Azure seam: placeholder under `app.infrastructure.webhooks.azure_event_grid_transport/` (a `README.md` documenting the env vars and the implementation contract). The application layer never imports `httpx`.

9. **Authorization** — the subscription CRUD endpoints require an operator role (`Action.WEBHOOK_SUBSCRIPTION_MANAGE`). The policy is the same `policy.evaluate` gate every other configuration endpoint uses (P11-US1, deny-by-default). The actor's tenant is the authorization scope; a cross-tenant subscription id is never observable (Constitution V).

10. **Multi-tenant posture** — every repository method takes `tenant_id` first; every SQL query includes `WHERE tenant_id = :tenant_id`. The `outbox` row is the durable source of events; the `outbound_deliveries` row is the durable record of "we tried to deliver this event to this subscription".

11. **No synthetic data leak** — `webhook_subscriptions.url` and `outbound_deliveries.payload_json` carry customer / partner data and must NEVER be populated with synthetic fixtures in the test profile (AGENTS.md §18). Tests use `example.test` URLs and obvious synthetic ids; the seed command does not create subscriptions.

12. **Audit + outbox atomicity** — `register_subscription` writes the subscription row and a `WebhookSubscriptionRegistered` audit row in the same transaction. The outbox row for the domain event is written by the originating use case; the catalog subscriber enqueues a delivery per matching subscription, not a new outbox row.

## Idempotency Behavior

- **First enqueue of a (subscription, event) pair**: a fresh `OutboundDelivery` row is inserted. Returns the row with `created=True`. The dispatcher attempts delivery on the next tick.
- **Duplicate enqueue from the same bus replay**: the unique constraint fires on the loser's `INSERT`; the application layer catches the `IntegrityError` and returns the prior row with `created=False`. The dispatcher continues to retry the prior row's pending attempts.
- **Wire-level duplicate delivery** (subscriber receives the same `X-Webhook-Event-Id` twice): the subscriber is responsible for dedup. The platform does not add a wire-level guard because the in-flight retry loop is the source of duplicates; a subscriber that crashes mid-retry will see the same `event_id` again.
- **Cross-tenant subscription id**: the `WHERE tenant_id = :tenant_id` filter on every `WebhookSubscriptionRepository` method returns `None` so a foreign tenant's subscription is never observable. Tenant isolation is mandatory (AGENTS.md §3 R1).

## HMAC Failure Behavior

- **Missing secret for a subscription** (e.g. operator registered the subscription but did not provision the secret in the `SecretProvider`): the row is dead-lettered on the first attempt with `last_error = "missing_secret:<name>"`. The operator console surfaces the error so the operator can provision the secret and replay.
- **Missing subscription** (the subscription was deleted between enqueue and dispatch): the row is dead-lettered with `last_error = "subscription_not_found"`. The operator can replay only after re-registering the subscription; until then the prior row stays dead-lettered.
- **Subscriber returns 4xx (permanent)**: dead-lettered on the first attempt. The operator can replay after fixing the subscriber; the row's `last_error` carries the truncated response.
- **Subscriber returns 5xx / 408 / 429 / network error**: the attempt counter is bumped; the next tick retries. After `MAX_ATTEMPTS` the row is dead-lettered.
- **Subscriber returns 2xx**: marked `SUCCEEDED`; `succeeded_at` is stamped.

## Replay Behavior

- **Operator-triggered replay** (`POST /api/v1/webhooks/subscriptions/{id}/replay`): resets all `DEAD_LETTERED` rows for the subscription to `PENDING` and zeroes `attempt_count`. The next dispatcher tick re-attempts. The endpoint returns `202 Accepted` so a UI can refresh its detail panel from the response.
- **Row-level replay** (deferred): the `OutboxDispatcher.replay` pattern is mirrored at `OutboundDeliveryRepository` (follow-up). The first slice ships the subscription-level replay; row-level replay is added when the operator console needs it.

## Wire Format

```http
POST /api/v1/webhooks/subscriptions HTTP/1.1
Content-Type: application/json
Idempotency-Key: <uuid>

{
  "url": "https://example.test/webhooks/case-events",
  "event_filters": ["CaseCreated", "CaseClosed"],
  "secret_name": "outbound_subscriber_acme",
  "signing_algorithm": "hmac_sha256",
  "require_bearer": false,
  "description": "Acme BI ingestion"
}
```

→ `201 Created` with the subscription wire shape (id, tenant_id, url, event_filters, secret_name, signing_algorithm, require_bearer, status, version_token, description, created_at, updated_at).

```http
POST <subscriber-url> HTTP/1.1
Content-Type: application/json
X-Webhook-Signature: sha256=<hex>
X-Webhook-Timestamp: 1725260000
X-Webhook-Algorithm: hmac_sha256
X-Webhook-Id: <uuid>
X-Webhook-Event-Id: <uuid>
X-Webhook-Event-Type: CaseCreated
X-Webhook-Delivery-Id: <uuid>
X-Correlation-Id: <correlation-id>

{
  "event_id": "<uuid>",
  "event_type": "CaseCreated",
  "version": 1,
  "tenant_id": "<uuid>",
  "payload": { "case_id": "<uuid>", "reference": "C-...-2026-000001", ... },
  "correlation_id": "<correlation-id>",
  "occurred_at": "2026-09-02T08:43:15.123456+00:00"
}
```

→ `2xx` is success; any other status is a failure (see HMAC Failure Behavior).

## Alternatives Considered

- **HTTP retry with exponential backoff in the transport**: rejected. The transport's job is to deliver one request and return the result; the application layer owns the retry policy so it can update the durable ledger between attempts. Mixing them would split the durable state across two places.
- **Reuse the outbox table for delivery rows**: rejected. The outbox is the source of truth for "events that happened"; the delivery ledger is the source of truth for "we tried to deliver this event to this subscription." Conflating them would couple event replay to delivery replay, which have different lifecycles.
- **Use Azure Service Bus topics for outbound**: rejected for the first slice. Service Bus is the production seam for inbound events; outbound HTTP is the customer's contract. A future slice can add a Service Bus publisher that consumes from the same outbox; the durable `outbound_deliveries` ledger remains the source of truth.
- **Single signing scheme (HMAC only)**: rejected. JWT bearer is the right answer for high-trust consumers (intra-tenant microservices, mTLS-paired services) that want to introspect claims rather than verify a signature. The two are cheap to support; refusing JWT would force the platform to add a separate auth code path for those consumers.
- **Auto-replay dead-lettered rows**: rejected. Dead-lettering is a contract with the operator: "we gave up, your turn." An auto-replay loop would mask configuration errors (wrong URL, revoked secret) by silently retrying forever.

## Consequences

- Two new ORM tables (`webhook_subscriptions`, `outbound_deliveries`) with a migration (`0011_create_outbound_webhooks`). The unique constraint on the delivery table is the content-idempotency anchor.
- A new `MessageBus` subscriber is registered at startup to mirror events to outbound subscriptions. The subscriber runs inline in the local profile (a slow subscriber cannot block the bus for longer than `ATTEMPT_TIMEOUT_SECONDS`).
- The HTTP transport seam (`OutboundDeliveryTransport` Protocol) keeps the test suite substitutable. The local adapter is a thin `httpx.AsyncClient` wrapper; the Azure adapter is a placeholder.
- The operator console gains a new surface: subscription CRUD, dead-lettered delivery list, subscription-level replay. The replay endpoint returns the subscription so a UI can refresh its detail panel from the response.
- Inbound and outbound are now symmetric in shape (both have a CRUD + a delivery endpoint) but the trust / idempotency / failure semantics are inverted. The contrast is documented in `01-deduplicated-feature-map.md:189`, ADR-007, and the module docstrings of `app.domain.webhook_capture` and `app.domain.outbound_webhook`.

## Azure Seam

- The HTTP transport is the swap point. `app.infrastructure.webhooks.azure_event_grid_transport/` is reserved for a future Event Grid adapter; the application layer never imports `httpx` so the swap is a wiring change in `app.main` only.
- The secret store is the swap point for the subscriber's signing key. The local `SecretProvider` reads from `./.secrets.json`; the Azure Key Vault adapter is reserved for production.
- The `webhook_subscriptions` and `outbound_deliveries` tables are portable SQL; the same DDL works on Azure SQL.
- The `OutboxDispatcher` becomes a hosted worker (Azure Functions / Container Apps) consuming the same `outbox` table in production; the catalog subscriber pattern is identical.

## Addendum 2026-09-22 — Delivery hardening (retry/backoff, domain-owned cap, transport-fault tolerance)

**Context.** The first-slice delivery loop retried on every tick with no
delay, `OutboundDelivery.record_attempt` left the dead-letter transition
to its callers (a caller that forgot the cap check retried forever), and
a transport that raised instead of returning a result killed the whole
tenant drain. The G-2 outbox hardening had already established the retry
posture (exponential backoff 5s/10s/20s/40s, cap 300s); webhooks had no
equivalent.

**Decision.** Two source files extended (plus tests), no schema change:

1. `app/domain/outbound_webhook.py` — new pure helpers
   `backoff_delay_seconds` / `delivery_due` on the G-2 schedule (naive
   SQLite reads normalized to UTC); `record_attempt` takes
   `max_attempts` and owns the `PENDING → DEAD_LETTERED` transition;
   new `record_permanent_failure` for poison (4xx), missing-secret, and
   missing-subscription rows; hand-rolled `_replace` helpers replaced
   with `dataclasses.replace` so future fields survive transitions.
2. `app/application/webhooks/outbound.py` — `dispatch_pending` skips
   not-due rows without POSTing or touching the counter (manual replay
   via `attempt` bypasses the gate by design); `transport.post` is
   wrapped so a raising adapter is recorded as a retriable
   `status_code=None` failure instead of killing the drain.

**Consequences.** One existing dispatcher test updated to advance the
clock past the backoff windows (the immediate second drain is now a
skip, not a retry). New suite `tests/webhooks/test_outbound_resilience.py`
pins timeout/retry/poison/retirement, duplicate-POST event-id stability,
fail-closed subscription auth, and absence of secret material in logs,
rows, audit payloads, and wire headers. Network transport stays behind
the `OutboundDeliveryTransport` port; no migration required.
