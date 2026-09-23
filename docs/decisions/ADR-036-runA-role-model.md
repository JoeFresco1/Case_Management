# ADR-036: Run A Role Model — Permission Strings in Domain, Enum at the Boundary

- Status: Proposed (2026-09-12, 002-leftovers Run A)
- Scope: `backend/app/domain/role.py`, `backend/app/application/roles/manage_roles.py`,
  `backend/app/domain/ports/role_session.py`, `backend/app/interfaces/http/routers/admin_roles.py`
- Related: FM-048 (P11-US4), FM-021 (governance history), ADR-027 (role authority, Option A),
  `specs/002-leftovers/architecture-runA.md §2–§3`

## Context

FM-048 requires security administrators to define roles, assign permissions and
users, and observe revocation without weakening deny-by-default enforcement.
ADR-027 already decided role authority (Option A: local seeded roles in dev,
IdP claims in prod, single `tenant_id` per request with audited switch). What
remained open is the in-code shape: where the permission vocabulary lives, how
reserved seed roles are protected, and how fast revocation takes effect.

## Options considered

1. **Permission strings in domain, `Action` enum validated at the application
   boundary (chosen).** `Role.permissions` carries canonical `Action.value`
   strings (`"case.create"`); `manage_roles._validate_permissions` rejects
   unknown actions before constructing the aggregate; the policy converts back
   to the enum when evaluating. The domain never imports `app.application`,
   so the layer rule (domain pure) holds.
   - Pros: layer rules stay green; wire format, persistence, and policy share
     one string vocabulary; unit tests exercise the aggregate without the
     policy registry.
   - Cons: two representations of the same vocabulary (string + enum) must be
     kept in sync; adding an `Action` without updating admin validation is a
     latent 422.

2. **Import the `Action` enum directly in `domain/role.py`.**
   - Pros: single vocabulary.
   - Cons: domain imports from application — violates `import-linter` layer
     contract (AGENTS.md §4.R10). Rejected.

3. **Cache effective permissions per user/session.**
   - Pros: fewer joins on the hot path.
   - Cons: revocation needs invalidation; any invalidation gap is a
     fail-open window. Rejected — P11-US4 acceptance demands immediate
     revocation, and the live `permissions_for_user` read on every `evaluate`
     call already satisfies it with no cache to invalidate.

## Decision

Option 1, plus:

- `Role` is frozen; `grant`/`revoke` return new values (in-place mutation would
  silently change authorization).
- Reserved names (`intake`, `reviewer`, `supervisor`, `process_admin`) cannot be
  created, modified, or deleted through the admin API; the factory only allows
  them when explicitly seeding (`is_reserved=True`).
- Every mutation requires `ADMIN_TENANT_CONFIG`, is tenant-scoped, and appends
  a stable audit event (`RoleDefined`, `RoleAssigned`, …) in the same
  transaction as the domain change.
- Known gap (not silent): role mutations write audit but no outbox row yet —
  see `architecture-runA.md §3` for the prescribed atomic wire-up and §6 for
  the gap table.

## Consequences

- `ruff + mypy --strict + import-linter` stay green on the role path; the only
  `Any` is the `created_at`/`assigned_at` boundary typing already present.
- Adding a new `Action` requires updating the admin permission surface and its
  tests, or the new permission is ungrantable (fail-closed, acceptable).
- Revocation latency is one request (next `evaluate` reads the deleted row).

## Azure seam

`IdentityProvider` (`app/deps.py`) — `LocalHeaderIdentityProvider` in dev maps
seeded users to roles; `EntraIdentityProvider` (`docs/integrations/seams.md`
row 4) maps Entra claims to the same `Action.value` strings in prod. Role
definition stays local per tenant; only the claim→permission mapping is an
adapter concern, never domain logic. Env: `AZURE_ENTRA_TENANT_ID`,
`AZURE_ENTRA_CLIENT_ID`, `AZURE_ENTRA_AUDIENCE`.
