# Pillar P13 — API, Events & Extensibility

**Status**: Draft · **Owner**: Platform team · **Updated**: 2026-08-31

> Versioned APIs, documented events, extension points for future modules.

## 1. Pillar statement

This pillar owns the **contract surface** of the platform: REST APIs, domain events, integration events, commands, and delivery intents. It also owns the **extension points** by which future modules (and external systems) interact with the platform.

The contract surface is the platform's public face. Contracts are versioned, documented, and stable. Internal database schemas are implementation details and are not contracts (Constitution VII). External systems interact through APIs, commands, events, or integration adapters — never through direct database access.

The pillar distinguishes:

- **Domain events**: facts about changes in the platform (e.g., `CaseCreated`).
- **Integration events**: messages intended for external consumers; derived from domain events but with consumer-friendly shapes.
- **Commands**: requests to perform an action.
- **Delivery intents**: durable requests to send or publish something externally (used for outbound notifications).
- **Audit events**: P09 — related but not the same stream.

## 2. Scope

### In scope
- REST API: resource-oriented, versioned (URL prefix `/api/v1`, future versions are new prefixes).
- OpenAPI schema generated from the FastAPI app; types exported to the frontend via `openapi-typescript`.
- Consistent error model: `code`, `message`, `details`, `correlation_id` (no stack traces, no SQL, no protected data).
- Idempotency keys on POST/PATCH/DELETE.
- Optimistic concurrency tokens (`If-Match: <version_token>`) on updates.
- Pagination on list endpoints (default 50, max 200).
- Domain event catalog: `CaseCreated`, `CaseUpdated`, `CaseSubmitted`, `CaseAssigned`, `CaseStateChanged`, `CasePaused`, `CaseResumed`, `CaseClosed`, `CaseReopened`, `ParticipantAdded`, `DocumentAdded`, `DocumentVersionAdded`, `DocumentScanCompleted`, `TaskCreated`, `TaskClaimed`, `TaskCompleted`, `TaskEscalated`, `DecisionRecorded`, `DecisionApproved`, `SlaWarningRaised`, `SlaBreached`, `ImportStarted`, `ImportCompleted`, `ImportFailed`, `ConfigurationPublished`.
- Outbox pattern: events are written to the outbox in the same transaction as the domain change; a dispatcher delivers them.
- Event versioning: every event has a `version` field; consumers can negotiate on version.
- Extension points: documented seams where future modules can plug in (e.g., `DocumentStore`, `MessageBus`, `IdentityProvider`, `SecretProvider`, `Telemetry`).

### Out of scope (deferred)
- GraphQL. (P3.)
- gRPC. (P3.)
- Webhooks as a public service. (P14; this pillar defines the contract, P14 implements the connector.)
- A public API marketplace. (P3.)
- Real-time event streaming to consumers. (P3; the outbox is the source of truth; consumers poll or subscribe to the bus.)

### Explicitly not building
- A "raw SQL" endpoint for external systems. Internal database schemas are not contracts.
- A "callback URL" without authentication or authorization. Every callback is authenticated and authorized.
- An event that contains unrestricted PHI by default. Events are minimal-payload; sensitive values are referenced by id.
- A "version-less" API. All endpoints are versioned; breaking changes require a new version.

## 3. Primary entities

| Entity | Purpose | First slice |
|---|---|---|
| `ApiError` | The standard error response shape. | Yes |
| `IdempotencyKey` | The header and storage shape. | Deferred (Phase 0 hardening) |
| `DomainEvent` | A base type for facts. | Yes |
| `IntegrationEvent` | A consumer-friendly event for external systems. | Deferred |
| `OutboxRecord` | A row in the outbox table. | Deferred (Phase 0 hardening) |
| `EventCatalog` | The versioned list of event types. | Yes (in code) |
| `ApiVersion` | The version of the API exposed (v1, v2, ...). | Yes |
| `ExtensionPoint` | A documented seam for future modules. | Yes (declared) |

## 4. User stories (priority-tiered)

### P1 — Critical
- **P13-US1**: As an API consumer, I can call `POST /api/v1/cases` with a JSON body and receive a case response; the response includes the case id, reference, status, and version token.
- **P13-US2**: As an API consumer, I can call `GET /api/v1/cases/{id}` and receive the case; the response is consistent with the same fields exposed in the create response.
- **P13-US3**: As an API consumer, when I make a request that fails validation, the response is structured: `code: validation_failed`, `message`, `details: {field: ...}`, `correlation_id`.
- **P13-US4**: As an API consumer, when I make a request that fails authorization, the response is structured: `code: forbidden` or `unauthorized`, with no protected data in the response.
- **P13-US5**: As a frontend developer, I can generate TypeScript types from the OpenAPI schema; the types are committed to the frontend repo and used by the React app.

### P2 — Important
- **P13-US6**: As an API consumer, I can supply an `Idempotency-Key` header on POST; duplicate calls with the same key and body return the prior result; a call with the same key and a different body returns `conflict`.
- **P13-US7**: As an API consumer, I can supply an `If-Match: <version_token>` header on PATCH; a stale token returns `conflict` and the response includes the current version.
- **P13-US8**: As an external system, I can subscribe to the documented event catalog; the events are delivered via the configured message bus (in-process locally, Azure Service Bus in production).
- **P13-US9**: As an operator, I can see the outbox lag (how many events are pending dispatch) and replay eligible failures.

### P3 — Deferred
- **P13-US10**: As an external system, I can subscribe to a webhook and receive events as they are dispatched; the webhook is authenticated and authorized.
- **P13-US11**: As an API consumer, I can use a GraphQL endpoint for ad-hoc queries.
- **P13-US12**: As a developer, I can extend the platform with a module that registers new event types and new policy actions; the registration is itself versioned and audited.

## 5. Cross-cuts

- **P01–P12 (every pillar)**: every pillar exposes its functionality through the API surface owned by P13.
- **P09 (Audit)**: audit events are a related but separate stream.
- **P14 (Integrations)**: external connectors use the API and event surfaces.
- **P11 (Security)**: every API call is authorized.

## 6. Acceptance scenarios (representative)

- M1: An API consumer calls `POST /api/v1/cases` with a valid body. The response is `201 Created` with a case id, reference, and version token.
- M2: An API consumer calls `POST /api/v1/cases` with a missing required field. The response is `422 Unprocessable Entity` with `code: validation_failed`, `details: {field: "title"}`, `correlation_id`.
- M3: An API consumer calls `GET /api/v1/cases/{id}` for a case in another tenant. The response is `404 Not Found` (not `403`, to avoid revealing existence) or `403 Forbidden` per the platform's choice; the choice is documented.
- M4: A duplicate call with the same `Idempotency-Key` and body returns the prior response.
- M5: A stale `If-Match` token returns `409 Conflict` with the current version in the response.
- M6: An external consumer subscribes to `CaseCreated` events. The event includes `event_type: CaseCreated`, `version: 1`, `event_id`, `occurred_at`, `tenant_id`, `case_id`, `correlation_id`, and a minimal payload (no PHI).

## 7. Planned slices

| Slice | Status | Notes |
|---|---|---|
| `001-case-management-platform` | Active | First slice ships the API surface for P01 (`/cases`), a minimal event catalog, and the error model. Outbox, idempotency, and pagination land in Phase 0 hardening. |
| Future slice 2 | Planned | Full event catalog published, outbox hardening, integration event shapes. |
| Future slice 3 | Planned | Webhooks, GraphQL, plugin extension points. |

## 8. Open questions

- Q1: When the API returns `404` for a case in another tenant, is that the right behavior, or should it be `403` to avoid existence-leakage? (Current intent: `404` if the actor is not authorized to know of the case's existence; the choice is documented per endpoint.)
- Q2: Are event payloads required to be backward-compatible within a major version, or can they evolve freely? (Current intent: additive changes only within a major version; breaking changes require a new event type version, e.g., `CaseCreated.v2`.)
- Q3: How is API deprecation communicated? (Current intent: `Sunset` header per RFC 8594, minimum 6 months notice, `Deprecation` header (RFC 9745) when available.)
