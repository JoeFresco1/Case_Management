# ADR-015 — Connector Lifecycle, Gated Named Connectors, and Safe Drain (FM-058)

**Status:** Accepted (Gated)  
**Date:** 2026-09-02  
**Owner:** Platform team  
**Related Pillars:** P14 Integrations & Migration, P09 Audit & Evidence, P11 Security & Identity, P13 API & Contract  
**Pillar Questions:** `specs/pillars/P14-integrations-migration/pillar.md` US5, US7, Q3  
**Tasks/Features Affected:** FM-058 (`specs/pillars/01-deduplicated-feature-map.md:122`); `backend/app/domain/connector.py:1`; `backend/app/application/connectors/manage_connectors.py:1`; `backend/app/interfaces/http/routers/connectors.py:1`; `backend/app/domain/ports.py` (`ConnectorRepository`); `backend/app/application/authorization.py` (`Action.CONNECTOR_NAMED_IMPLEMENT`); `backend/tests/connectors/test_lifecycle.py:1`; AGENTS.md §3 R12, §4 R8, §12.

## Context

Pillar P14 (`specs/pillars/P14-integrations-migration/pillar.md`) states the platform's strongest principle on the integration boundary: **no connector to an enterprise system is enabled without explicit authorization, approved credentials, approved network access, and an approved integration design** (Constitution V.10, XII.5). A connector is a separately authorized, separately versioned, separately auditable component; a connector cannot be enabled merely because a URL or credential is present.

The first slice (`001-case-management-platform`) ships with **no** enterprise connector enabled (P14-US1, N1 from `pillar.md:82`). The connector framework is referenced but not implemented. FM-058 (`01-deduplicated-feature-map.md:122`) is the next milestone: an **approved connector lifecycle** where a security administrator approves, enables, monitors, retires, and safely drains an enterprise connector, with the additional Gated rule that **implementing any named connector requires separate explicit authorization** (US5, US7).

Two design questions are not answered by the pillar or the feature map:

1. **Where is the authorization gate enforced?** The application layer's `authorize()` call is the policy half; the domain's `__post_init__` is the deterministic backstop. Both must agree, and the domain check must be independent of the application path so a misconfigured HTTP route cannot bypass the gate.
2. **How does retirement interact with in-flight traffic?** A connector that retires while in-flight work is still running silently loses that work, which is a Constitution V violation. The deterministic invariant is "no retirement while `in_flight_count > 0`". The operator confirms the count before the terminal transition; the use case refuses to issue the transition if the count is non-zero.

A third question — what kinds of connectors are recognized, and which are "named" — is the surface where the Gated decision lands. The pillar's Q3 records the current intent: "OnBase connector is part of the public roadmap, gated by explicit authorization, with a separate feature slice". This ADR therefore reserves `ONBASE` and `PANZURA` in the catalog but locks them behind a separate Action; the first slice ships `SYNTHETIC` only.

## Decision

The connector framework is implemented as three layers plus a Gated authorization action:

### 1. Domain aggregate (`backend/app/domain/connector.py`)

`Connector` is a frozen, tenant-scoped aggregate with the lifecycle:

```
DRAFT ──approve──▶ APPROVED ──enable──▶ ENABLED ──begin_drain──▶ DRAINING ──retire──▶ RETIRED
                          │                    │
                          └──── (any failure) ──┘
```

Each transition is a method that returns a new `Connector` (Constitution XV — boring over clever). The state machine is one-way: a retired connector cannot be re-enabled; a new connector with a new id is required. Idempotent transitions return the aggregate unchanged so an operator-UI retry does not produce a duplicate audit event.

The **named-connector authorization gate** is enforced in the aggregate's `__post_init__`:

```python
if self.kind not in _UNRESTRICTED_KINDS:
    if self.implementation_authorized_by is None:
        raise InvariantViolation(
            f"named connector of kind {self.kind.value!r} requires "
            "implementation_authorized_by; synthetic-only in first slice"
        )
```

A kind outside `_UNRESTRICTED_KINDS` (`ONBASE`, `PANZURA`, anything added in the future) refuses to construct without `implementation_authorized_by`, `implementation_authorized_at`, and a non-empty `authorization_rationale`. The check is in the domain so a misconfigured application path cannot bypass the gate.

### 2. Application use cases (`backend/app/application/connectors/manage_connectors.py`)

The application layer owns the audit + outbox + repository plumbing. The five public entry points are:

- `draft_connector(cmd, *, connector_repo, audit, outbox, clock)` — DRAFT
- `approve_connector(cmd, *, ...)` — DRAFT → APPROVED
- `enable_connector(cmd, *, ...)` — APPROVED → ENABLED
- `monitor_connector(query, *, connector_repo)` — read-only snapshot
- `retire_connector(cmd, *, ...)` — ENABLED → DRAINING → RETIRED

Authorization:

- Every entry point requires `Action.ADMIN_TENANT_CONFIG`.
- `draft_connector` additionally requires `Action.CONNECTOR_NAMED_IMPLEMENT` for any non-synthetic kind.

`CONNECTOR_NAMED_IMPLEMENT` is **Gated**: it is declared in `Action` but no role grants it today. A process administrator with `process_admin` can draft a `synthetic` connector; the same actor is denied when attempting a named kind. The product decision to grant the action to a role is a future ADR.

### 3. HTTP router (`backend/app/interfaces/http/routers/connectors.py`)

Endpoints:

| Method | Path | Body | Use case |
|---|---|---|---|
| `POST` | `/api/v1/admin/connectors` | `DraftConnectorRequest` | `draft_connector` |
| `GET`  | `/api/v1/admin/connectors` | — | list |
| `POST` | `/api/v1/admin/connectors/{id}/approve` | `ApproveConnectorRequest` | `approve_connector` |
| `POST` | `/api/v1/admin/connectors/{id}/enable`  | — | `enable_connector` |
| `GET`  | `/api/v1/admin/connectors/{id}/monitor` | — | `monitor_connector` |
| `POST` | `/api/v1/admin/connectors/{id}/retire`  | `RetireConnectorRequest` | `retire_connector` |

The first slice ships a placeholder `ConnectorRepository` in the router that returns `None` for every read and is a no-op for every write. The placeholder is honest: the production SQL adapter is a separate feature slice and a **SQL repository is required** before the endpoints can serve real traffic. The HTTP layer surfaces 404 for any read or write that does not find a row, so the endpoints are consistent with the rest of the platform.

### 4. Safe-drain invariant (P14-US7)

`retire_connector` runs three sub-transitions in one HTTP request:

1. `begin_drain` — `ENABLED → DRAINING`. Always issued. New traffic is refused. Audit + outbox events are emitted (`ConnectorDrainStarted`).
2. `confirm_drain` — records the operator's snapshot (`in_flight`, `completed`, `rejected`). The snapshot is durable on the row. Audit + outbox events are emitted (`ConnectorDrainConfirmed`).
3. `retire` — `DRAINING → RETIRED`. **Issued only when `in_flight == 0`**. When the operator's snapshot reports in-flight traffic, the use case leaves the connector in `DRAINING` and returns `drained=False`; the operator re-issues the retire once the count reaches zero.

The domain's `Connector.retire()` raises `InvariantViolation("cannot retire a connector with in-flight traffic; confirm drain first (in_flight_count must be 0)")` if the operator attempts the transition with `in_flight > 0`. The HTTP layer surfaces this as 409. This is the deterministic backstop of P14-US7: a connector that retires with in-flight work loses that work silently, which is a Constitution V violation.

### 5. Integration seam

The connector adapter is a separate infrastructure module per AGENTS.md §12. The first slice's `DEFAULT_SYNTHETIC_ADAPTER_REF` points at `app.infrastructure.connectors.synthetic` — a placeholder directory. Production wires the named-connector adapters there when the Gated authorization is recorded. The seam is annotated with `# INTEGRATION_SEAM: connector — local synthetic adapter; production wires the named connectors here when ADR-015 Gated authorization is recorded`.

## Idempotency and Audit

Every lifecycle transition appends an `audit_events` row and an `outbox` row in the same transaction. The audit payload carries:

- `key`, `kind`, `from_state`, `to_state` (transitions only)
- `named` — whether the connector is a named kind
- `approved_by` / `rationale` (approve only)
- `drain_notes`, `in_flight_at_retire`, `completed_at_retire`, `rejected_at_retire` (retire only)

The audit row is the durable evidence for "who authorized this connector, when, with what rationale". The outbox row fans the event to subscribers (operator console, future SIEM) without coupling the use case to a particular bus.

## Schema Implications

This ADR does not change the database schema. The first slice's HTTP layer uses a placeholder in-memory repository; a SQL adapter for `connectors` lands when the Gated authorization is recorded (per AGENTS.md §3 R1, only generic SQLAlchemy types; `tenant_id` on every FK; the canonical tables list in `AGENTS.md:§16.1` already includes `connectors` in spirit but is not yet populated). The port `ConnectorRepository` is declared in `app/domain/ports.py:983` so the persistence shape is fixed from day one.

## Consequences

- **Security**: the named-connector gate is dual-enforced (application policy + domain invariant). A misconfigured HTTP route, a forgotten authorization call, or a bug in the policy module cannot bypass it.
- **Operations**: a single synthetic connector exercises the full lifecycle in tests; the audit chain captures every transition; the operator console can replay the timeline.
- **Compliance**: P14-US5 and P14-US7 are addressed end-to-end. The "no connector enabled by default" rule of P14-US1 is preserved because the Gated action has no role grant.
- **Future work**: when the product authorizes a named connector, the chain is (a) update AGENTS.md to record the authorization, (b) add a role that grants `CONNECTOR_NAMED_IMPLEMENT`, (c) implement the named adapter in `app/infrastructure/connectors/`, (d) replace the placeholder repository with the SQL implementation, (e) update this ADR with a new "Status: Gated → Authorized" note.

## Alternatives Considered

1. **Single Action for the whole lifecycle (no separate named-implement gate)**: rejected because the gate is the entire point of US5. A single Action would let a process administrator enable an OnBase connector without the explicit named-connector authorization.
2. **Authorization in the HTTP layer only**: rejected because the domain check is the deterministic backstop. A future refactor of the HTTP router (e.g. a new gRPC endpoint) could not bypass the gate.
3. **Auto-drain on retire (no operator confirmation)**: rejected because the safe-drain invariant is "no in-flight traffic", and only the operator can confirm that. An automatic drain would still need to estimate in-flight count, which is impossible without an outbox + adapter view. The operator-confirmed snapshot is honest and auditable.
4. **Re-approval of a retired connector**: rejected because the state machine is one-way. Re-approval would let a "retired" connector re-enter `APPROVED` without a new draft + new id, which would let the audit chain lose the boundary between "we retired this" and "we revived this". A new connector with a new id is required.

## Open Questions

- **Q1**: When a named connector is authorized, does the role grant cover the entire tenant or only the security administrators? (Current intent: security administrators only; a separate ADR is required for any broader grant.)
- **Q2**: Does the placeholder repository need a SQL adapter before the endpoints are exposed, or can the endpoints ship with the placeholder? (Current intent: the endpoints ship with the placeholder; the SQL adapter lands when a named connector is authorized, which is the only path that would produce real traffic. The placeholder returns 404 for any read and a no-op for any write; the audit + outbox events still fire so the test suite can verify the chain.)
- **Q3**: Is a "re-approval" of a retired connector ever desirable? (Current intent: no. The state machine is one-way. A new connector with a new id is required to revive a capability.)

## Integration Seam (AGENTS.md §12)

| Seam | Port | Local adapter | Production adapter | Status |
|---|---|---|---|---|
| Connector adapter | `Connector.adapter_ref` | `app.infrastructure.connectors.synthetic` (placeholder) | `app.infrastructure.connectors.onbase` (Gated) | Gated |
| Connector repository | `ConnectorRepository` (declared in `app/domain/ports.py:993`) | placeholder in router | SQL adapter (pending) | Gated |

The seam is annotated in the code with `# INTEGRATION_SEAM: connector — local synthetic adapter; production wires the named connectors here when ADR-015 Gated authorization is recorded`.
