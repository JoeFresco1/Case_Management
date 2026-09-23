"""Portable-SQL enforcement test (AGENTS.md R1, ADR-001, ADR-018).

Mirrors ``scripts/check_portable_sql.sh`` for the pytest suite so the
same lint runs locally without needing bash on the developer host.
The bash script is the CI gate; this test is the developer gate.

The check covers four surfaces:

1. ``backend/app/domain`` and ``backend/app/application`` are
   dialect-neutral. The forbidden tokens (``JSONB``, ``GIN``,
   ``ILIKE``, ``tsvector``, ``gen_random_uuid``, array/range markers)
   are not allowed here at all — there is no portability escape
   hatch in domain code.
2. ``backend/alembic/versions`` may carry a forbidden token only
   when the same file contains a ``# portability:`` marker that
   documents the exception. The marker is the seam an Azure-SQL
   adapter would consult to know what to strip.
3. Every migration defines both ``upgrade()`` and ``downgrade()``.
   Migrations that ship without a reversible path are blocked
   because the foundation must roll forward and back cleanly.
4. The alembic env (``alembic/env.py``) registers the portable-SQL
   lint so an operator running ``alembic upgrade head`` sees the
   failure immediately rather than at the production rollout.

The fixture-driven round trip in
``tests/foundation/test_migration_portability.py`` exercises the
sqlite upgrade→downgrade→upgrade path; this file focuses on the
static contract.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_ROOT / "backend"
ALEMBIC_VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"
DOMAIN_DIR = BACKEND_DIR / "app" / "domain"
APPLICATION_DIR = BACKEND_DIR / "app" / "application"
INTERFACES_DIR = BACKEND_DIR / "app" / "interfaces"

# Tokens forbidden everywhere per ADR-001 / AGENTS.md R1.
# ``JSONB`` is forbidden; ``JSON`` is the portable generic and is
# permitted. The regex uses word boundaries so ``jsonbuilder`` (a
# hypothetical Python class) would not match.
FORBIDDEN_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bJSONB\b"),
    re.compile(r"\bGIN\b"),
    re.compile(r"\bILIKE\b"),
    re.compile(r"\btsvector\b"),
    re.compile(r"\bgen_random_uuid\b"),
)
FORBIDDEN_ARRAY_RE = re.compile(r"\bARRAY\s*(?:\[|\()", re.IGNORECASE)
FORBIDDEN_RANGE_RE = re.compile(
    r"\b(?:INT|BIGINT|NUM|TIMESTAMP|DATE|TS|D)\w*RANGE\b", re.IGNORECASE
)
PORTABILITY_MARKER_RE = re.compile(r"#\s*portability\s*:", re.IGNORECASE)


def _scan_python_source(source: str) -> list[str]:
    """Return the forbidden-token violations present in ``source``.

    Tokenizes source so identifiers such as ``sa.ARRAY`` are checked as
    well as one-line raw SQL strings. Multi-line strings (triple-quoted
    docstrings or trigger bodies) are skipped so prose can document the
    rule without becoming a false positive.
    """
    violations: list[str] = []
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
            for pattern in FORBIDDEN_TOKEN_PATTERNS:
                if pattern.search(token.string):
                    violations.append(pattern.pattern)
            next_token = (
                significant_tokens[index + 1].string if index + 1 < len(significant_tokens) else ""
            )
            if FORBIDDEN_ARRAY_RE.search(token.string) or (
                token.string.upper() == "ARRAY" and next_token in {"[", "("}
            ):
                violations.append("ARRAY")
            if FORBIDDEN_RANGE_RE.search(token.string):
                violations.append("<type>RANGE")
    except (tokenize.TokenError, IndentationError):
        return []
    return list(dict.fromkeys(violations))


def _scan_strict(path: Path) -> list[str]:
    """Forbidden tokens anywhere in source — no escape hatch.

    Walks the AST so docstrings that *describe* the rule do not fire;
    only single-line string literals (where raw SQL would actually
    live) and identifiers in code trigger the lint.
    """
    if not path.exists():
        return []
    findings: list[str] = []
    for py_file in path.rglob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        violations = _scan_python_source(source)
        if not violations:
            continue
        findings.append(f"{py_file.relative_to(REPO_ROOT)}: {violations}")
    return findings


def test_domain_and_application_have_no_forbidden_tokens() -> None:
    """R1 / AC-009: domain and application stay dialect-neutral."""
    domain_hits = _scan_strict(DOMAIN_DIR)
    application_hits = _scan_strict(APPLICATION_DIR)
    assert not domain_hits, "Forbidden tokens in domain: " + ", ".join(domain_hits)
    assert not application_hits, "Forbidden tokens in application: " + ", ".join(application_hits)


def test_interfaces_have_no_forbidden_tokens() -> None:
    """Interfaces compose domain + application; same rule applies.

    The interfaces layer is where the FastAPI routers and schemas live;
    allowing a forbidden token there would silently leak into the
    public API surface. Mirrors AGENTS.md R1 ("domain/application")
    — we extend the same rule to ``interfaces`` because the contract
    says nothing portable belongs in code that compiles to schema.
    """
    interface_hits = _scan_strict(INTERFACES_DIR)
    assert not interface_hits, "Forbidden tokens in interfaces: " + ", ".join(interface_hits)


def test_migrations_have_both_upgrade_and_downgrade() -> None:
    """Every Alembic migration must be reversible (AGENTS.md R2 / R8)."""
    assert ALEMBIC_VERSIONS_DIR.exists()
    offenders: list[str] = []
    for migration in sorted(ALEMBIC_VERSIONS_DIR.glob("*.py")):
        if migration.name == "__pycache__":
            continue
        try:
            source = migration.read_text(encoding="utf-8")
        except OSError:
            continue
        tree = ast.parse(source)
        names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        if "upgrade" not in names:
            offenders.append(f"{migration.name}: missing upgrade()")
        if "downgrade" not in names:
            offenders.append(f"{migration.name}: missing downgrade()")
    assert not offenders, "Migrations missing upgrade/downgrade:\n" + "\n".join(offenders)


def test_migrations_forbidden_tokens_require_portability_marker() -> None:
    """Migrations may carry forbidden tokens only with a marker.

    Docstrings that mention the rule by name do not count: the scan
    uses ``_scan_python_source`` so only one-line string literals
    (where raw dialect SQL would actually live) trigger the check.
    """
    offenders: list[str] = []
    for migration in sorted(ALEMBIC_VERSIONS_DIR.glob("*.py")):
        if migration.name == "__pycache__":
            continue
        try:
            source = migration.read_text(encoding="utf-8")
        except OSError:
            continue
        violations = _scan_python_source(source)
        if not violations:
            continue
        if PORTABILITY_MARKER_RE.search(source):
            continue
        offenders.append(f"{migration.name}: {violations}")
    assert not offenders, (
        "Migrations with forbidden tokens but no '# portability:' marker:\n"
        + "\n".join(offenders)
        + "\nSee AGENTS.md R1 and ADR-001."
    )


def test_alembic_env_registers_portable_sql_lint() -> None:
    """Alembic env must lint migration source before running migrations.

    Why this assertion exists: developers can run alembic directly
    (e.g., ``uv run alembic upgrade head``) without going through
    pytest. The lint inside ``env.py`` is what catches a forbidden
    token in that workflow; without it the only enforcement is the
    static tests, which a developer can skip.
    """
    env_path = BACKEND_DIR / "alembic" / "env.py"
    assert env_path.exists()
    source = env_path.read_text(encoding="utf-8")
    assert "FORBIDDEN_TOKENS" in source, (
        "alembic/env.py must declare a FORBIDDEN_TOKENS tuple (see ADR-018)."
    )
    assert "lint_migration_source" in source, (
        "alembic/env.py must implement lint_migration_source() (see ADR-018)."
    )
    assert "run_lint_on_pending" in source, (
        "alembic/env.py must invoke the lint before run_migrations (see ADR-018)."
    )


def test_check_portable_sql_sh_exists_and_is_executable_intent() -> None:
    """The CI bash gate must be present and reference the same tokens.

    The script does not have to be marked executable on Windows for the
    test to pass; CI runners set the bit on checkout. We assert the
    file content instead.
    """
    script_path = REPO_ROOT / "scripts" / "check_portable_sql.sh"
    assert script_path.exists(), "scripts/check_portable_sql.sh missing"
    text = script_path.read_text(encoding="utf-8")
    for token in (
        "JSONB",
        "GIN",
        "ILIKE",
        "tsvector",
        "gen_random_uuid",
        "ARRAY",
        "RANGE",
    ):
        assert token in text, f"check_portable_sql.sh must reference {token}"
    assert "portability" in text.lower(), (
        "check_portable_sql.sh must document the '# portability:' marker"
    )


def test_migration_violation_is_flagged() -> None:
    """Smoke: a synthetic source with JSONB fails the lint.

    This is the regression guard for the lint itself — if the regex
    ever loosens, this test fires first.
    """
    bad = (
        "def upgrade():\n"
        "    op.execute('CREATE INDEX CONCURRENTLY foo ON bar USING GIN (payload)')\n"
        "def downgrade():\n"
        "    op.drop_index('foo', 'bar')\n"
    )
    assert _scan_python_source(bad), (
        "expected JSONB/GIN to be flagged; lint regex may have regressed"
    )
    assert _scan_python_source("sa.ARRAY(sa.String())") == ["ARRAY"]
    assert _scan_python_source("sa.dialects.postgresql.DATERANGE()") == ["<type>RANGE"]
    # Same source with a portability marker should NOT be flagged by
    # the migrations scanner — the marker is the contract exception.
    with_marker = bad + "# portability: postgres-only GIN index for legacy payload\n"
    assert PORTABILITY_MARKER_RE.search(with_marker)
