# Pillar P11 — Security & Identity

**Status**: Draft · **Owner**: Platform team · **Updated**: 2026-08-31

> Least privilege, deny by default, server-side enforcement, separation of duties, identity-portable.

## 1. Pillar statement

This pillar owns **identity** and **authorization**: who is acting, what they can do, and what they can see. The pillar is the security backbone of the platform and is non-negotiable.

Key commitments:

- **Server-side enforcement**: authorization is evaluated server-side for every protected operation. UI visibility is never an authorization mechanism (Constitution V).
- **Default-deny**: an action is allowed only if a published policy explicitly allows it.
- **Identity-portable**: the application consumes a normalized identity and claims model. Local dev uses seeded identities; production uses Microsoft Entra ID (or another approved IdP) behind the same `IdentityProvider` port.
- **Multi-tenant**: every protected operation is scoped to a tenant. A failing or absent tenant filter must not silently allow cross-tenant access.
- **Least privilege**: an actor is granted only the permissions required for their role.
- **Separation of duties**: consequential decisions (approvals, dispositions, configuration publish) can require a different identity from the originator.
- **Break-glass**: an authorized actor can override authorization for an emergency, with explicit reason and enhanced audit.
- **AI cannot bypass security** (Constitution XIV): an AI suggestion is never authoritative on a consequential action.

## 2. Scope

### In scope
- `IdentityProvider` port: a normalized `UserContext` is the only thing the application sees.
- `Policy` registry: a small, declarative, versionable authorization model.
- Authorization at the use-case level: every use case calls `policy.evaluate(action, context, resource)` and refuses if not allowed.
- Per-tenant isolation: every repository method takes `tenant_id` explicitly; every query includes `WHERE tenant_id = :tenant_id`. A SQLAlchemy tenant-injection listener may be added as defense-in-depth, opt-in via a config flag, and is never the sole mechanism.
- Field-level and document-level access control.
- Action-level permissions and separation-of-duties constraints.
- Break-glass access with reason and audit.
- Audit of every access and authorization decision (P09).
- AI boundary: AI suggestions can inform but cannot authorize; the AI suggestion is itself an audit event.

### Out of scope (deferred)
- A full RBAC designer UI. (P3; v1 ships a JSON policy registry.)
- ABAC (attribute-based access control) with arbitrary expressions. (P3; v1 uses a constrained declarative policy language.)
- Identity governance: account lifecycle, deprovisioning, joiner-mover-leaver. (P14; this pillar consumes identity, it does not manage it.)
- A "super admin" role. (P3; v1 has no super-admin; break-glass is the only override.)

### Explicitly not building
- Client-side authorization. None. Ever.
- A "trust the client" mode. The server is the source of truth.
- A path that lets a non-tenant row be read because the row was found by id alone. Every read is tenant-scoped.

## 3. Primary entities

| Entity | Purpose | First slice |
|---|---|---|
| `IdentityProvider` (port) | The identity boundary. | Yes (local header) |
| `UserContext` | The normalized identity: user_id, tenant_id, display_name, roles, is_system. | Yes |
| `Policy` | An action → allow/deny decision. | Yes (basic registry) |
| `PolicyAction` | A controlled action name (e.g., `case.create`, `case.read`, `workflow.publish`). | Yes |
| `PolicyResource` | The resource the action targets (case, document, configuration). | Yes |
| `AuthorizationContext` | The actor + resource + extra claims. | Yes |
| `Role` | A named bundle of permissions. | Yes |
| `BreakGlassGrant` | A time-bound override with reason. | Deferred (P3) |
| `AuthorizationAuditEvent` | An audit event for an authorization decision. | Yes |

## 4. User stories (priority-tiered)

### P1 — Critical
- **P11-US1**: As a system, every protected use case evaluates `policy.evaluate(action, context, resource)` and refuses if not allowed; UI hiding is never the only check.
- **P11-US2**: As a system, every read and write of case/document data is scoped to a tenant; a user in tenant A cannot read a case in tenant B by any path, including guessed ids, raw SQL, or a misconfigured listener.
- **P11-US3**: As a developer, in `app_env=local`, I can use header-based dev identities; in any other environment, header-based identity is rejected (fail-closed).

### P2 — Important
- **P11-US4**: As a process administrator, I can define a role with a set of permissions; users assigned to that role gain the permissions; revoking the role revokes the permissions.
- **P11-US5**: As a security administrator, I can require a separation-of-duties check on a consequential action (e.g., approval); the same identity that created the case cannot be the approver.
- **P11-US6**: As a security administrator, I can mark a field as sensitive (P05); only actors with field-level permission can read the field's value in the response.
- **P11-US7**: As a security administrator, I can mark a document as restricted; only actors with document-level permission can access it.
- **P11-US8**: As a security administrator, I can configure a session timeout, an idle timeout, and a maximum failed-attempt policy.
- **P11-US9**: As a system, every authorization decision (allow and deny) is an audit event with the actor, action, resource, and reason.

### P3 — Deferred
- **P11-US10**: As a security administrator, I can grant a break-glass override for a bounded period; the override is audited; the actor's actions during the override are flagged in the audit timeline.
- **P11-US11**: As a security administrator, I can configure a privileged-access-workstation policy: certain actions require the request to come from an approved client certificate.
- **P11-US12**: As a system, AI suggestions cannot authorize; the suggestion is presented to a human who authorizes as themselves; the human's identity, not the AI's, is on the audit event.

## 5. Cross-cuts

- **P01 (Case)**: every case read/write checks tenant + role + field.
- **P03 (Document)**: every document access checks tenant + role + document-level permission.
- **P08 (Records)**: legal hold, retention, and disposition are gated by P11.
- **P09 (Audit)**: every authorization decision is an audit event.
- **P13 (API)**: every API endpoint enforces P11.

## 6. Acceptance scenarios (representative)

- K1: A user in tenant A attempts to read a case in tenant B by passing the case id. The system returns `forbidden`; the cross-tenant attempt is logged.
- K2: A user without `case.read` permission attempts to call `GET /cases/{id}`. The system returns `forbidden`; the deny is logged.
- K3: A user without field-level permission requests a case summary. The sensitive field is omitted from the response; the response itself is `200 OK` with the redacted payload.
- K4: An actor attempts to approve a decision they themselves created. The system returns `forbidden` with `separation_of_duties_violation`.
- K5: A developer starts the app with `APP_ENV=prod` and the dev identity provider is selected. The app refuses to start (fail-closed).
- K6: A header-based identity attempt is made against a non-local environment. The request is rejected with `unauthorized`; the attempt is logged.

## 7. Planned slices

| Slice | Status | Notes |
|---|---|---|
| `001-case-management-platform` | Active | First slice ships the `IdentityProvider` port, the `UserContext`, the `Policy` registry (basic), and tenant isolation in every repository. UI hiding is NOT relied on. The full action × role matrix is in the spec as a reference; only the P1 actions are wired in the first slice. |
| `002-leftovers` Run A | Draft | Role administration operable (FM-048 / P11-US4) + sensitive-field redaction boundary (FM-011 / P11-US6). See `specs/002-leftovers/architecture-runA.md`, `docs/decisions/ADR-036-runA-role-model.md`, `docs/decisions/ADR-037-runA-redaction-boundary.md`. |
| `002-leftovers` Run B | Draft | SoD enforcement on consequential transitions (FM-017 / P11-US5: originator self-approval refused, `403`) + authorize-before-execute on every search path with redacted snippets (FM-031/032/033). See `specs/002-leftovers/architecture-runB.md §3`. |
| `002-leftovers` Run C | Draft | Document-protection gate (FM-025 / P11-US7): restriction threshold, redacted-original duality (original always under the stricter right), filing lock; through-bundle-only grants + digest-stored bearer links with metadata-free denials. See `specs/002-leftovers/architecture-runC.md`, `docs/decisions/ADR-041-runC-rendition-store-adapter.md`, `docs/decisions/ADR-042-runC-bundle-permission-model.md`. |
| Future slice 2 | Planned | Full action × role matrix, field-level redaction, separation of duties, break-glass. |
| Future slice 3 | Planned | ABAC, AI boundary contract enforcement, advanced session policies. |

## 8. Open questions

- Q1: Where do roles come from — local config, an IdP claim, or both? (Current intent: roles are derived from IdP claims in production; v1 ships with seeded roles in the local dev identity.)
- Q2: Can a user belong to multiple tenants? (Current intent: yes in v1; the `UserContext` carries one `tenant_id` at a time and cross-tenant access requires explicit switching with audit.)
- Q3: Is "deny by default" a policy value or the absence of a policy? (Current intent: absence of a published policy for an action is deny.)
