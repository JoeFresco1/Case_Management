# ADR-001: Portable SQLAlchemy generic types in domain models

- Status: Accepted (2026-08-31)
- Scope: `backend/app/infrastructure/persistence/models.py`, every Alembic
  migration, and the canonical ORM column vocabulary.
- Supersedes: nothing.
- Related: AGENTS.md R1, plan.md:288-303, constitution §VI.

## Context

The first slice targets local PostgreSQL 16 today and Azure SQL in
production tomorrow. The plan document treats database portability as
an architectural objective, not an assumption of dialect equivalence
(plan.md:288-303). A naïve port that assumes dialect equality will
silently break on UUID handling, JSON semantics, indexing, identity
behavior, datetime/timezone semantics, transaction isolation, SQL
syntax, and migration tooling. PostgreSQL-specific features
(`JSONB`, `GIN`, `ILIKE`, `tsvector`, `gen_random_uuid()`, array types,
range types) are forbidden in the domain and application layers and
must not leak into migrations.

## Options considered

1. **Dialects-as-features: pick one dialect per environment.**
   - Pros: lets us use the most expressive features of each backend.
   - Cons: forces a rewrite of every repository at port time; risks
     subtle semantic drift (e.g., `JSONB` containment vs `JSON` LIKE).
     Violates Constitution VI ("architect for Azure today").

2. **Generic SQLAlchemy types + portable SQL only.**
   - Pros: same code runs against PostgreSQL (asyncpg) and Azure SQL
     (aioodbc) without dialect-specific branches. Portable
     `LIKE`-based search starts simple; full-text search becomes an
     adapter concern (PostgreSQL FTS, Azure Cognitive Search) when
     needed. Smaller surface to test.
   - Cons: we give up JSONB containment and GIN indexing in v1. If a
     query genuinely needs them, the need belongs in the repository
     port, expressed portably (or behind a feature flag) per R3.

3. **Per-feature adapter with seam at the column type.**
   - Same as #2 for v1, but with a documented seam for future dialect
     features. This is what we pick.

## Decision

We adopt **option 2** with explicit seams for future work. The
canonical ORM vocabulary is:

- `sqlalchemy.Uuid` (not `sqlalchemy.dialects.postgresql.UUID`).
- `sqlalchemy.JSON` (not `JSONB`).
- `sqlalchemy.DateTime(timezone=True)` (always timezone-aware).
- `sqlalchemy.Text`, `sqlalchemy.String(length)`, `sqlalchemy.Integer`,
  `sqlalchemy.Boolean`.
- `sqlalchemy.ForeignKey`, `sqlalchemy.Index`.

Identifier values come from the application-side `uuid4()` default;
migrations do **not** rely on `gen_random_uuid()` so the same DDL runs
on sqlite (test profile) and on either production backend. The
infrastructure search vocabulary starts with portable `LIKE`; a real
search engine is an adapter concern, never a domain concern.

A grep-based CI check enforces AC-009 / R1: no `JSONB`, `GIN`,
`ILIKE`, `gen_random_uuid()`, `tsvector` in
`backend/app/domain/**` or `backend/app/application/**`. The check is
wired in a follow-up task; the same string is asserted by
`backend/tests/foundation/test_migration_portability.py`.

Naming convention is centralized at `app/db.py:46`
(`pk_*`, `fk_*`, `ix_*`, `uq_*`, `ck_*`) and applied via
`MetaData(naming_convention=...)`. Every migration is reversible
(`upgrade` and `downgrade` paths). Raw dialect SQL is allowed in
migrations only with a portability comment.

## Consequences

- Every table carries `tenant_id` + `created_at`/`updated_at` (UTC).
  Tenant isolation is enforced by an explicit `WHERE tenant_id =
  :tenant_id` in every repository method. A SQLAlchemy `before_compile`
  listener is **defense-in-depth only** (never the sole mechanism).
- Search starts with portable `LIKE`. JSONB containment operators are
  not available; the first slice uses JSON columns for document
  metadata and `LIKE`-based predicates for case data.
- The first migration (`0001_create_tenants_users`) creates **only**
  the `tenants` and `users` tables. Each subsequent migration owns its
  own tables and ships alongside the task that introduces the domain
  logic for them (T011, T024, T025, T049, T058, T063, T069, ...).
- A portability unit test (tests/foundation/test_migration_portability.py)
  asserts: the upgrade creates the expected tables; the downgrade drops
  them; the migration source contains no forbidden tokens; the model
  layer contains no forbidden tokens. The same regex is the seed for a
  CI grep check (added in a follow-up task).

## Azure seam

When the Azure SQL adapter lands, no ORM code changes. The
configuration file (`app/config.py`) picks the engine URL; the column
vocabulary stays the same. Differences in dialect behavior
(`UUID`/`uniqueidentifier`, `JSON`/`NVARCHAR(MAX)`) are handled by
SQLAlchemy's dialect layer; tests in the sqlite profile cover the
generic surface so we never depend on dialect-specific behavior.