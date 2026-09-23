# ADR-018: Migration portability contract and lint

- Status: Accepted (2026-09-04)
- Scope: `backend/alembic/env.py`, `scripts/check_portable_sql.sh`,
  `backend/tests/foundation/test_portable_sql.py`, every migration
  under `backend/alembic/versions/`.
- Supersedes: nothing.
- Related: AGENTS.md R1 / R2, ADR-001, plan.md:288-303, ADR-021.

## Context

ADR-001 commits the codebase to SQLAlchemy generic types and portable
SQL, but the contract stops at the model layer. The first slice now
ships 14 migrations whose upgrade paths touch SQLite (test profile) and
PostgreSQL (local docker). The lint that backs ADR-001 lives in
`backend/tests/foundation/test_migration_portability.py` and is
executed only when pytest runs — which means a developer who shells
out to `uv run alembic upgrade head` against a forgotten `DATABASE_URL`
sees the bug only after the schema has already drifted.

Two gaps remain:

1. **No static CI gate.** The forbidden-token regexes live in Python
   and only run inside pytest. CI needs a stdlib-only shell gate so
   the check fires before any language toolchain is installed (mirrors
   `scripts/check_architecture_md.py` and `scripts/ci_synthetic_data_check.py`).
2. **Alembic has no enforcement.** Nothing prevents a developer from
   merging `0007_create_roles.py`'s successor that ships `op.execute("
   CREATE INDEX ... USING GIN (...)")` and only runs the unit tests.

The first slice must be able to demonstrate that
`alembic upgrade head` followed by `downgrade -1` and another
`upgrade head` succeeds on the sqlite test profile. The portable-SQL
contract is the boundary that makes this safe.

## Options considered

1. **Trust pytest + ADR-001.** Keep the existing in-test scanner.
   - Pros: zero new code; already in place.
   - Cons: developer-run `alembic upgrade` is unenforced; CI on a
     fresh checkout without a virtualenv is unenforced; reviewers
     cannot see the rule at a glance.

2. **Add a bash CI gate + alembic env-time lint.** Mirror the
   architecture.md gate (`scripts/check_architecture_md.py`).
   - Pros: works before any toolchain is installed; runs in CI and on
     developer machines; lints the migration source before alembic
     applies it, so a forbidden token fails fast with the file name
     and revision id.
   - Cons: two implementations of the same regex must be kept in
     sync; the escape-hatch marker (`# portability:`) has to be
     honoured by both.

3. **Make alembic refuse to start without a marker file.** Strict.
   - Pros: simple contract.
   - Cons: forces every migration to declare a marker even when no
     dialect-specific SQL is present, which adds noise without
     value. Option 2 keeps the marker opt-in.

## Decision

Adopt **option 2**. The contract is:

- **Forbidden tokens everywhere.** `JSONB`, `GIN`, `ILIKE`,
  `tsvector`, `gen_random_uuid`, `ARRAY[...]`, and `<type>RANGE` are
  banned in `backend/app/{domain,application,interfaces}` with no
  exception.
- **Forbidden tokens in migrations** are allowed **only** when the
  same migration file carries a `# portability:` comment naming the
  dialect-specific construct. The comment is the seam an Azure-SQL
  adapter would consult when generating a dialect-neutral rewrite.
- **Every migration defines both `upgrade()` and `downgrade()`**.
  The first-slice round-trip smoke (upgrade → downgrade → upgrade
  on sqlite) is the proof; the static test enforces it.
- **Alembic env registers the lint.** `backend/alembic/env.py`
  defines `FORBIDDEN_TOKENS`, `lint_migration_source(source)`, and
  `run_lint_on_pending(script_directory)`. Both `run_migrations_online`
  and `run_migrations_offline` invoke the lint before handing the
  connection to alembic.
- **Bash gate.** `scripts/check_portable_sql.sh` is the CI-level
  gate. It greps the same tokens, asserts the
  upgrade/downgrade function presence, and exits 1 on any violation.

The escape hatch `SKIP_PORTABLE_SQL_LINT=1` is provided for the
sqlite downgrade smoke harness so an operator can bypass the lint
when they have already proven portability separately. The
environment variable is opt-in and documented in the env module
docstring.

## Consequences

- The forbidden-token regex now lives in three places
  (`env.py`, `scripts/check_portable_sql.sh`, and the pytest
  module). Drift between them is the primary maintenance cost.
  The pytest module asserts both the env.py and the bash gate
  still reference every token, so a silent drop in either is
  caught locally.
- Migrations that genuinely need PostgreSQL-only SQL must add a
  `# portability:` comment in their header. The comment is the
  contract exception; a missing comment fails the bash gate and
  the alembic env lint.
- `alembic upgrade head` against a sqlite test profile now runs
  the lint, so the static test and the runtime check share the
  same source of truth.
- The synthetic-data gate already enforces `example.test`-only
  fixtures; the lint doesn't change that, it just adds a new
  family of invariants.

## Azure seam

When the Azure SQL adapter lands, it consumes the `# portability:`
markers to identify migrations whose upgrade paths need rewriting.
Migrations without the marker are guaranteed portable by the lint;
the adapter's job is to translate the marker-tagged dialect blocks
into a T-SQL equivalent (or to skip them with an explicit runtime
fallback). The lint itself does not need to change — it is
dialect-neutral on purpose.

The Azure SQL placeholder
(`backend/app/infrastructure/persistence/azure_sql_placeholder/`)
will read this ADR plus `docs/integrations/seams.md` to know which
marker blocks to handle.