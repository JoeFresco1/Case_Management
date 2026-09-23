"""create audit_events and outbox tables with append-only enforcement

This migration ships the canonical append-only audit table and the
durable outbox table that backs every domain-event fan-out
(AGENTS.md §12, plan §4.R12, FM-003).

# Append-only enforcement (Constitution IV / XVIII)

The audit table is append-only under ALL roles — including the
migration role. Enforcement lives at four layers (plan §4.R12):

1. **Database trigger**: this migration installs a ``BEFORE UPDATE``
   and ``BEFORE DELETE`` trigger on ``audit_events`` that aborts the
   statement. The trigger body is dialect-aware: SQLite (test
   profile) uses ``RAISE(ABORT, ...)``; PostgreSQL (local) uses
   ``RAISE EXCEPTION`` inside a PL/pgSQL function. Both are installed
   unconditionally; production mirrors this migration verbatim.
2. **ORM listener**: ``app.infrastructure.persistence.audit_events``
   hooks SQLAlchemy ``before_update`` / ``before_delete`` and raises
   ``AuditEventImmutable`` before the row is flushed. The listener is
   wired in ``app/db.py``.
3. **Application**: there is no application path that issues UPDATE
   or DELETE against ``audit_events``. Correcting history means
   appending a new ``AuditCorrectionRecorded`` row.
4. **Migrations**: forward-only. No future migration may add a
   downgrade step that mutates historical rows.

# Portability (AGENTS.md §4.R1, plan §3, plan §4.R1)

All column types are SQLAlchemy generic types (``Uuid``, ``JSON``,
``DateTime(timezone=True)``, ``Text``, ``String``, ``Integer``). No
``JSONB``, no dialect-specific UUID syntax. Identifier defaults are
**application-side** (``uuid4()``) — the migration never calls
``gen_random_uuid()``. This keeps the migration identical on sqlite
(test) and PostgreSQL (local dev).

The trigger body is the only dialect-specific SQL in this migration.
It is annotated with ``-- PORTABILITY: ...`` comments and is
short-circuited via ``op.get_bind().dialect.name`` so the migration
emits exactly the right trigger per dialect.

# Reversibility

``downgrade`` drops the triggers, the indexes, and the tables. Schema
changes to existing rows must use compensating audit events at the
application layer (Constitution IV / XVIII) — never raw SQL against
``audit_events``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_create_audit_outbox"
down_revision: str | None = "0001_create_tenants_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Trigger bodies — dialect-aware
# ---------------------------------------------------------------------------


# Why these strings: the migration must work on sqlite (test profile,
# AGENTS.md §4.R1) AND on PostgreSQL (local dev). The two dialects
# share no trigger syntax, so we render the right DDL per dialect at
# runtime.
#
# Why an explicit function for PostgreSQL: PG triggers cannot ``RAISE``
# directly; they must call a stored function. The function is created
# in the same transaction as the trigger so they are dropped together.
# The function name embeds the revision id to avoid collisions with
# other migrations / tenants.
_PG_FUNCTION_NAME = "audit_events_reject_mutation_0002"
_PG_TRIGGER_UPDATE_NAME = "trg_audit_events_no_update_0002"
_PG_TRIGGER_DELETE_NAME = "trg_audit_events_no_delete_0002"
_SQLITE_TRIGGER_UPDATE_NAME = "trg_audit_events_no_update_0002"
_SQLITE_TRIGGER_DELETE_NAME = "trg_audit_events_no_delete_0002"

# Use a sentinel error code that maps to IntegrityError on both
# dialects — the application can detect "append-only violation" without
# parsing message strings.
_ERROR_CODE = "AUDIT_EVENTS_APPEND_ONLY"


def _install_triggers_sqlite() -> None:
    """Install the SQLite trigger pair on ``audit_events``.

    SQLite's ``RAISE(ABORT, ...)`` raises an ``IntegrityError`` in
    SQLAlchemy — the test harness can assert on that.
    """
    # portability: SQLite trigger syntax is the local-test equivalent of
    # the PostgreSQL trigger function below; both enforce append-only rows.
    # BEFORE UPDATE: any UPDATE is rejected.
    op.execute(
        f"""
        CREATE TRIGGER {_SQLITE_TRIGGER_UPDATE_NAME}
        BEFORE UPDATE ON audit_events
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, '{_ERROR_CODE}: audit_events are append-only');
        END
        """
    )
    # BEFORE DELETE: any DELETE is rejected.
    op.execute(
        f"""
        CREATE TRIGGER {_SQLITE_TRIGGER_DELETE_NAME}
        BEFORE DELETE ON audit_events
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, '{_ERROR_CODE}: audit_events are append-only');
        END
        """
    )


def _install_triggers_postgresql() -> None:
    """Install the PostgreSQL trigger pair on ``audit_events``.

    PG triggers must call a stored function. The function raises
    ``EXCEPTION`` which surfaces as ``IntegrityError`` in SQLAlchemy —
    the same observable behavior as the sqlite path.
    """
    # portability: PostgreSQL requires a trigger function; the SQLite path
    # above uses the dialect's native RAISE expression instead.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_PG_FUNCTION_NAME}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '{_ERROR_CODE}: audit_events are append-only'
                USING ERRCODE = 'check_violation';
            RETURN NULL;
        END;
        $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_PG_TRIGGER_UPDATE_NAME}
        BEFORE UPDATE ON audit_events
        FOR EACH ROW
        EXECUTE FUNCTION {_PG_FUNCTION_NAME}();
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_PG_TRIGGER_DELETE_NAME}
        BEFORE DELETE ON audit_events
        FOR EACH ROW
        EXECUTE FUNCTION {_PG_FUNCTION_NAME}();
        """
    )


def _drop_triggers_sqlite() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_SQLITE_TRIGGER_UPDATE_NAME}")
    op.execute(f"DROP TRIGGER IF EXISTS {_SQLITE_TRIGGER_DELETE_NAME}")


def _drop_triggers_postgresql() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_PG_TRIGGER_UPDATE_NAME} ON audit_events")
    op.execute(f"DROP TRIGGER IF EXISTS {_PG_TRIGGER_DELETE_NAME} ON audit_events")
    op.execute(f"DROP FUNCTION IF EXISTS {_PG_FUNCTION_NAME}()")


# ---------------------------------------------------------------------------
# upgrade / downgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ---- audit_events ----------------------------------------------------
    # Why a B-tree on (tenant_id, case_id): the per-case timeline query
    # (FM-043) filters by tenant and case id; the composite index lets
    # the planner serve the WHERE without a table scan.
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id", ondelete="CASCADE", name="fk_audit_events_tenant_id_tenants"
            ),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("causation_id", sa.String(length=64), nullable=True),
        # Why JSON (generic) and not JSONB: AGENTS.md §4.R1 / plan
        # §4.R1 forbids JSONB because the binary type is not portable
        # to Azure SQL. JSON gives us the same logical surface with
        # portable parsing.
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )
    op.create_index(
        "ix_audit_events_tenant_case",
        "audit_events",
        ["tenant_id", "case_id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_tenant_correlation",
        "audit_events",
        ["tenant_id", "correlation_id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_tenant_occurred_at",
        "audit_events",
        ["tenant_id", "occurred_at"],
        unique=False,
    )

    # ---- outbox ----------------------------------------------------------
    # Why a B-tree on (tenant_id, dispatched_at, id): the outbox
    # dispatcher queries "rows where dispatched_at IS NULL and
    # attempt_count < N, oldest first" — the composite index is the
    # ordered index it scans.
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE", name="fk_outbox_tenant_id_tenants"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "dispatched_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_outbox"),
    )
    op.create_index(
        "ix_outbox_tenant_dispatched",
        "outbox",
        ["tenant_id", "dispatched_at"],
        unique=False,
    )

    # ---- Append-only triggers --------------------------------------------
    # Why after the tables: triggers reference the table by name and
    # must exist *after* the table is created; sqlite raises if the
    # table does not exist when the trigger is installed.
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        _install_triggers_sqlite()
    elif dialect == "postgresql":
        _install_triggers_postgresql()
    else:
        raise RuntimeError(
            f"audit append-only triggers are not implemented for dialect {dialect!r}"
        )


def downgrade() -> None:
    # Drop triggers first so the tables can be dropped without
    # constraints tripping.
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        _drop_triggers_sqlite()
    elif dialect == "postgresql":
        _drop_triggers_postgresql()
    else:
        raise RuntimeError(
            f"audit append-only triggers are not implemented for dialect {dialect!r}"
        )

    op.drop_index("ix_outbox_tenant_dispatched", table_name="outbox")
    op.drop_table("outbox")

    op.drop_index("ix_audit_events_tenant_occurred_at", table_name="audit_events")
    op.drop_index("ix_audit_events_tenant_correlation", table_name="audit_events")
    op.drop_index("ix_audit_events_tenant_case", table_name="audit_events")
    op.drop_table("audit_events")
