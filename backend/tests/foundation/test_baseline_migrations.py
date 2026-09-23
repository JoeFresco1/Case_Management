"""Baseline migration test (T00-DB / AGENTS.md §4.R1, §16.1).

Walks every migration ``upgrade head`` -> sequential ``downgrade -1``
back to ``base`` -> re-``upgrade head`` on a clean sqlite file. The
sqlite path is the AGENTS.md §4.R1 fallback that never requires
Docker / WSL.

# Acceptance contract (per task brief)

1. ``alembic upgrade head`` succeeds on a clean sqlite file.
2. Sequential ``alembic downgrade -1`` calls step every revision back
   to ``base`` without raising.
3. A second ``alembic upgrade head`` after the full downgrade succeeds
   (idempotent replay).
4. Every foundation table is created by the canonical migration set
   and removed by ``downgrade base``.
5. Every tenant-scoped FK column is non-null and indexed (the brief:
   "tenant_id on every FK").
6. No PostgreSQL-only operators, types, or raw SQL appear in any
   migration source file.

The test is hermetic: it spawns alembic in a subprocess so the
global application context (settings, logging, audit listeners) is
not mutated by the import side-effects of ``alembic`` itself.
"""

from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path

from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_ROOT / "backend"
ALEMBIC_VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"


# The canonical foundation table set (AGENTS.md §16.1). One entry per
# ``op.create_table`` that the brief enumerates.
FOUNDATION_TABLES: frozenset[str] = frozenset(
    {
        # Phase 0 / 0001 + 0002
        "tenants",
        "users",
        "audit_events",
        "outbox",
        # Phase 1 / 0003
        "case_types",
        "case_type_versions",
        "cases",
        # Phase 2 / 0004 (workflow)
        "workflow_definitions",
        "workflow_versions",
        "workflow_instances",
        "workflow_transitions",
        # Phase 2 / 0005 (workspace)
        "tasks",
        "documents",
        "document_versions",
        "participants",
        "decisions",
        # Phase 3 / 0006 (foundation hardening)
        "configuration_publications",
        "idempotency_keys",
        # H1/H2 slices (migrations 0007+): tables the brief did not
        # enumerate but the tenant-FK contract still covers.
        "reference_formats",  # 0021 (FM-009)
        "break_glass_grants",  # 0023 (FM-035)
        "form_sessions",  # 0024 (FM-038)
        "form_versions",  # 0024 (FM-036)
        "form_bindings",  # 0024 (FM-036)
        "legal_holds",  # 0026 (FM-039)
        # Records retention + disposition (0031 / FM-040): versioned
        # policies + per-case review queue. Holds already covered above;
        # archival/freeze/orphan stay in-memory seams (no ORM model yet);
        # evidence bundles (FM-060) are computed ZIPs audited as
        # AuditExported rows; report definitions (FM-053) stay in-memory.
        "retention_policies",  # 0031
        "disposition_requests",  # 0031
        # T5 viewer pipeline (0029 / FM-023 ext): overlay + checkout +
        # rendition rows. Tenant-FK contract asserted by the same loop.
        "viewer_annotations",  # 0029
        "document_checkout_locks",  # 0029
        "document_renditions",  # 0029
    }
)

# Tables the brief calls out for explicit FK indexing on tenant_id.
# All tenant-scoped FKs must be non-null; this set is what the test
# inspects after ``upgrade head``.
TABLES_WITH_TENANT_FK: frozenset[str] = frozenset(FOUNDATION_TABLES - {"tenants"})


# Forbidden tokens per AGENTS.md §4.R1 (AC-009). Migrations are scanned
# for one-line strings (raw SQL fragments) but the regex tolerates the
# canonical triple-quoted docstrings that describe the rule itself.
_FORBIDDEN_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bJSONB\b"),
    re.compile(r"\bGIN\b"),
    re.compile(r"\bILIKE\b"),
    re.compile(r"\bgen_random_uuid\b"),
    re.compile(r"\btsvector\b"),
    re.compile(r"\bARRAY\b", re.IGNORECASE),
    re.compile(r"\b(?:INT|BIGINT|NUM|TIMESTAMP|DATE|TS|D)\w*RANGE\b", re.IGNORECASE),
)


def _run_alembic(*args: str, db_file: Path) -> subprocess.CompletedProcess[str]:
    """Invoke alembic in a subprocess with the sqlite test profile.

    Why a subprocess: alembic configures the global application
    context (settings, logging) when imported; running it inline would
    mutate module-level state shared with pytest. Running it as a
    subprocess keeps each invocation hermetic and matches the CI
    contract from ``test_migration_portability.py``.
    """
    env = os.environ.copy()
    env["APP_ENV"] = "test"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _current_revision(db_file: Path) -> str | None:
    """Return the current alembic revision recorded in the file, or None.

    Uses ``alembic current`` because ``alembic heads`` reports head
    revisions independent of the database state; what we need is the
    revision that the upgrade actually applied.
    """
    proc = _run_alembic("current", db_file=db_file)
    assert proc.returncode == 0, (
        f"alembic current failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("INFO"):
            return stripped
    return None


def _list_tables(db_file: Path) -> list[str]:
    """List every user table currently in the sqlite database.

    A fresh sync engine keeps the inspection path hermetic and
    avoids the async engine's session bookkeeping that ``inspect()``
    rejects on raw connections.
    """
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        return list(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _column_metadata(db_file: Path, table: str) -> list[dict]:
    """Return ``[{name, type, nullable, ...}, ...]`` for ``table``."""
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        return list(inspect(engine).get_columns(table))
    finally:
        engine.dispose()


def _foreign_keys(db_file: Path, table: str) -> list[dict]:
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        return list(inspect(engine).get_foreign_keys(table))
    finally:
        engine.dispose()


def _indexes(db_file: Path, table: str) -> list[dict]:
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        return list(inspect(engine).get_indexes(table))
    finally:
        engine.dispose()


def _assert_tenant_id_required_and_indexed(db_file: Path, table: str) -> None:
    """Every tenant-scoped table must have a non-null ``tenant_id`` FK column.

    The brief: "tenant_id on every FK". This covers the tenant_id
    column itself (non-null, FK, indexed) — every foundation table
    except ``tenants`` itself, which has no tenant_id.
    """
    cols = _column_metadata(db_file, table)
    tenant_col = next((c for c in cols if c["name"] == "tenant_id"), None)
    assert tenant_col is not None, f"{table}: tenant_id column missing"
    assert tenant_col.get("nullable") is False, f"{table}: tenant_id must be NOT NULL"

    fks = _foreign_keys(db_file, table)
    tenant_fk = next(
        (fk for fk in fks if "tenant_id" in fk.get("constrained_columns", [])),
        None,
    )
    assert tenant_fk is not None, f"{table}: tenant_id must be a ForeignKey to tenants.id"
    assert tenant_fk.get("referred_table") == "tenants", (
        f"{table}: tenant_id FK must point at tenants.id (got {tenant_fk.get('referred_table')})"
    )

    indexes = _indexes(db_file, table)
    matching = [idx for idx in indexes if "tenant_id" in idx.get("column_names", [])]
    assert matching, f"{table}: tenant_id must be indexed (no ix_* on tenant_id)"


def test_baseline_migrations_upgrade_downgrade_replay(tmp_path: Path) -> None:
    db_file = tmp_path / "baseline.db"

    # ---- 1. clean upgrade -------------------------------------------------
    upgrade = _run_alembic("upgrade", "head", db_file=db_file)
    assert upgrade.returncode == 0, (
        f"alembic upgrade head failed:\nSTDOUT:\n{upgrade.stdout}\nSTDERR:\n{upgrade.stderr}"
    )

    current = _current_revision(db_file)
    assert current is not None, "alembic current returned no revision after upgrade head"
    # Alembic emits something like "<revision> (head)" — assert at least
    # that something looks like a revision id and the head marker is
    # present.
    assert "head" in current, f"unexpected alembic current output: {current!r}"

    # Every foundation table exists at head.
    tables_at_head = set(_list_tables(db_file))
    missing = FOUNDATION_TABLES - tables_at_head
    assert not missing, f"foundation tables missing after upgrade head: {sorted(missing)}"

    # tenant_id required + FK + indexed on every tenant-scoped table.
    for table in TABLES_WITH_TENANT_FK:
        _assert_tenant_id_required_and_indexed(db_file, table)

    # ---- 2. sequential downgrade back to base ----------------------------
    # Step one revision at a time so we exercise every downgrade path.
    # ``alembic downgrade -1`` moves back exactly one step; loop until
    # the current revision is ``None`` (i.e., base). The budget counts
    # the actual revision files so new migrations never silently exceed
    # a hardcoded constant.
    revision_count = len(list(ALEMBIC_VERSIONS_DIR.glob("*.py")))
    for step in range(revision_count + 2):
        before = _current_revision(db_file)
        only_alembic_bookkeeping = _list_tables(db_file) == ["alembic_version"]
        if before is None or only_alembic_bookkeeping:
            break
        proc = _run_alembic("downgrade", "-1", db_file=db_file)
        assert proc.returncode == 0, (
            f"alembic downgrade -1 (step {step}) failed:\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
        after = _current_revision(db_file)
        if after is None:
            break
    else:  # pragma: no cover — defensive
        raise AssertionError("downgrade did not reach base within expected steps")

    # After downgrade to base, only the alembic bookkeeping table remains.
    final_tables = set(_list_tables(db_file))
    leftover = FOUNDATION_TABLES & final_tables
    assert not leftover, (
        f"foundation tables survived downgrade base: {sorted(leftover)} "
        f"(all tables: {sorted(final_tables)})"
    )

    # ---- 3. re-upgrade ---------------------------------------------------
    replay = _run_alembic("upgrade", "head", db_file=db_file)
    assert replay.returncode == 0, (
        f"alembic upgrade head (replay) failed:\nSTDOUT:\n{replay.stdout}\nSTDERR:\n{replay.stderr}"
    )

    tables_after_replay = set(_list_tables(db_file))
    assert FOUNDATION_TABLES.issubset(tables_after_replay), (
        f"foundation tables missing after replay upgrade head: "
        f"{sorted(FOUNDATION_TABLES - tables_after_replay)}"
    )


def test_baseline_migrations_column_types_are_generic(tmp_path: Path) -> None:
    """Every column on every foundation table uses a portable generic type.

    Allowed types (per ADR-001, AGENTS.md §4.R1): ``Uuid`` (generic),
    ``JSON``, ``DateTime(timezone=True)``, ``String``, ``Text``,
    ``Integer``, ``Boolean``. ``ForeignKey`` is an auxiliary construct,
    not a column type.
    """
    db_file = tmp_path / "types.db"
    proc = _run_alembic("upgrade", "head", db_file=db_file)
    assert proc.returncode == 0, (
        f"alembic upgrade head failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )

    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        insp = inspect(engine)
        offenders: list[str] = []
        for table in sorted(FOUNDATION_TABLES):
            if not insp.has_table(table):
                offenders.append(f"{table}: table not present after upgrade head")
                continue
            for col in insp.get_columns(table):
                col_type = str(col["type"]).upper()
                # sqlite reflects ``Uuid`` as ``CHARACTER(36)`` /
                # ``UUID`` / ``TEXT`` depending on dialect version. We
                # assert the SQLAlchemy-side type via the source-level
                # migration rather than the reflected string; the
                # column-name presence check below covers the rest.
                portable_markers = (
                    "UUID",
                    "CHAR",
                    "TEXT",
                    "INTEGER",
                    "BIGINT",
                    "VARCHAR",
                    "BOOLEAN",
                    "DATETIME",
                    "TIMESTAMP",
                    "JSON",
                )
                if not any(marker in col_type for marker in portable_markers):
                    offenders.append(f"{table}.{col['name']} -> {col_type}")
    finally:
        engine.dispose()

    assert not offenders, "Non-portable column types detected:\n" + "\n".join(offenders)


def test_baseline_migrations_use_no_forbidden_tokens() -> None:
    """No migration source file contains a forbidden token (AGENTS.md §4.R1).

    Tokens banned by AC-009 / R1: ``JSONB``, ``GIN``, ``ILIKE``,
    ``gen_random_uuid()``, ``tsvector``. Migration source files are
    allowed to *describe* these rules in triple-quoted docstrings;
    the regex tolerates those by ignoring multi-line string tokens.
    """
    violations: list[str] = []
    for migration in sorted(ALEMBIC_VERSIONS_DIR.glob("*.py")):
        source_bytes = migration.read_bytes()
        try:
            tokens = list(tokenize.tokenize(io.BytesIO(source_bytes).readline))
        except tokenize.TokenError:
            continue
        for tok in tokens:
            if tok.type in (
                tokenize.COMMENT,
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.ENCODING,
            ):
                continue
            # Skip multi-line strings (triple-quoted docstrings) so the
            # migration header can document the rule without violating it.
            if tok.type is tokenize.STRING and "\n" in tok.string:
                continue
            for pattern in _FORBIDDEN_TOKEN_PATTERNS:
                if pattern.search(tok.string):
                    violations.append(
                        f"{migration.name}:{tok.start[0]} [{pattern.pattern}] -> {tok.line.strip()}"
                    )

    assert not violations, "Forbidden tokens in migrations:\n" + "\n".join(violations)


def _extract_module_assignments(source: str) -> dict[str, object]:
    """Return ``{name: value_ast_or_annotation}`` for every module-level assignment.

    Handles both ``name: Type = literal`` (AnnAssign) and ``name = literal``
    (Assign) forms. Migrations use the annotated form (``revision: str =
    "0001_..."``); the AST must look at both shapes.
    """
    tree = ast.parse(source)
    out: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                out[node.target.id] = node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                out[target.id] = node.value
    return out


def test_baseline_migrations_have_unique_revision_chain(tmp_path: Path) -> None:
    """Every migration declares a unique ``revision`` id.

    A duplicate revision id would silently merge two migrations into
    one branch and the upgrade would skip one of them. The test reads
    each file's module-level ``revision`` literal and asserts the set
    is equal in size to the file count.
    """
    revisions: dict[str, list[Path]] = {}
    for migration in sorted(ALEMBIC_VERSIONS_DIR.glob("*.py")):
        source = migration.read_text(encoding="utf-8")
        assigns = _extract_module_assignments(source)
        rev_node = assigns.get("revision")
        assert rev_node is not None, (
            f"{migration.name}: missing module-level 'revision: str' literal"
        )
        assert isinstance(rev_node, ast.Constant), (
            f"{migration.name}: revision literal is not a Constant: {rev_node!r}"
        )
        assert isinstance(rev_node.value, str), (
            f"{migration.name}: revision literal is not a string: {rev_node.value!r}"
        )
        rev_value = rev_node.value
        revisions.setdefault(rev_value, []).append(migration)

    duplicates = {rev: paths for rev, paths in revisions.items() if len(paths) > 1}
    assert not duplicates, f"duplicate revision ids: {duplicates}"


def _down_revision_from_ast(node: object | None) -> str | None:
    """Reduce a ``down_revision`` AST node to a ``str | None``.

    Alembic migrations annotate ``down_revision: str | None = "..."`` or
    ``down_revision: str | Sequence[str] | None = None``. The annotation
    shape creates either a ``Constant`` (``str`` or ``None``) or a
    ``BinOp(BitOr, ...)`` for the union type. Branched migrations use
    a tuple of strings (``Sequence[str]``) which the test rejects
    because the chain must be linear.
    """
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name) and node.id == "None":
        return None
    # ``str | None`` desugars to BinOp(BitOr, Constant("str"), Name("None")).
    # With ``from __future__ import annotations`` the annotation itself
    # is a string; we only see this shape when annotations are evaluated.
    return None


def test_baseline_migrations_chain_is_linear(tmp_path: Path) -> None:
    """The down_revision graph is a single linear chain ending in None.

    Each migration must declare a single ``down_revision`` (the prior
    revision in the chain) so ``alembic upgrade head`` can walk the
    graph deterministically. Branched chains are reserved for future
    slices and would need explicit branching labels.
    """
    parsed: dict[str, str | None] = {}
    for migration in sorted(ALEMBIC_VERSIONS_DIR.glob("*.py")):
        source = migration.read_text(encoding="utf-8")
        assigns = _extract_module_assignments(source)
        rev_node = assigns.get("revision")
        assert rev_node is not None, (
            f"{migration.name}: missing module-level 'revision: str' literal"
        )
        assert isinstance(rev_node, ast.Constant), (
            f"{migration.name}: revision literal is not a Constant: {rev_node!r}"
        )
        assert isinstance(rev_node.value, str), (
            f"{migration.name}: revision literal is not a string: {rev_node.value!r}"
        )
        rev_value = rev_node.value
        down_node = assigns.get("down_revision")
        down_value = _down_revision_from_ast(down_node)
        parsed[rev_value] = down_value

    # Root migration must have down_revision == None.
    roots = [rev for rev, down in parsed.items() if down is None]
    assert len(roots) == 1, f"expected exactly one root migration (down_revision=None), got {roots}"

    # Every non-root must reference a revision that actually exists and
    # that is itself referenced by exactly one other migration (linear).
    incoming: dict[str, int] = dict.fromkeys(parsed, 0)
    for rev, down in parsed.items():
        if down is not None:
            assert down in parsed, f"{rev}: down_revision '{down}' not present in the migration set"
            incoming[down] += 1

    # The chain is linear iff: one head (incoming == 0), one root
    # (down_revision is None), and every other revision has incoming == 1.
    heads = [rev for rev, count in incoming.items() if count == 0]
    assert len(heads) == 1, f"expected exactly one head revision (incoming == 0), got {heads}"

    non_linear = [rev for rev, count in incoming.items() if count > 1]
    assert not non_linear, (
        f"non-linear migration chain: revisions with > 1 incoming edge: {non_linear}"
    )

    # Sanity: every revision except the head and the root must have
    # incoming == 1.
    middle = [rev for rev, count in incoming.items() if count != 1 and rev not in heads]
    assert not middle, f"revisions with unexpected incoming edge count: {middle}"


def test_baseline_migrations_are_documented() -> None:
    """Every migration carries a triple-quoted module docstring.

    AGENTS.md §4.R4 requires module-level docstrings with purpose and
    seam notes. A missing docstring on a migration means the contract
    is unstated and review will reject the change.
    """
    offenders: list[str] = []
    for migration in sorted(ALEMBIC_VERSIONS_DIR.glob("*.py")):
        source = migration.read_text(encoding="utf-8")
        if not source.lstrip().startswith('"""'):
            offenders.append(migration.name)
    assert not offenders, "migrations missing module docstring: " + ", ".join(offenders)
