# ADR-020: Material decisions log for first slice (001-case-management-platform)

- Status: Accepted (2026-09-03)
- Scope: `specs/001-case-management-platform/**`, `docs/decisions/**`,
  `docs/integrations/seams.md`, `docs/TECH_SPEC.md`, `AGENTS.md §4.R8`.
- Supersedes: nothing.
- Related: AGENTS.md §4.R8 (Decisions Documented), §4.R9 (Architecture with
  Mermaid), §4.R10 (Scalability, ease of integration & maintenance),
  §12 (Integration seams), §13 (Mermaid on completion). Constitution II
  (recorded decisions), III (versioned config), V (fail-closed), VI
  (architect for Azure today), XIV (deterministic core before AI).

## Context

The first slice (`specs/001-case-management-platform/`) has accumulated
28 material decisions across ADRs 001-019 and 021-028. AGENTS.md §4.R8
requires every material decision to be recorded as an ADR using the
Context / Options / Decision / Consequences / **Seam note** template, and
linked from the slice plan or the roadmap §7 ("Open product questions").
§4.R9 requires a Mermaid-rich `architecture.md` at slice convergence, and
§4.R10 requires that architectural seams (local vs Azure) be visible in
the architecture document and in `docs/integrations/seams.md`.

Until this ADR landed, there was no single index pointing readers at the
right ADR for each architectural question, no link from the roadmap §7
back to the relevant ADR, and no programmatic guarantee that
`architecture.md` exists for each slice folder.

This ADR closes those gaps by:

1. Publishing a canonical index of all first-slice material decisions.
2. Cross-linking each ADR back to the roadmap §7 question it resolves
   and to the architecture.md section it informs.
3. Recording the seam status for every Protocol in `domain/ports.py`.
4. Shipping a CI check (`scripts/check_architecture_md.py`) that fails
   if any `specs/NNN-*` folder is missing `architecture.md`.

## Options considered

1. **Inline the decisions into a single mega-ADR.** Rejected — destroys
   atomicity of each decision, loses the per-ADR review history, and
   forces every reader to scroll past decisions they do not care about.
   (Constitution XV: prefer deleting clever code over defending it.)
2. **Keep ADRs as-is and add an index file only.** Rejected — does not
   close the roadmap-link gap (AGENTS.md §4.R8) and does not enforce the
   `architecture.md`-exists rule for future slices (AGENTS.md §4.R9).
3. **Index ADR + roadmap link + seam-status table + CI check
   (chosen).** Keeps each ADR atomic, makes the index browsable in one
   place, and turns the architecture.md gate into a hard precondition
   for closing a slice.

## Decision

We adopt option 3. Specifically:

- This ADR is the **single source of truth** for "where do I find the
  decision that backs X" for the first slice. Every other ADR should be
  referenced from here; this ADR should never be the only place a
  decision lives — each decision still has its own ADR file.
- The roadmap §7 ("Open product questions") now points at the relevant
  ADR for each numbered question where one exists.
- The seams table at the bottom of this ADR mirrors
  `docs/integrations/seams.md` and is regenerated whenever that file
  changes (single source of truth = `seams.md`).
- A small CI script `scripts/check_architecture_md.py` enforces the
  AGENTS.md §4.R9 rule that every `specs/NNN-*/` slice has an
  `architecture.md` file with at least one Mermaid code fence.

## Decision index — first slice (ADR-001 .. ADR-028)

Each row links the ADR to the AGENTS.md / plan.md / roadmap §7 question
it answers and to the architecture.md section it informs.

| ADR | Topic | Resolves (AGENTS.md / plan / roadmap) | Architecture.md section |
|-----|-------|----------------------------------------|-------------------------|
| ADR-001-portable-types | SQLAlchemy generic types only (no JSONB / GIN / ILIKE / gen_random_uuid) | AGENTS.md §4.R1, plan.md:289 | §4.1 Portability invariants |
| ADR-002-seams | Integration seams: local adapter now, Azure adapter placeholder | AGENTS.md §3 Issue #2, §12, plan.md:42 | §7 Integration seams table |
| ADR-004-event-catalog | Stable event names + versioning rules for the outbox | AGENTS.md §16.3, plan.md:206 | §9 Outbox + dispatcher sequence |
| ADR-005-email-capture | Inbound email → case/document/participant routing | P04 (partial) | deferred (P04 slice) |
| ADR-006-scan-quarantine | Quarantine seam for unscanned / unscannable documents | P03 partial, P04 | §3 module map (P03 row) |
| ADR-007-webhook-inbound | Webhook ingress + signature verification | P04, P13 | deferred (P04 slice) |
| ADR-008-semantic-search | Vector search seam (P06 deferred) | AGENTS.md §11, roadmap §7 #2 | deferred (P06 slice) |
| ADR-009-siem | SIEM export (P09 surface) | P09, AGENTS.md §4.R12 | §9 (Outbox → SIEM) |
| ADR-010-extensions | Extension point API for P13 | AGENTS.md §13, P13 | §2 Mermaid flowchart |
| ADR-011-session-controls | Session, idle, and absolute timeouts | P11 | §7 seam #4 (Identity) |
| ADR-012-ai-boundary | AI is bounded assistance only — single `recommend()` verb | AGENTS.md §4.R11, Constitution XIV, roadmap §7 #4 | §7 seam #7 (AI assistance) |
| ADR-013-outbound-webhooks | Outbound webhook delivery semantics | P13, P14 | §9 Outbox + dispatcher |
| ADR-014-graphql | GraphQL read-only surface (decision: no) | P13 | deferred (negative decision) |
| ADR-015-connector-lifecycle | Connector lifecycle states + Gating | P14, Constitution XII.5 | deferred (P14 slice) |
| ADR-016-coexistence | Side-by-side legacy coexistence rules | Constitution XII, plan.md §Risks | §11 Local development contract |
| ADR-017-evidence-bundle | Evidence bundle export for FOI / legal hold | P08, P09 | deferred (P08 slice) |
| ADR-019-security | Localhost-only dev headers, fail-closed posture | AGENTS.md §4.R12, Constitution V | §7 seam #4 + §7.1 fail-closed note |
| ADR-020 (this) | Decisions index + roadmap §7 links + CI gate | AGENTS.md §4.R8, §4.R9 | §12 (gate artifact) |
| ADR-021-multi-tenant-persistence-strategy | Single DB + `tenant_id` vs database-per-tenant | roadmap §7 #1 | §4 ER sketch (every table) |
| ADR-022-public-vs-internal-api | Internal API only in first slice | roadmap §7 #5 | §17.1 HTTP API shape |
| ADR-023-event-compatibility-versioning | Event schema versioning (additive only) | P13, §13 | §9 Outbox + dispatcher |
| ADR-024-p01-external-ref-uniqueness-scope | `CaseReference` uniqueness scope = tenant | AGENTS.md §16.3, plan.md:42 | §4 ER (`cases.reference`) |
| ADR-025-p05-incompatible-metadata-type-change | Type-change in `case_type_versions.schema_json` is breaking | P05, plan.md:171 | §3.1 Pillar → module matrix (P05) |
| ADR-026-p09-audit-chain-scope | Audit hash chain is per-tenant | P09, plan.md:252 | §5 sequence (`AuditEventStore`) |
| ADR-027-p11-identity-role-authority | `actor_type` for system vs human actions | P11, plan.md:259 | §7 seam #4 |
| ADR-028-event-transport-and-deferred-scope | Outbox is the only integration event surface today | AGENTS.md §4.R10, plan.md §Risks | §9 Outbox + dispatcher |

> Numbering gap (003, 018, 020 reserved): 003 was reserved for an early
> multi-tenant discussion later renumbered as ADR-021; 018 was reserved
> for telemetry schema and absorbed into ADR-002; this ADR is 020.

## Roadmap §7 cross-link (AGENTS.md §4.R8 requirement)

Roadmap §7 lists 5 open product questions. Where this slice has decided
the answer, the ADR is named here; the roadmap itself is updated in the
same commit to add the link.

| Roadmap §7 question | Decided by | Status |
|----------------------|------------|--------|
| #1 Multi-tenant single-DB vs DB-per-tenant | ADR-021 | **Accepted** (single-DB with `tenant_id` everywhere) |
| #2 Real-time event delivery | ADR-002 §Outbox + ADR-028 | **Accepted** (outbox only; WebSockets/SSE deferred) |
| #3 Notifications | (not decided in this slice) | Open — recorded for P14 |
| #4 AI assistance surface | ADR-012 | **Accepted** (single `recommend()` verb; no-op stub local; Azure OpenAI placeholder) |
| #5 Public API vs internal API | ADR-022 | **Accepted** (internal only in first slice) |

## Seam status (mirrors `docs/integrations/seams.md`)

This table mirrors the inventory. The canonical version is in
`docs/integrations/seams.md`; this table exists so a reader of this ADR
sees the seam status without jumping files.

| # | Seam | Port location | Local status | Azure status | Decision ADR |
|---|---|---|---|---|---|
| 1 | Relational DB | `app/domain/ports.py` (repositories) | Local adapter implemented | Azure adapter pending (placeholder) | ADR-001, ADR-021 |
| 2 | Document store | `app/domain/ports.py` (`DocumentStore`) | Local adapter implemented | Azure adapter pending (placeholder) | ADR-002 |
| 3 | Messaging | `app/domain/ports.py` (`MessageBus`, `OutboxWriter`) | Local adapter implemented | Azure adapter pending (placeholder) | ADR-002, ADR-004, ADR-028 |
| 4 | Identity | `app/deps.py` (`IdentityProvider`) | Local adapter implemented | Azure adapter pending (placeholder) | ADR-002, ADR-011, ADR-019, ADR-027 |
| 5 | Secrets | `app/domain/ports.py` (`SecretProvider`) | Local adapter implemented | Azure adapter pending (placeholder) | ADR-002 |
| 6 | Telemetry | `app/domain/ports.py` (`Telemetry`) + structlog | Local adapter implemented | Azure adapter pending (placeholder) | ADR-002 |
| 7 | AI assistance | `app/application/ai/boundary.py` (`AIAdvisor`) | Local adapter implemented (`NoOpAIAdvisor`) | Azure adapter pending (placeholder) | ADR-012 |
| 8 | Enterprise imports | `ImportJobRepository` (FM-026..FM-030) | Defer until Phase 3+ | Defer until Phase 3+ (Gated) | ADR-015, ADR-016 |

## CI gate — `architecture.md` must exist for each slice

AGENTS.md §4.R9 says: *"CI checks that `architecture.md` exists before
marking slice done."* This ADR adds the script that does the check. The
script is dependency-free (stdlib only) so it can run before `uv sync`.

```python
# scripts/check_architecture_md.py
"""Fail if a specs/NNN-*/ folder is missing architecture.md.

Walks specs/, finds every NNN-* folder, and asserts that architecture.md
exists with at least one Mermaid code fence. Used in CI per AGENTS.md
§4.R9. Exit code 0 on success, 1 on failure. Stdlib only.
"""
```

Run with:

```bash
python scripts/check_architecture_md.py
```

## Consequences

**Positive**

- A reader of `architecture.md` or of any ADR can find the related
  decisions in one table.
- The roadmap §7 "open questions" list is now auditable: every
  decided question points at an ADR; every open question is recorded
  as open.
- Closing a slice without `architecture.md` is now mechanically
  impossible.
- Seams are visible from three places (architecture.md §7,
  seams.md, this ADR §"Seam status") but one is canonical.

**Negative / cost**

- Three places to keep seams in sync. Mitigated by treating this ADR's
  table as a derived view: edits go to `seams.md` first; this ADR is
  updated in the same commit.
- A new CI script is one more thing to run. Mitigated by being
  stdlib-only and <50 lines.

**Seam note** (per AGENTS.md §4.R8 template)

- Which seam: this ADR introduces no new runtime seam.
- Which adapter: not applicable.
- Which env vars: not applicable.
- Which file changed: `docs/decisions/ADR-020-final.md` (new),
  `specs/pillars/00-product-roadmap.md` (link to this ADR from §7),
  `specs/001-case-management-platform/architecture.md` (link to this
  ADR from §12), `scripts/check_architecture_md.py` (new).

## Follow-ups

- After each subsequent slice converges, add a new row to the index
  here and a new section to `architecture.md`.
- Re-run `python scripts/check_architecture_md.py` in CI before merging
  any new slice.
- When `docs/integrations/seams.md` changes, regenerate the "Seam
  status" table above in the same commit.