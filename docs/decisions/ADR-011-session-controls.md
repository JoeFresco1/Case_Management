# ADR-011 — Session & Trusted-Client Controls (FM-049 / P11-US8, US11)

**Status:** Accepted (Gated) — design approved; production target selection requires an explicit decision
**Date:** 2026-09-02  
**Owner:** Platform team  
**Related Pillars:** P11 Security & Identity, P09 Audit, History & Compliance  
**Pillar Stories:** P11-US8 (session timeout + idle timeout + failed-attempt policy), P11-US11 (privileged-action client-cert requirement)  
**Tasks/Features Affected:** FM-049, `backend/app/domain/session.py`, `backend/app/application/security/session_controls.py`, `backend/app/interfaces/http/routers/security.py`, `backend/tests/security/test_session_controls.py`, `AGENTS.md:121`

## Context

P11 pillar §4 already lists two user stories the first platform does not address: **P11-US8** (idle / session timeout + failed-attempt lockout) and **P11-US11** (privileged-access-workstation client-cert requirement). The first slice (`001-case-management-platform`) implements `IdentityProvider`, the policy registry, and tenant isolation; both session stories are explicitly deferred to a later slice because they require a product decision (which timeout values, which client-cert authority, which actions go on the privileged list).

`01-deduplicated-feature-map.md:108` flags FM-049 as **H3 / Gated**. The horizon label is correct: a real-world deployment picks the timeout values per tenant and per regulatory regime; the privileged-action allowlist is the lever that turns a privileged-access-workstation program on or off. Both decisions belong with the operator, not the engineering team.

What this ADR decides:

1. The **shape** of the session-control surface so the next slice can ship the durable (Postgres / Azure SQL) persistence and the Entra integration seam without re-architecting.
2. The **default policy values** the platform ships so a tenant that never publishes a configuration still gets safe defaults.
3. The **Gated** posture for the privileged-action surface: the policy actions are published but no role grants them today, so the surface is deny-by-default until a product decision is made.

## Decision Required

What is the canonical shape of the session-control surface, and what stays Gated?

## Viable Options

### Option A — Application-layer Session + SessionPolicy Protocols with in-memory synthetic adapters (recommended)
- Define `Session` and `SessionPolicy` aggregates in `app/domain/session.py` with pure-domain lifecycle (idle / absolute expiry, failed-attempt counter, lockout window, approved-client-cert thumbprint).
- Define `SessionRepository` and `SessionPolicyRepository` Protocols in `app/domain/ports.py` (P11-US8 / P11-US11).
- Ship an in-memory adapter in `app/interfaces/http/routers/security.py` for the synthetic environment; reserve the Postgres / Azure SQL adapter for the next slice.
- Wire `GET /api/v1/security/sessions/{id}`, `POST /api/v1/security/sessions/{id}/check-privilege`, `POST /api/v1/security/sessions/{id}/approve-client-cert` (Gated), `POST /api/v1/security/authentication-failures`, `POST /api/v1/security/authentication-successes`, `GET/POST /api/v1/security/session-policy` (publish Gated).
- Add `Action.SESSION_REQUIRE_CERT` and `Action.SESSION_POLICY_PUBLISH` to the policy enum; **no role grants either today** (mirrors `AUDIT_STREAM` in ADR-009).
- Ship the documented defaults: idle 15 min, absolute 8 h, max 5 failures, lockout 15 min, privileged-action allowlist empty.
- **Pros:** Pure domain layer (no FastAPI / SQLAlchemy imports), so the domain is testable in isolation. Application use cases are thin orchestrators that compose the domain + the policy registry. The synthetic environment runs the full surface end-to-end without a database. The Gated posture (no role grants the action today) preserves deny-by-default and matches ADR-009 / P11 Q3.
- **Cons:** Synthetic environment only persists sessions in process memory. The Postgres / Azure SQL adapter, the Entra client-cert authority, and the operator console for "approved thumbprints" are deferred — they require a follow-up slice and a product decision.

### Option B — Push the session checks into the authorization layer
- Fold the session-control use cases into `app/application/authorization.py` so `Policy.evaluate` reads the session store directly.
- **Pros:** Single gate; fewer modules.
- **Cons:** Conflates *what* a caller may do with *whether* a caller may do anything at all. The policy registry would need to know about clock skew and cert stores; the Session aggregate's pure lifecycle would couple to the policy shape; the Gated posture is harder to express (the policy would still need a sentinel action). This is exactly the separation R3 warns about: "the call path needs a sceptical walk before committing".

### Option C — Skip the synthetic adapters; require a database from day one
- Make the session-control surface SQL-only; no in-memory adapter.
- **Pros:** Production-shape from day one.
- **Cons:** Violates AGENTS.md §4.R1 / Issue #1: WSL is unreliable on this host, so the synthetic environment needs to run without a database. Also bloats the first PR with a SQL migration that has not been agreed with the operator.

## Consequences / Tradeoffs

| Option | Schema/Migration | Public Contract | Tenant Isolation | Reversibility | Gated fit |
|---|---|---|---|---|---|
| A | None this slice; SQL adapter lands in follow-up | `GET/POST /api/v1/security/sessions/*`, `/security/authentication-failures|successes`, `/security/session-policy` | Strong (per-tenant protocol methods, Protocol-level tenant_id) | High — Postgres / Azure SQL adapter swaps behind the Protocol | Yes — `SESSION_REQUIRE_CERT` + `SESSION_POLICY_PUBLISH` added to the enum; no role grants either today |
| B | None | Same as A | Strong | Medium — `Policy` gains a SessionReader dependency | Partial — single gate, but the Gated semantics are harder to express |
| C | `sessions` + `session_policies` + `approved_client_certs` migrations | Same as A | Strong | High | Yes — but blocks synthetic env without a database |

## Recommended Option

**Option A — Application-layer Session + SessionPolicy Protocols with in-memory synthetic adapters, Gated via `SESSION_REQUIRE_CERT` + `SESSION_POLICY_PUBLISH`.**

## Why Recommended

- The domain stays pure (no FastAPI / SQLAlchemy imports) and is testable in isolation; the application use cases are thin orchestrators that compose the domain + the policy registry + the audit recorder.
- The synthetic environment can exercise the full surface end-to-end without a database, satisfying AGENTS.md §3 Issue #1 (WSL is unreliable).
- The Gated posture mirrors ADR-009 (`AUDIT_STREAM`): the action is in the enum, no role grants it, the surface is off-limits until a product decision is made.
- The wire shape is the same shape the SQL adapter will serve: a future PR replaces the in-memory store with `SqlSessionRepository` and `SqlSessionPolicyRepository`; the protocol is unchanged.

## Activation Checklist (Gated)

The privileged-action surface is **off** today. Activation requires:

- [ ] Product decision on the privileged-action allowlist (which actions require an approved client cert).
- [ ] Decision on the cert authority (local CA bundle in dev, Entra-issued in production, or both).
- [ ] Add `SESSION_REQUIRE_CERT` and `SESSION_POLICY_PUBLISH` to `ROLE_PROCESS_ADMIN.actions` in `app/application/authorization.py`.
- [ ] Migrate the in-memory store to `SqlSessionRepository` / `SqlSessionPolicyRepository` with `tenant_id` indexes and the audit + outbox wiring from FM-002 / FM-043.
- [ ] Update `/api/v1/health` to surface the active session-store adapter (per AGENTS.md §12).

## Defaults (shipped today, overridable per tenant)

| Setting | Default | Source |
|---|---|---|
| Idle timeout | 15 minutes | `SessionPolicy.DEFAULT_IDLE_TIMEOUT` |
| Absolute timeout | 8 hours | `SessionPolicy.DEFAULT_ABSOLUTE_TIMEOUT` |
| Max failed attempts | 5 | `SessionPolicy.DEFAULT_MAX_FAILED_ATTEMPTS` |
| Lockout duration | 15 minutes | `SessionPolicy.DEFAULT_LOCKOUT_DURATION` |
| Privileged actions | `frozenset()` | `SessionPolicy.DEFAULT_PRIVILEGED_ACTIONS` |

These match the AGENTS.md acceptance criteria ("idle 15min logs out", "5 failed attempts lock"). A tenant can tighten or loosen any value via `POST /api/v1/security/session-policy` after the Gated action is granted.

## Audit Events (P09 cross-cut)

| Event | Trigger | Payload |
|---|---|---|
| `SessionAuthenticationFailed` | Failed authentication attempt | `{failed_attempts, locked}` |
| `SessionLocked` | Counter crosses threshold | `{failed_attempts, locked_until}` (causation: `SessionAuthenticationFailed`) |
| `SessionAuthenticationSucceeded` | Successful authentication | `{}` |
| `SessionClientCertificateApproved` | Operator approved a cert (Gated) | `{session_user_id}` — **never the thumbprint** |
| `SessionPolicyPublished` | Tenant policy replaced (Gated) | `{idle_timeout_seconds, absolute_timeout_seconds, max_failed_attempts, lockout_duration_seconds, privileged_actions}` |

Thumbprints are referenced by id (`session_user_id` / `tenant_id`); the cert bytes themselves are never on the audit chain (AGENTS.md §18 redaction list).

## Seam Inventory (AGENTS.md §12)

| Seam | Port | Local adapter | Azure adapter | Status |
|---|---|---|---|---|
| Session store | `SessionRepository` (`app/domain/ports.py`) | `InMemorySessionStore` (`interfaces/http/routers/security.py`) | `SqlSessionRepository` (Postgres) → Azure SQL adapter | Deferred to follow-up slice |
| Session policy store | `SessionPolicyRepository` (`app/domain/ports.py`) | `InMemorySessionPolicyStore` (`interfaces/http/routers/security.py`) | `SqlSessionPolicyRepository` (Postgres) → Azure SQL adapter | Deferred |
| Client-cert authority | Implicit — operator-recorded thumbprints | Local validation against session row | Entra-issued certs (or local CA bundle) | Gated |
| IdP outcome integration | `record_authentication_failure` / `record_authentication_success` HTTP seam | Local IdP adapter (dev) | Entra IDP via webhook | Gated |

## Notes on Tenant Isolation

The `SessionRepository` Protocol declares `tenant_id` as the first parameter on every method; the SQL adapter MUST carry `WHERE tenant_id = :tenant_id` (AGENTS.md §3 R1). The in-memory adapter keys on `(tenant_id, session_id)` so a cross-tenant probe returns ``None`` indistinguishable from a missing id. The policy registration `before_compile` listener (`app/db.py`) is defense-in-depth; this ADR does not weaken it.

## Out of Scope (this slice)

- SQL adapter (`SqlSessionRepository`, `SqlSessionPolicyRepository`) — landed in the same follow-up that produces the durable session table.
- Break-glass integration (P11-US10 / ADR-014 deferred) — break-glass overrides the policy registry but not the session-control surface; the locked / idle boundaries still apply.
- AI boundary (P11-US12 / ADR-015 deferred) — AI suggestions are not authoritative on session-control events; the audit chain carries the human decision.

## Open Questions

- Q1: Does the operator want a per-user privileged-action allowlist in addition to the per-tenant one? (Current intent: per-tenant only; per-user grant lands in a future slice when break-glass is in scope.)
- Q2: Does the cert authority validate against a local CA bundle, an Entra-issued cert, or both? (Deferred to the Entra-integration slice.)