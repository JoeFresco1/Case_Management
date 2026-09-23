# ADR-019: Defense-in-depth security posture (fail-closed, deny-by-default, server-side enforcement)

- Status: Accepted (2026-09-02)
- Scope: `backend/app/application/authorization.py`, `backend/app/deps.py`,
  `backend/app/main.py`, `backend/app/infrastructure/telemetry/structured_logging.py`,
  `backend/app/infrastructure/secrets/*`, every fixtures directory under
  `backend/tests/**`, and the CI synthetic-data check.
- Supersedes: nothing.
- Related: AGENTS.md §4.R12 (Security First), `.specify/memory/constitution.md`
  §V, `specs/001-case-management-platform/first-slice/plan.md` §Audit &
  Identity, ADR-002 (seams), ADR-027 (P11 identity/role/authority).

## Context

The case-management platform handles restricted and PHI-classified fields
(`backend/app/application/cases/redactor.py`, `app/domain/case_type.py`),
must operate against synthetic data today and real (eventually
production) data tomorrow, and ships with multiple cross-tenant surfaces
(API, audit, outbox, telemetry, document store, secret store). The
Constitution §V and AGENTS.md §4.R12 codify seven non-negotiable security
properties:

1. **Fail-closed / deny-by-default** — unknown action, unknown role, or
   missing grant => `Forbidden`. The policy evaluator never returns
   "allow" by silence (`app/application/authorization.py:policy.evaluate`).
2. **Server-side enforcement only** — UI visibility never = authorization.
   All gated actions go through `authorize()` in the application layer
   before any repository call or response is produced.
3. **Identity isolation by environment** — `LocalHeaderIdentityProvider`
   (which reads `X-Dev-User` / `X-Dev-Tenant`) is **only** active when
   `APP_ENV=local`. In `shared`, `staging`, `prod`, the provider rejects
   those headers with `401 Unauthorized` even if the request otherwise
   looks valid (`app/deps.py:62`, `app/deps.py:98`).
4. **Synthetic marker always visible** — `SYNTHETIC_ENVIRONMENT=1` is
   emitted on startup, returned by `GET /api/v1/health`, and rendered in
   the UI banner (`app/main.py:95`, `interfaces/http/routers/health.py`).
   The marker exists so log/telemetry consumers can never confuse a
   developer machine for production data.
5. **SecretProvider never embeds secrets** — secrets are referenced by id
   at the seam, resolved at call time via `SecretProvider.get(id)`
   (`app/application/ports.py`, `app/infrastructure/secrets/local_secret_provider.py`).
   No literal credentials may appear in source, fixtures, workflow JSON,
   or audit payloads.
6. **PHI/secret redaction in every log and response** — every structlog
   processor pipeline runs `_redact_processor` against the bound event
   dict (`structured_logging.py:118`). The redaction list is the single
   source of truth for keys whose values must be replaced with
   `"[REDACTED]"` before a log line, error response, or audit payload
   leaves the process.
7. **No PHI / secrets / production identifiers in fixtures, logs, or
   payloads** — synthetic fixtures must use `example.test` domains,
   obviously fake names, no DOBs, no SSNs, no medical record numbers,
   no real-looking reference numbers. A CI check enforces this.

Earlier slices wired several pieces individually but never recorded a
single decision that names all seven properties, ties them to their code
homes, and lists the consequences for adapters and operators. This ADR
fills that gap and is the contract the security acceptance tests
(`backend/tests/security/`) verify against.

## Options considered

1. **Per-feature ad-hoc enforcement.** Each slice writes its own
   identity, policy, redaction, and synthetic-marker code.
   - Pros: ships fastest per slice.
   - Cons: drift across slices (one uses `Authorization: Bearer`, another
     uses `X-Dev-User`; one redacts `password`, another doesn't); no
     single audit story; `grep` for the policy evaluator misses dynamic
     dispatch. Violates Constitution V.

2. **Centralize in `application/` + enforce via deps/middleware
   (chosen).** `authorize()` is the only public entry; identity flows
   through `get_current_user`; redaction lives in one processor; secrets
   flow through `SecretProvider`; the CI synthetic-data check catches
   drift in fixtures.
   - Pros: one path per concern; layer rules from `import-linter` keep
     adapters from drifting into domain; redaction list can be tested
     with a single fixture-driven test; CI can refuse a PR that
     re-introduces `example.com` or a DOB into fixtures.
   - Cons: every cross-cutting concern needs a place, and we have to
     resist adding parallel implementations later.

3. **External policy engine (OPA/Cedar).**
   - Pros: declarative, audited, supports runtime reload.
   - Cons: introduces a network dependency for every authorization
     decision; conflicts with fail-closed / local-first stance;
     reserved for P11-US9 (break-glass) and later. Out of scope here.

## Decision

Adopt option 2. Each of the seven properties is wired in one place:

| # | Property | Code home | Enforcement |
|---|----------|-----------|-------------|
| 1 | Fail-closed / deny-by-default | `app/application/authorization.py:Policy.evaluate` | `authorize()` raises `Forbidden` on missing grant; `import-linter` forbids direct repository use in `interfaces/` |
| 2 | Server-side only | `app/application/authorization.py` + `app/deps.py:get_current_user` | Routers call `authorize()` before any repository call; UI uses the same checks as a hint, never as authority |
| 3 | `X-Dev-*` rejected outside `local` | `app/deps.py:LocalHeaderIdentityProvider` | `APP_ENV in {"local","test"}` => header accepted; otherwise `401` |
| 4 | `SYNTHETIC_ENVIRONMENT` marker | `app/main.py:lifespan`, `interfaces/http/routers/health.py` | Logged on startup, returned by `/api/v1/health`, rendered by the frontend banner |
| 5 | `SecretProvider` only | `app/application/ports.py:SecretProvider`, `app/infrastructure/secrets/local_secret_provider.py` | `lint-imports` forbids `os.environ["..."]` for known secret names outside `infrastructure/secrets/` |
| 6 | PHI / secret redaction | `app/infrastructure/telemetry/structured_logging.py:_redact_processor` | Same processor is invoked by `redact_sensitive(payload)` for audit payloads and error responses |
| 7 | Synthetic-data check | `scripts/ci_synthetic_data_check.py` + `backend/tests/security/test_phie_redaction.py` | CI step fails the build if `example.com`/DOB/SSN patterns appear in `backend/tests/**`, `backend/app/**/*.json`, fixtures, or commit messages |

The redaction list (`_REDACT_KEYS` in `structured_logging.py`) is
extended in this slice to cover: `private_key`, `client_secret`,
`refresh_token`, `session_id`, `cookie`, and `bearer`. The extension is
motivated by the fact that identity adapters and the upcoming P11 break-
glass work will emit these keys, and they must be redacted before they
reach log aggregation.

The CI synthetic-data check rejects:

- `example.com`, `acme-corp.com`, `@gmail`, `@yahoo`, `@hotmail` in any
  fixture, JSON, or commit message.
- Bare dates in `DOB:` / `birth_date:` / `dob:` patterns in fixtures.
- Bare 9-digit numbers matching `\d{3}-\d{2}-\d{4}` (SSN) or
  `\d{16}` (credit-card-shaped) in fixtures and logs.
- References to `prod-`, `live-`, or `corp-` prefixes in tenant ids or
  external refs.

## Consequences

Positive:

- Single grep target per concern: `Policy.evaluate` for authorization,
  `LocalHeaderIdentityProvider` for identity, `_REDACT_KEYS` for
  redaction, `lifespan` for the synthetic marker.
- A failing test in `backend/tests/security/test_phie_redaction.py`
  blocks any drift in redaction coverage.
- The CI synthetic-data check makes "I added a fixture with
  `bob@example.com`" a build failure, not a code-review nit.
- Auditors can map every Constitution §V property to one ADR row and
  one code home.

Negative / follow-ups:

- Any new identity adapter (Entra, on-prem AD) must re-implement the
  `APP_ENV=local`-only header rule; the ADR's row 3 is the contract.
  Reviewers should reject adapters that ship `X-Dev-*` acceptance in
  non-local environments.
- The redaction list is a denylist. New sensitive keys must be added
  explicitly. The companion `test_phie_redaction.py` test enumerates
  the current list and fails if any required key is missing.
- Field-level encryption at rest is **not** covered here. ADR-002
  (seams) marks the KeyVault adapter for `cases.data_json`; until that
  lands, the redaction list is the only protection at the persistence
  boundary.

## Azure-seam note

- **Identity**: `LocalHeaderIdentityProvider` is replaced by
  `EntraIdentityProvider` (P11-US7). The `APP_ENV`-only header rule
  stays the same; the adapter must read Entra-issued tokens, never
  `X-Dev-*`.
- **Secrets**: `LocalSecretProvider` is replaced by `AzureKeyVault`
  (P11-US7 / ADR-002). `SecretProvider.get(id)` is the call site — no
  environment-specific branches inside `application/` or `domain/`.
- **Telemetry**: redaction list is unchanged when the OTLP exporter
  (ADR-002) replaces `structlog.PrintLoggerFactory`. The processor
  pipeline is configured identically; only the sink changes.