# ADR-046: Run G operability convergence (FM-052/055/056/057; FM-047 union catalog)

- Status: Accepted (2026-09-22)
- Scope: FM-052, FM-055, FM-056, FM-057 convergence for Run G (T7), plus
  FM-047 union-catalog registration as ridden scope.
- Related: FM-047, FM-052, FM-055, FM-056, FM-057; ADR-010 (extension/AI
  boundary), ADR-013 (outbound webhooks + G-3 hardening addendum), ADR-023
  (event compatibility), ADR-045 (system/diagnostics/storage); P13/pillar.md,
  P09/pillar.md, P11/pillar.md.

## Context

G-2 through G-7 built operability incrementally: the versioned event catalog
with publication-time compatibility gates, outbox drain hardening (safe
errors, backoff, dead-letter), the governed GraphQL read surface, diagnostics
and storage administration seams, webhook delivery hardening (domain-owned
cap, backoff gate, transport-fault tolerance), the operability HTTP/OpenAPI
surface, and the outbox triage UI. Five classification questions stayed open
at convergence: (1) where breaking event changes are rejected; (2) who owns
the webhook dead-letter transition; (3) whether a slow/poison webhook row can
starve the batch or a raising transport can kill the drain; (4) what a denied
governed query returns; (5) which Run G surfaces stay in-memory placeholders
vs durable rows.

## Decision

1. **Breaking event changes fail at publication, not in production (FM-055).**
   `validate_publication` rejects duplicate `(event_type, version)` shapes,
   version regressions, removed-key new versions, and PHI-named keys; `DomainEvent`
   construction validates against the catalog so malformed events never reach
   the durable outbox; unknown types dead-letter instead of publishing
   (`domain/events.py`, ADR-023).
2. **The webhook dead-letter cap is domain-owned (FM-056).** `record_attempt`
   takes `max_attempts` and owns `PENDING → DEAD_LETTERED`; `record_permanent_failure`
   fast-paths poison (permanent 4xx), missing-secret, and missing-subscription
   rows without burning the retry budget; `backoff_delay_seconds`/`delivery_due`
   reuse the G-2 outbox schedule (SQLite-naive reads normalized to UTC); no
   caller can retry forever by forgetting the check (ADR-013 addendum).
3. **Dispatch is backoff-gated and transport-fault tolerant (FM-056).**
   `dispatch_pending` skips not-due rows without POSTing or touching the
   attempt counter (manual `attempt`/replay bypasses by design, so operators
   keep an immediate path); a raising transport is recorded as a retriable
   `status_code=None` failure instead of killing the tenant drain; duplicate
   enqueue hits `UNIQUE (tenant_id, subscription_id, event_id)` and returns
   the prior row, with a stable `X-Webhook-Event-Id` for subscriber dedup.
4. **Denied governed queries return null branches, never partial data (FM-057).**
   Resolvers call the application service layer only (no infrastructure
   imports, pinned by test); tenant scoping is implicit in the actor (no
   `tenantId` argument; cross-tenant case → null+error, list → empty);
   sensitive fields redact, documents filter by `DOCUMENT_READ` +
   classification allowlist; bounds (8000 chars / 30 selections / depth 4 /
   limit ≤200, mutations/variables rejected) match at HTTP and direct
   `execute()`; every request appends one `GraphQLQueryExecuted` audit row
   without result data.
5. **Extension registry stays process-local with a declared persistent overlay
   (FM-047).** Registration is governed (`ADMIN_TENANT_CONFIG`, idempotent
   identical retry, `409` on shape conflict) and audited; extension event
   types join the union catalog with built-ins winning on conflict (old
   subscribers keep the built-in contract). The persistent
   `extension_registry` table is FM-047 slice-2 design behind the same
   Protocols — durability lands when a pilot needs queryable history. These
   are not gaps.
6. **Diagnostics/storage admin stays infrastructure-only and fail-closed.**
   `LocalStorageVolume` serves `local-fs`, `LocalEncryptionAtRest` is
   plaintext-local, sinks are declared config only; declaring `azure-blob` /
   enabled encryption without the seam fails closed with `declared` vs
   `actual` posture (ADR-045). No Gated switches flipped.

## Consequences

- T7 acceptance holds: concurrent edits conflict structurally; replays are
  idempotent; poison dead-letters with safe errors only; unauthorized
  governed reads are forbidden null branches, never partial rows.
- Known debt carried (not Run G gaps): repo-wide `ruff check`/`format`
  failures are confined to `app/domain/form.py` (Run E debt per ADR-043 —
  Run G adds zero new findings); full-suite-from-cold-start exceeds the tool
  timeout (phase-scoped suites pass; full run belongs in CI); Postgres path
  unverified on this host per the WSL limitation (sqlite profile + portable
  generics + migration parity in CI).
- Gate debt is explicit in `architecture-runG.md §8`: blockers are reported
  with exact scope, none hidden.

## Azure-seam note

No seam added or flipped. Run G reuses seams 2/3/5/6 (document store,
messaging + outbox dispatcher + event catalog, secrets, telemetry) and the
`OutboundDeliveryTransport` → Event Grid placeholder reserved by ADR-013;
the SIEM/signed-batch surface keeps its Gated marker; the deterministic core
stays LLM/SDK-free per Constitution XIV — no ranker or assistant
participates in outbox, dispatch, webhook, extension, or governed-query
decisions.
