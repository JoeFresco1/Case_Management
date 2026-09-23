# ADR-009 — SIEM Stream & Signed Audit Batches (FM-046 / P09-US8, US9)

**Status:** Accepted (Gated) — design approved; production target selection requires an explicit decision
**Date:** 2026-09-02  
**Owner:** Platform team  
**Related Pillars:** P09 Audit, History & Compliance, P11 Security & Identity, P13 API & Event Contract  
**Pillar Stories:** P09-US8 (stream to external SIEM), P09-US9 (signed batches for off-system auditors)  
**Tasks/Features Affected:** FM-046, `T05-046` `task_2c7cef14593e`, `backend/app/application/audit/siem_stream.py`, `backend/app/interfaces/http/routers/audit.py:577` `/audit/signed-batches`, `backend/app/infrastructure/persistence/siem.py`, `backend/app/infrastructure/persistence/models.py:214` `signed_audit_batches`, `AGENTS.md:121`

## Context

P09 already ships an **append-only** per-tenant hash chain with a verifiable `verify-audit` CLI (P09-US3, FM-003, `T01-003`). The first slice has no externally-verifiable surface: a regulator who wants to take the chain off-system must either trust the platform's read API or pull a structured export (P09-US5). P09 pillar §7 defers "Signed batches, SIEM streaming, custom event types" to a future slice — this ADR decides how that future slice starts.

Pillar §7 also places the **deferral reason** explicitly: real-time streaming to an external SIEM is the regulatory bridge between the platform and the customer's existing SOC; the surface must not weaken the chain and must be deny-by-default until a product decision is made.

The two user stories are independent at the data plane and share a single authorization surface:

- **P09-US8** — system streams audit events to an external SIEM in real time. Best-effort; the durable chain is the source of truth.
- **P09-US9** — auditor requests a signed batch of audit events. The batch carries a signature the auditor's tooling can verify off-system.

Both surfaces must be **Gated** per the FM-046 horizon label ("H3 / Gated" in `01-deduplicated-feature-map.md:105`) because they require a product decision (which Azure SIEM, which signing algorithm) and explicit role authorization before activation.

## Decision Required

What is the canonical shape of the externally-verifiable audit surface, and what stays Gated?

## Viable Options

### Option A — Application-layer SIEM Protocol + signed-batch Protocol (recommended)
- Define `SiemSink` and `SignedBatchRepository` Protocols in `application/audit/siem_stream.py`; ship a `local-in-memory` adapter for the synthetic environment; reserve the Azure tamper-evident blob store for a follow-up slice.
- Stream: read the durable `audit_events` table, push rows to the sink in chronological order. The stream is best-effort; a sink error is recorded in telemetry and does not roll back the durable commit.
- Signed batch: read a contiguous span from the durable chain, verify the chain via `app.domain.audit.verify_chain`, sign the canonical JSON with HMAC-SHA-256 under a per-tenant key, persist via the repository.
- Gated via a new policy action `audit.stream` (no role grants it today) so the surface is deny-by-default until a product decision is made.
- **Pros:** Preserves the four-layer immutability invariant — the chain lives in `audit_events` and the stream / batch are *views* over it. Tenant isolation is the same `tenant_id` filter. Testable in-memory (sqlite + AGENTS.md §4.R1). Reversible: the Protocols are unchanged when the Azure adapter swaps in.
- **Cons:** HMAC-SHA-256 is symmetric — the auditor must trust the platform's key. The Azure adapter replaces this with asymmetric signing (RSA / ECDSA) once the vendor is selected. The Gated label means v1 ships without an off-system integration.

### Option B — SIEM-only via Event Hub forwarder, no signed batches
- Forward `audit_events` rows to Azure Event Hub on commit; no signed-batch surface.
- **Pros:** Simple; reuses the existing outbox dispatcher shape (FM-055, ADR-028).
- **Cons:** Drops P09-US9 entirely. The auditor cannot take an attestation off-system; a regulator's only option is to trust the platform's read API. Fails the FM-046 boundary.

### Option C — SIEM + signed batches in the same outbox
- Treat signed batches as a class of outbox event; the dispatcher creates them.
- **Pros:** Reuses outbox durability.
- **Cons:** Conflates two failure modes: outbox rows are "send once, retry on failure" — a signed batch is a synchronous, auditor-driven request. Mixing them would either slow the dispatcher (it has to wait for the auditor) or produce signatures in the wrong order (the chain has to be locked). A separate port keeps the boundaries clean.

## Consequences / Tradeoffs

| Option | Schema/Migration | Public Contract | Tenant Isolation | Reversibility | Gated fit |
|---|---|---|---|---|---|
| A | `signed_audit_batches` placeholder + Protocol (Gated; no live DDL until Azure target is selected) | `GET/POST /api/v1/audit/signed-batches`, `audit.stream` action | Strong (per-tenant, Protocol-scoped) | High (Azure adapter is a class behind the Protocol) | Yes — Gated via action + no role grant |
| B | None | Stream forwarder only | Strong | High | No — drops P09-US9 |
| C | Reuses outbox | Same as A but with dispatcher coupling | Strong | Medium — outbox tuning required | Partial — auditor-driven |

## Recommended Option

**Option A — Application-layer SIEM Protocol + signed-batch Protocol, Gated via `audit.stream`.**

## Why Recommended

- **Faithful to P09 §7 deferral.** The pillar explicitly defers "Signed batches, SIEM streaming, custom event types" to a future slice. Shipping the Protocols and the `audit.stream` action is exactly the "future slice starts here" shape: a stable port the Azure adapter can plug into without a migration.
- **Preserves the four-layer immutability invariant.** The stream and the signed batch are both **views** over the durable `audit_events` table. A SIEM outage does not weaken the chain because the chain does not pass through the SIEM. A signature tamper attempt is detectable by re-reading the rows through the standard audit read API and recomputing the chain.
- **Aligns with ADR-026 (per-tenant chain scope).** The signed batch's `first_event_hash` and `last_event_hash` are per-tenant; the stream is per-tenant; the Gated action is granted per-tenant through the role policy. No global ordering assumption.
- **Deny-by-default.** The new `audit.stream` action joins `AUDIT_READ` and `AUDIT_EXPORT` in the policy registry but is not granted to any role (`ROLE_INTAKE`, `ROLE_REVIEWER`, `ROLE_SUPERVISOR`, `ROLE_PROCESS_ADMIN`). The HTTP endpoint returns 403 to any caller today; an explicit operator decision grants the role the action.
- **Reversible.** The Azure adapter is a class behind the `SignedBatchRepository` Protocol. The HMAC-SHA-256 signature is pinned in the wire shape (`algorithm="HMAC-SHA-256"`) so the v2 asymmetric adapter can be released as a versioned schema change rather than a wire break.
- **Testable without a container.** The `InMemorySiemSink` and `InMemorySignedBatchRepository` satisfy the Protocols so the use case, the HTTP route, and the acceptance tests converge against the AGENTS.md §4.R1 sqlite profile.

## Gated Implementation Note

The signed-batch SQL adapter is **intentionally a placeholder** in `models.py:214`. Shipping a DDL migration that emits a `signed_audit_batches` table would commit the project to a column shape before the Azure SIEM target is selected. The application layer reads/writes through a Protocol so the placeholder does not block convergence. Migrations land with the Azure adapter in a follow-up slice.

The Gated label applies to:

- The Azure SIEM target (vendor, signature algorithm, key-management seam).
- The wire signature algorithm (HMAC-SHA-256 → asymmetric upgrade).
- The `audit.stream` role grant (no role grants it today).

## Reversibility / Migration Implications

- **Before any signed batch is published:** the Protocol and the route can be removed or re-pointed at a different store with no on-disk change.
- **After batches exist:** the wire `algorithm` field pins the version; v2 can be released as a new column or a new `algorithm` label, not a wire break. Auditors holding v1 batches continue to verify them with the v1 algorithm.
- **HMAC vs asymmetric:** when the Azure adapter is selected, the wire shape is extended (`signature_format="asymmetric-v1"`, `public_key_id=...`) so existing v1 consumers continue to verify their batches. The router publishes both algorithms for a transition window.

## Tasks/Features Affected

- `T05-046` `task_2c7cef14593e` (this task): ships the Protocol, the Gated action, the HTTP route, the in-memory adapters, the placeholder table, this ADR, and the acceptance tests.
- Follow-up slice: select the Azure SIEM target, implement the Azure `SiemSink` (Event Hub / Service Bus forwarder), implement the Azure `SignedBatchRepository` (tamper-evident blob store with asymmetric signing), grant `audit.stream` to the operator role, write the DDL migration for `signed_audit_batches`.
- FM-003, FM-043, FM-044, FM-046.

## Approval Gate

**Created:** `gate_siem_signed_batches` on `task_2c7cef14593e` (proposed). Blocks production deployment of the SIEM surface until the Azure target is selected and `audit.stream` is granted to a named role.

## Azure Seam

- `SiemSink` Protocol: `app/application/audit/siem_stream.py` — local `InMemorySiemSink` adapter at `infrastructure/persistence/siem.py:36`; Azure adapter placeholder `# INTEGRATION_SEAM: Azure SIEM — replaced when corp env available` at the same location.
- `SignedBatchRepository` Protocol: `app/application/audit/siem_stream.py` — local `InMemorySignedBatchRepository` at `infrastructure/persistence/siem.py:60`; Azure tamper-evident blob store replaces this.
- `signed_audit_batches` table placeholder: `infrastructure/persistence/models.py:214` — column shape documents the minimum row the Azure adapter must satisfy; DDL deferred until target selection.
- `audit.stream` policy action: `app/application/authorization.py:90` — Gated, no role grants it today.

---

*Status Accepted (Gated). Do not enable `audit.stream` in any environment without a superseding ADR that names the Azure SIEM target and the granted role.*
