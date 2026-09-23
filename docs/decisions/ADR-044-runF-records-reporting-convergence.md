# ADR-044: Run F records/reporting convergence (FM-039/040/041/042/044/045/051/053/054; FM-046 stays Gated)

- Status: Accepted (2026-09-21)
- Scope: FM-039, FM-040, FM-041, FM-042, FM-044, FM-045, FM-051, FM-053, FM-054 convergence for Run F (T6).
- Related: FM-039..FM-046, FM-051/053/054, ADR-009 (SIEM gate), P08/pillar.md, P09/pillar.md, P12/pillar.md.

## Context

F-2 through F-7 built records governance incrementally: legal-hold lifecycle with
SQL durability, retention/disposition rules with clock pause-on-hold, durable
`retention_policies` + `disposition_requests` tables (migration `0031`),
operational reporting services, chain-of-custody evidence with governed export,
and the records/reporting API + UI surface. Four classification questions stayed
open at convergence: (1) does a hold block disposition of evidence too, or just
the case; (2) does the retention clock pause or reset on hold (spec §9 open
question #5); (3) is disposition review a ticket object or a single-step
decision; (4) which Run F surfaces stay in-memory placeholders vs durable rows.

## Decision

1. **Holds block disposition of case + documents + audit evidence (FM-039).**
   `LegalHold` refusal (`REFUSED_HOLD_CODE="refused_hold"`, `domain/legal_hold.py:57`)
   is consulted by the disposition executor, not just case close: a held execution
   returns `outcome="refused_hold"` with the policy version cited, leaves the
   request `pending`, and writes no audit row (no partial delete, no phantom trail).
2. **Retention clock pauses on hold, resumes on release (FM-040).** Spec §9 open
   question #5 is settled as pause-and-resume: `due` shifts by hold duration on
   release (`test_clock_pause__released_hold__shifts_due_by_hold_duration`); the
   clock never resets, and a held case is never eligible while held.
3. **Disposition review is single-step approve/reject, not a ticket system
   (FM-040).** `PENDING → APPROVED|REJECTED → EXECUTED`; reject requires a reason;
   double-approve / approve-after-reject / decided-row updates are `Conflict` with
   a single audit row. An approval-request ticket object would be a new feature,
   not FM-040 (same rationale as the FM-017 decision to skip ticket objects).
4. **Published retention versions are immutable; placeholders are declared design
   (FM-040/041/053).** Publishing v2 supersedes without mutating v1; published
   rows are frozen against mutation; retire is irreversible. Archival / freeze /
   orphan rows and FM-053 report definitions remain in-memory seam placeholders by
   design (migration `0031` scope note documents why no DDL is emitted for them);
   evidence bundles are computed archives whose export is itself an
   `AuditExported` row. These are not gaps — durability lands when a pilot needs
   queryable history, behind the same Protocols.
5. **FM-046 stays Gated (no authorization change in Run F).** Signed-batch
   mechanics are operable (`InMemorySignedBatchRepository`,
   `signed_batch_store: local-in-memory`) but `audit.stream` remains ungranted by
   default (deny-by-default per ADR-009); no SIEM target is selected and no stream
   write is possible without an explicit grant. Run F claims mechanics only.

## Consequences

- T6 acceptance holds: held disposition → `refused_hold` with policy version
  cited; every export/disposition writes its own audit event while originals stay
  immutable; dashboards carry source-time + authorization scope per KPI.
- Known debt carried (not Run F gaps): full
  `tests/foundation/test_baseline_migrations.py` exceeds the 300 s tool timeout on
  this host (targeted `0031` upgrade/downgrade SQL + parity-column tests pass; run
  the full replay in CI); repo-wide `ruff check` / `ruff format --check` failures
  are confined to `app/domain/form.py` (Run E debt per ADR-043 — Run F adds zero
  new findings).
- Gate debt is explicit in `architecture-runF.md §8`: blockers are reported with
  exact scope, none hidden.

## Azure-seam note

No seam added or flipped. Records reuse seams 1–3 (SQLAlchemy repos, FS
`DocumentStore`, outbox + in-proc bus); the SIEM/signed-batch surface keeps its
`# INTEGRATION_SEAM: Azure SIEM — Gated` marker (`siem_stream.py:3`,
`siem.py:3`, `models.py:372`) until a concrete SIEM target and key-management
decision arrive. The deterministic core stays LLM/SDK-free per Constitution XIV —
no ranker or assistant participates in holds, disposition, export, or reporting
decisions.
