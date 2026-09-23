# ADR-042: Permissioned-bundle permission model (Run C)

- Status: Accepted (2026-09-14)
- Scope: `DocBundle` + `ShareLink` (T5-US3), `POST|GET|PATCH /bundles…`,
  `POST /bundles/{id}/links`, `POST /bundles/links/redeem|revoke`
  (FM-025-adjacent collaboration; T8 evidence bundles are separate).
- Supersedes: nothing.
- Related: AGENTS.md R12, Constitution V, FM-025 (P03-US6/US8, P11-US7),
  ADR-031 (break-glass enforcement — same denial-shape rationale),
  `specs/002-leftovers/architecture-runC.md` §2–§3,
  `app/domain/doc_bundle.py:1`, `app/application/documents/doc_ops.py:717`
  (bundles + share links), `app/interfaces/http/routers/doc_bundles.py:1`.

## Context

T5-US3 requires worker collaboration over document sets ("bundle/share
documents with independent permissions and expiry") rebuilt as modern
web UX, not cloned envelopes. The model must answer four questions:
(1) what a bundle grant confers, (2) how bearer links authorize without
a login, (3) how revocation propagates, and (4) whether bundle
membership may widen search visibility.

## Options considered

1. **Bundle grants widen the underlying document grants.**
   - Pros: one permission check everywhere (bundle member ⇒ document
     reader); simplest router code.
   - Cons: removing a member (or revoking the bundle) would have to
     recompute every affected document grant — a fan-out that cannot be
     atomic with the membership edit, leaving a window where a removed
     member still reads directly. Rejected (fail-open window).
2. **Through-bundle-only grants + bearer links, never a search scope
   (chosen).**
   - Pros: a bundle grant confers read access *through the bundle
     only* — the underlying document grants never change, so removal /
     revocation closes the path immediately with no fan-out. Bearer
     links carry only a SHA-256 digest server-side (`token_sha256`);
     the raw token exists solely in the mint response, is never logged,
     audited, or persisted, and verifies in constant time
     (`hmac.compare_digest`). Bundle liveness is checked *before* link
     liveness, so revoking the bundle implicitly kills its links.
     Content search (`application.search.document_search`) never takes
     a bundle as a corpus, so membership cannot make bytes searchable
     to a non-member. Every denial (unknown / expired / revoked /
     exhausted, cross-tenant, not-granted) carries only a safe reason
     code — no bundle name, member list, or existence signal — so a
     guessed-URL probe learns nothing (same rationale as ADR-031).
   - Cons: two read paths to enforce (direct grants + bundle path);
     bearer redemption is unauthenticated by design, so link TTL
     (5 minutes – 30 days) and `max_uses` bound exposure instead of
     identity. Accepted: TTL/uses are validated in the domain
     (`MIN_LINK_TTL`/`MAX_LINK_TTL`), and long-lived collaboration uses
     revocable per-user bundle grants instead of links.

## Decision

Option 2, as implemented:

- Roles form a hierarchy (`EDITOR` implies `VIEWER`); the bundle
  `created_by` owner holds implicit editor access so an empty grant map
  never locks the owner out. Membership is a deduplicated reference
  tuple (cap 500 — an envelope is curated, not a bulk export; bulk is
  the T8 evidence-bundle path with its own policy audit).
- `check_bundle_access` enforces cross-tenant first, revoked second,
  grant-cover third; per-document sensitivity still applies on top
  (bundle access never bypasses
  `document_protection.check_read_allowed` for restricted originals,
  and restricted members require `document.read_restricted` at bundle
  creation).
- Links: entropy from `secrets.token_urlsafe(24)` in the application
  layer; digest-only storage; `record_use` increments the count and
  denies with the bare reason once expired / revoked / exhausted;
  unknown tokens take the identical `UNKNOWN` denial path (no
  enumeration oracle). Minting requires bundle `VIEWER`; granting and
  membership edits require `EDITOR`; revocation is irreversible and
  freezes membership.
- Every mutation (`DocBundleCreated`, `DocBundleMembersChanged`,
  `DocBundleRevoked`, `ShareLinkMinted`, `ShareLinkRedeemed`,
  `ShareLinkRevoked`) appends its audit row + outbox row in the same
  transaction as the domain row (FR-L02); stores are process-local
  singletons on `app.state` (same pattern as the H0 legal-hold
  in-memory repository) behind `BundleStore`/`LinkStore` Protocols a
  SQL adapter can replace without touching the use cases.

## Consequences

- `test_doc_bundle.py` (domain: hierarchy, TTL bounds, digest
  verification, revocation order) + `test_doc_bundles_router.py`
  (HTTP: grant-gated reads, member-removal cuts access, expired link
  denies with no metadata leak, bundle content not searchable by
  non-members) pin the contract.
- T8 evidence bundles (`FM-060`) stay a separate concept: policy-
  versioned portable archives for offline/legal-hold review, not
  live collaboration grants. A bundle is not an export and an export
  is not a grant.

## Azure-seam note

No new seam. Desktop-bridge payloads and mail/file shares (T8 named
channels) reuse `ShareLink` as the expiry + auth-check kernel: the
channel adapter delivers the raw token out-of-band while this module
enforces lifetime, use-count, and revocation. No Azure SDK is touched.
