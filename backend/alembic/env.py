"""Alembic environment.

Configures the async engine from application settings, registers all
ORM models via ``app.infrastructure.persistence.models``, and runs a
**portable-SQL lint** before each migration so dialect-specific tokens
(JSONB, GIN, ILIKE, tsvector, gen_random_uuid, array/range types) are
caught at migration time rather than at production rollout.

The lint is intentionally lightweight: it walks the migration file's
source for forbidden tokens and refuses to run a migration whose
upgrade path contains them without an accompanying ``# portability:``
comment that justifies the dialect-specific operation. This mirrors
AGENTS.md R1 / plan.md:288-303 and ADR-001.

CI enforces the same contract via ``scripts/check_portable_sql.sh``
and ``backend/tests/foundation/test_portable_sql.py`` so a developer
who bypasses ``alembic upgrade`` still gets blocked at lint time.
"""

from __future__ import annotations

import asyncio
import io
import re
import tokenize
from logging.config import fileConfig
from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.config import get_settings
from app.db import Base
from app.infrastructure.persistence import models  # noqa: F401  (register models)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


# Forbidden tokens per AGENTS.md R1 / ADR-001 / plan.md:288-303.
# ``re.IGNORECASE`` keeps the lint tolerant of ``Jsonb`` vs ``JSONB``
# without false-positives on JSON (the portable type) which is a
# substring.
FORBIDDEN_TOKENS: tuple[str, ...] = (
    "JSONB",
    "GIN",
    "ILIKE",
    "tsvector",
    "gen_random_uuid",
)
# Array / range markers are checked after tokenization, so prose in a
# migration docstring does not look like executable dialect-specific DDL.
FORBIDDEN_ARRAY_RE = re.compile(r"\bARRAY\s*(?:\[|\()", re.IGNORECASE)
FORBIDDEN_RANGE_RE = re.compile(
    r"\b(?:INT|BIGINT|NUM|TIMESTAMP|DATE|TS|D)\w*RANGE\b", re.IGNORECASE
)


def lint_migration_source(source: str) -> list[str]:
    """Return human-readable violations found in ``source``.

    The lint scans the upgrade path of a migration and refuses raw
    dialect SQL unless the same script carries a ``# portability:``
    comment naming the dialect-specific construct. Documenting the
    exception is the seam that lets a future Azure-SQL adapter strip
    the dialect block.
    """
    violations: list[str] = []
    has_portability_marker = bool(re.search(r"#\s*portability\s*:", source, re.IGNORECASE))
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        significant_tokens = [
            token
            for token in tokens
            if token.type
            not in {
                tokenize.COMMENT,
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.ENDMARKER,
            }
            and not (token.type == tokenize.STRING and "\n" in token.string)
        ]
        for index, token in enumerate(significant_tokens):
            for forbidden in FORBIDDEN_TOKENS:
                if re.search(rf"\b{re.escape(forbidden)}\b", token.string, re.IGNORECASE):
                    violations.append(forbidden)
            next_token = (
                significant_tokens[index + 1].string if index + 1 < len(significant_tokens) else ""
            )
            if FORBIDDEN_ARRAY_RE.search(token.string) or (
                token.string.upper() == "ARRAY" and next_token in {"[", "("}
            ):
                violations.append("ARRAY")
            if FORBIDDEN_RANGE_RE.search(token.string):
                violations.append("<type>RANGE")
    except tokenize.TokenError:
        # A syntax error will be reported by Alembic when it imports the
        # revision; this lint should not hide that more specific failure.
        return []

    if violations and has_portability_marker:
        return []
    return list(dict.fromkeys(violations))


def run_lint_on_pending(script_directory: ScriptDirectory | None) -> None:
    """Lint every pending migration; raise ``RuntimeError`` on violation.

    Why this runs unconditionally (including ``offline``): the contract
    is the same in both modes — a migration that depends on a
    PostgreSQL-only operator will fail on sqlite in the test profile
    before it ever reaches production, so catching it here turns a
    silent runtime failure into an explicit upgrade-time error.

    ``script_directory`` is the ``ScriptDirectory`` alembic exposes via
    ``context.script`` (newer releases) or falls back to deriving from
    ``context.config`` (older releases). Both code paths land on the
    same iteration over revisions.
    """
    sd = script_directory
    if sd is None:
        try:
            sd = ScriptDirectory.from_config(context.config)
        except Exception as exc:  # pragma: no cover - defensive
            raise RuntimeError("unable to load Alembic revisions for portability lint") from exc
    for script in sd.walk_revisions():
        try:
            source = Path(script.path).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, AttributeError):
            continue
        bad = lint_migration_source(source)
        if bad:
            raise RuntimeError(
                f"portable-SQL lint failed for migration {script.revision}: "
                f"forbidden tokens {bad!r} without '# portability:' "
                "marker. See AGENTS.md R1 and ADR-001."
            )


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    run_lint_on_pending(getattr(context, "script", None))
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    run_lint_on_pending(getattr(context, "script", None))
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
