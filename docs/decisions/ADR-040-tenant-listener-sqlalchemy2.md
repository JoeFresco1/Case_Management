# ADR-040: Tenant-listener SQLAlchemy 2.x fix (gate-unblocking)

**Status**: Accepted (2026-09-13) · **Scope**: `backend/app/db.py`,
`backend/tests/security/test_tenant_listener.py` · **Runs**: Run B gate (B-8)

## Context

The full sqlite gate (`uv run pytest -q`, 1409 tests) failed 6/6 tests in
`tests/interfaces/http/test_idempotency_atomic.py` with HTTP 500 on the
first mutation — but only when run after `tests/integration`. Alone, the
file passed. The trigger: `tests/integration/test_seams.py` boots the
real app, whose startup installs the `do_orm_execute` tenant listener
(`app/db.py::install_tenant_listener`) on the ORM `Session` class
process-wide. Any later test using a tenant-pinned session (the
idempotency provider pins via `set_current_tenant`) then executed the
listener body, which called `with_loader_criteria(_criteria)` with a
single callable. SQLAlchemy 2.0 (pinned 2.0.52) requires
`with_loader_criteria(entity, where_criteria)` — the call raised
`TypeError`, uncaught by the `suppress(ArgumentError)`, and the request
500'd. No test exercised the listener-enabled path (security tests only
cover disabled), so the defect survived. In production the same 500
would fire on any tenant-pinned ORM execution with the listener on.

## Options

1. **Fix the call per-entity** (chosen): derive entities from the
   statement's `column_descriptions` and apply
   `with_loader_criteria(entity, entity.tenant_id == tenant_id)` only
   where an ORM entity with `tenant_id` is present. Core DML with no
   entity passes through untouched.
2. Disable the listener globally: loses the defense-in-depth layer
   (Constitution V / FM-002) — rejected.
3. Contain in tests only (remove the global listener around the
   idempotency suite): hides a genuine prod 500 — rejected.

## Decision

Option 1. The listener keeps its contract (best-effort, explicit
repository filters authoritative, `ArgumentError` still suppressed,
all other errors propagate) and is now covered by
`tests/security/test_tenant_listener.py` (pinned ORM select filters,
pinned Core DML passes through, unpinned select unfiltered).

## Consequences

- Full gate green: **1412 passed**, coverage **90.72%** (≥80%).
- Blast radius is nil by construction: the listener early-returns
  unless a session is pinned, and only the idempotency provider pins
  sessions today.
- Known residue (not introduced here): `reset_engine()` clears the
  `_LISTENER_INSTALLED` flag but cannot unregister the `Session`-level
  listener, so a reinstall registers a second identical callback
  (harmless — the body is idempotent). A future slice may add an
  explicit uninstall helper.

## Azure-seam note

None — portable ORM behavior, identical on sqlite and Azure SQL.
