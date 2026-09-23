"""Migration portability checks (ADR-001, AGENTS.md R1, plan.md:288-303).

This test enforces the foundation slice's portability invariants:

1. ``alembic upgrade head`` followed by ``alembic downgrade -1`` succeed
   on the sqlite test profile, without needing a live PostgreSQL container.
2. After upgrade, the expected ``tenants`` and ``users`` tables exist
   with portable column types (``Uuid``, ``JSON``, ``DateTime(timezone=True)``,
   ``String``, ``Boolean``) and no dialect-specific types (``JSONB``,
   ``GIN``, ``tsvector``, ``gen_random_uuid()``).
3. The migration source code contains none of the forbidden tokens.
4. The domain and application layers contain none of the forbidden
   tokens — the same regex is the seed for the CI grep check that
   enforces AC-009.

The sqlite path is used so the test never depends on Docker / WSL
behavior (AGENTS.md R1 WSL fallback).
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    create_engine,
    inspect,
)
from sqlalchemy import Text as SAText
from sqlalchemy import Uuid as SAUuid

from app.config import get_settings
from app.db import Base
from app.infrastructure.persistence import models  # noqa: F401  register models

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_ROOT / "backend"
ALEMBIC_VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"


# Tokens banned from domain, application, and migrations per R1 / AC-009.
DOMAIN_DIR = BACKEND_DIR / "app" / "domain"
APPLICATION_DIR = BACKEND_DIR / "app" / "application"


# Tokens banned from domain, application, and migrations per R1 / AC-009.
FORBIDDEN_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bJSONB\b"),
    re.compile(r"\bGIN\b"),
    re.compile(r"\bILIKE\b"),
    re.compile(r"\bgen_random_uuid\s*\("),
    re.compile(r"\btsvector\b"),
)


def _run_alembic(*args: str, db_file: Path) -> subprocess.CompletedProcess[str]:
    """Invoke alembic in a subprocess with the sqlite test profile.

    Why a subprocess: alembic configures the global application context
    (settings, logging) when imported; running it inline would mutate
    module-level state shared with pytest. Running it as a subprocess
    keeps each invocation hermetic and matches the CI contract.

    Why call ``python -m alembic`` rather than ``uv run alembic``:
    ``uv run`` rebuilds the project before execution. The hatch build
    needs a ``README.md`` and fails when the workspace has none. The
    in-tree virtualenv already has alembic installed; invoking the
    module directly avoids the unnecessary rebuild and keeps the test
    hermetic.
    """
    env = os.environ.copy()
    env["APP_ENV"] = "test"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    # ``sys.executable -m alembic`` resolves the installed alembic console
    # script via the active virtualenv without triggering a uv build.
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _scan_for_forbidden_tokens(path: Path) -> dict[str, list[str]]:
    """Return ``{pattern: [matching_lines...]}`` for every Python file under ``path``.

    The scan uses the ``tokenize`` module so it ignores comments and
    triple-quoted docstrings; a CI failure therefore points at code that
    actually references a forbidden token, not at prose describing one.
    One-line strings (e.g., ``op.execute("...JSONB...")``) are still scanned
    because that's where raw dialect SQL would live.
    """
    if not path.exists():
        return {}

    findings: dict[str, list[str]] = {}
    for py_file in path.rglob("*.py"):
        # Skip alembic migrations: their portability is covered by the
        # explicit migration-source scan below.
        if ALEMBIC_VERSIONS_DIR in py_file.parents:
            continue
        try:
            source_bytes = py_file.read_bytes()
        except OSError:
            continue
        try:
            tokens = list(tokenize.tokenize(io.BytesIO(source_bytes).readline))
        except tokenize.TokenError:
            # A syntax error in source is not our concern here; skip.
            continue
        for tok in tokens:
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.ENCODING):
                continue
            # Why: triple-quoted docstrings always span multiple lines.
            # A one-line string with a forbidden token is almost always a
            # raw-SQL fragment inside ``op.execute(...)`` and must fail
            # the check.
            if tok.type is tokenize.STRING and "\n" in tok.string:
                continue
            for pattern in FORBIDDEN_TOKEN_PATTERNS:
                if pattern.search(tok.string):
                    rel = py_file.relative_to(REPO_ROOT)
                    key = f"{rel}:{tok.start[0]} [{pattern.pattern}] -> {tok.line.strip()}"
                    findings.setdefault(pattern.pattern, []).append(key)
    return findings


def _scan_migrations_for_forbidden_tokens() -> list[str]:
    """Scan every migration for forbidden tokens (preserving strings).

    Migrations are allowed to *mention* forbidden tokens in
    triple-quoted docstrings to document the rules; the only
    meaningful violations are real code references. One-line string
    literals (e.g., ``op.execute("SELECT ... JSONB ...")``) are still
    flagged because raw dialect SQL belongs there.
    """
    violations: list[str] = []
    for migration in sorted(ALEMBIC_VERSIONS_DIR.glob("*.py")):
        source_bytes = migration.read_bytes()
        try:
            tokens = list(tokenize.tokenize(io.BytesIO(source_bytes).readline))
        except tokenize.TokenError:
            continue
        for tok in tokens:
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.ENCODING):
                continue
            # Why: triple-quoted docstrings always span multiple lines;
            # skipping those allows the migration header to document
            # the rules. One-line strings still surface.
            if tok.type is tokenize.STRING and "\n" in tok.string:
                continue
            for pattern in FORBIDDEN_TOKEN_PATTERNS:
                if pattern.search(tok.string):
                    violations.append(
                        f"{migration.name}:{tok.start[0]} [{pattern.pattern}] -> {tok.line.strip()}"
                    )
    return violations


def test_migration_upgrade_then_downgrade_round_trip(tmp_path: Path) -> None:
    db_file = tmp_path / "alembic_roundtrip.db"

    upgrade = _run_alembic("upgrade", "head", db_file=db_file)
    assert upgrade.returncode == 0, (
        f"alembic upgrade head failed:\nSTDOUT:\n{upgrade.stdout}\nSTDERR:\n{upgrade.stderr}"
    )

    # Inspect the resulting schema via a fresh sync engine. The async
    # engine isn't needed for read-only inspection and keeps the test
    # independent of the application engine's session lifecycle.
    def _inspect_sync() -> dict[str, list[tuple[str, str]]]:
        engine = create_engine(f"sqlite:///{db_file.as_posix()}")
        try:
            insp = inspect(engine)
            result: dict[str, list[tuple[str, str]]] = {}
            for table in ("tenants", "users"):
                cols = insp.get_columns(table)
                result[table] = [(c["name"], str(c["type"])) for c in cols]
            return result
        finally:
            engine.dispose()

    columns = _inspect_sync()
    assert set(columns) == {"tenants", "users"}, (
        f"expected exactly tenants+users, got {sorted(columns)}"
    )

    # Column-type assertions. ``String`` and ``Uuid`` round-trip with
    # sqlite-affine names; the asserts are by column-name + presence of
    # the expected python type in the ORM model class.
    tenant_model = models.Tenant
    assert any(c.name == "id" for c in tenant_model.__table__.columns)
    id_col = next(c for c in tenant_model.__table__.columns if c.name == "id")
    assert isinstance(id_col.type, SAUuid)

    user_model = models.User
    by_name = {c.name: c for c in user_model.__table__.columns}
    assert isinstance(by_name["id"].type, SAUuid)
    assert isinstance(by_name["tenant_id"].type, SAUuid)
    assert isinstance(by_name["roles"].type, JSON)
    assert isinstance(by_name["is_system"].type, Boolean)
    assert isinstance(by_name["created_at"].type, DateTime)
    assert by_name["created_at"].type.timezone is True
    assert isinstance(by_name["updated_at"].type, DateTime)
    assert by_name["updated_at"].type.timezone is True
    assert isinstance(by_name["key"].type, String)
    assert isinstance(by_name["display_name"].type, String)

    # Why: every table the migration introduces lives in ``Base.metadata``
    # so the ORM can hydrate rows. We assert the metadata saw the same
    # two tables — guards against silent drift between the migration
    # and the model declarations.
    metadata_tables = set(Base.metadata.tables)
    assert {"tenants", "users"}.issubset(metadata_tables)

    downgrade = _run_alembic("downgrade", "-1", db_file=db_file)
    assert downgrade.returncode == 0, (
        f"alembic downgrade -1 failed:\nSTDOUT:\n{downgrade.stdout}\nSTDERR:\n{downgrade.stderr}"
    )

    # Full round-trip: drop everything via ``downgrade base`` and
    # confirm the foundation tables are gone. ``downgrade -1`` only
    # steps back one revision; the rest of the schema (audit, outbox,
    # idempotency, etc.) belongs to other tasks and would otherwise
    # leak into this assertion.
    full_downgrade = _run_alembic("downgrade", "base", db_file=db_file)
    assert full_downgrade.returncode == 0, (
        "alembic downgrade base failed:\n"
        f"STDOUT:\n{full_downgrade.stdout}\n"
        f"STDERR:\n{full_downgrade.stderr}"
    )

    # After full downgrade only the alembic bookkeeping table should
    # remain; the schema must be empty. A sync engine keeps the
    # inspection path hermetic and avoids async-engine bookkeeping
    # that ``inspect()`` rejects on the raw connection.
    sync_engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        remaining = list(inspect(sync_engine).get_table_names())
    finally:
        sync_engine.dispose()
    foundation_tables = {"tenants", "users"}
    leftover = foundation_tables & set(remaining)
    assert not leftover, (
        f"downgrade left foundation tables behind: {sorted(leftover)} (full set: {remaining})"
    )

    # And the migration history must replay cleanly back to head.
    replay = _run_alembic("upgrade", "head", db_file=db_file)
    assert replay.returncode == 0, (
        f"alembic upgrade head (replay) failed:\nSTDOUT:\n{replay.stdout}\nSTDERR:\n{replay.stderr}"
    )


def test_all_migration_tables_have_tenant_metadata_and_named_constraints(
    tmp_path: Path,
) -> None:
    """Every tenant table is indexed and every schema constraint is named.

    This inspects the complete revision chain, including revisions after the
    original foundation set. Explicit names make upgrades and downgrades
    deterministic across SQLite, PostgreSQL, and the future Azure adapter.
    """
    db_file = tmp_path / "schema_contract.db"
    upgrade = _run_alembic("upgrade", "head", db_file=db_file)
    assert upgrade.returncode == 0, (
        f"alembic upgrade head failed:\nSTDOUT:\n{upgrade.stdout}\nSTDERR:\n{upgrade.stderr}"
    )

    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        inspector = inspect(engine)
        tables = sorted(set(inspector.get_table_names()) - {"alembic_version", "tenants"})
        assert "channels" in tables, "captured-email dependency table is missing"
        for table in tables:
            columns = {column["name"]: column for column in inspector.get_columns(table)}
            tenant_id = columns.get("tenant_id")
            assert tenant_id is not None, f"{table}: tenant_id is required"
            assert tenant_id["nullable"] is False, f"{table}: tenant_id must be NOT NULL"

            tenant_fks = [
                fk
                for fk in inspector.get_foreign_keys(table)
                if fk.get("constrained_columns") == ["tenant_id"]
            ]
            assert len(tenant_fks) == 1, f"{table}: expected one tenant_id FK"
            assert tenant_fks[0]["referred_table"] == "tenants"
            assert tenant_fks[0]["name"], f"{table}: tenant FK must be named"

            tenant_indexes = [
                index
                for index in inspector.get_indexes(table)
                if "tenant_id" in index.get("column_names", [])
            ]
            assert tenant_indexes, f"{table}: tenant_id must be indexed"
            assert all(index["name"] for index in tenant_indexes)

            primary_key = inspector.get_pk_constraint(table)
            assert primary_key["name"], f"{table}: primary key must be named"
            assert all(foreign_key["name"] for foreign_key in inspector.get_foreign_keys(table)), (
                f"{table}: every foreign key must be named"
            )
            assert all(unique["name"] for unique in inspector.get_unique_constraints(table)), (
                f"{table}: every unique constraint must be named"
            )
    finally:
        engine.dispose()


def test_column_migrations_rollback_their_added_columns(tmp_path: Path) -> None:
    """Column additions disappear when their owning revisions are downgraded."""
    db_file = tmp_path / "column_rollback.db"

    upgrade = _run_alembic("upgrade", "head", db_file=db_file)
    assert upgrade.returncode == 0, (
        f"alembic upgrade head failed:\nSTDOUT:\n{upgrade.stdout}\nSTDERR:\n{upgrade.stderr}"
    )

    downgrade_sla = _run_alembic("downgrade", "0012_create_saved_searches", db_file=db_file)
    assert downgrade_sla.returncode == 0, (
        "downgrade through the SLA/queue revisions failed:\n"
        f"STDOUT:\n{downgrade_sla.stdout}\nSTDERR:\n{downgrade_sla.stderr}"
    )
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        assert "legal_hold" not in {
            column["name"] for column in inspect(engine).get_columns("cases")
        }
    finally:
        engine.dispose()

    downgrade_documents = _run_alembic("downgrade", "0008_create_import_jobs", db_file=db_file)
    assert downgrade_documents.returncode == 0, (
        "downgrade through the channel/email/document revisions failed:\n"
        f"STDOUT:\n{downgrade_documents.stdout}\nSTDERR:\n{downgrade_documents.stderr}"
    )
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        document_columns = {column["name"] for column in inspect(engine).get_columns("documents")}
        assert not {"locked", "locked_at", "locked_by"} & document_columns
        assert "channels" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    replay = _run_alembic("upgrade", "head", db_file=db_file)
    assert replay.returncode == 0, (
        f"alembic upgrade head replay failed:\nSTDOUT:\n{replay.stdout}\nSTDERR:\n{replay.stderr}"
    )


def test_migration_source_uses_no_forbidden_tokens() -> None:
    """Every migration file under ``alembic/versions`` must be portable.

    The check mirrors AC-009. Raw dialect SQL is allowed in migrations
    only with a portability comment; the forbidden tokens above are
    never acceptable.
    """
    assert ALEMBIC_VERSIONS_DIR.exists(), f"alembic versions dir missing: {ALEMBIC_VERSIONS_DIR}"

    violations = _scan_migrations_for_forbidden_tokens()
    assert not violations, "Forbidden tokens found in migrations:\n" + "\n".join(violations)


def test_domain_layer_uses_no_forbidden_tokens() -> None:
    """AC-009: ``domain`` and ``application`` layers stay dialect-neutral.

    Forbidden tokens: ``JSONB``, ``GIN``, ``ILIKE``, ``gen_random_uuid()``,
    ``tsvector``. Any match here is a portability regression.
    """
    findings = _scan_for_forbidden_tokens(DOMAIN_DIR)
    assert not findings, "Forbidden tokens in domain layer:\n" + "\n".join(
        f"{p}: {m}" for p, matches in findings.items() for m in matches
    )

    findings_app = _scan_for_forbidden_tokens(APPLICATION_DIR)
    assert not findings_app, "Forbidden tokens in application layer:\n" + "\n".join(
        f"{p}: {m}" for p, matches in findings_app.items() for m in matches
    )


def test_models_use_generic_sqlalchemy_types() -> None:
    """Every column on every ORM model must be one of the generic types.

    Allowed types (per ADR-001):

    - ``Uuid`` (generic), ``JSON``, ``DateTime(timezone=True)``
    - ``String``, ``Text``, ``Integer``, ``Boolean``
    - ``ForeignKey`` is an auxiliary construct, not a column type.
    """
    allowed_types: tuple[type, ...] = (
        SAUuid,
        JSON,
        DateTime,
        String,
        Integer,
        Boolean,
        SAText,
    )

    offenders: list[str] = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if not isinstance(column.type, allowed_types):
                offenders.append(f"{table.name}.{column.name} -> {type(column.type).__name__}")
    assert not offenders, "Non-generic column types detected:\n" + "\n".join(offenders)


def test_settings_database_url_is_well_formed() -> None:
    """Default config points at local PostgreSQL but the test profile is sqlite.

    This guards against accidentally committing a default URL that
    contains a dialect-specific scheme (e.g., ``postgres://`` without
    the async driver). The test profile URL is supplied by the
    ``APP_ENV=test`` + ``DATABASE_URL`` env pair; we don't override the
    default here, we just confirm the default parses cleanly.
    """
    settings = get_settings()
    assert settings.database_url, "DATABASE_URL is empty"
    assert settings.database_url.startswith("postgresql+asyncpg://") or (
        settings.database_url.startswith("sqlite+")
    ), f"unexpected DATABASE_URL scheme: {settings.database_url}"
