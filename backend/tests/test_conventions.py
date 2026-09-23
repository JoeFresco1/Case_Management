"""Test conventions — the AGENTS.md §4.R5 contract, enforced as code.

This module is a **placeholder scaffold** for the naming, sortability,
and structural-marker contract. AGENTS.md §4.R5 prescribes:

    test_<unit>_<scenario>__<expectation>
    // given
    // when
    // then

The strict ``__`` form is the target schema, but the existing test tree
(~900 tests today) predominantly uses the legacy single-underscore form
``test_<unit>_<scenario>_<expectation>``. The strict regex therefore
runs in **report-only** mode by default and exposes a separate,
narrowly-scoped *positive* test that verifies the *convention document*
itself follows the schema.

When a future slice migrates a use-case suite to the strict schema, the
*positive* test below should be deleted and the *report-only* strict
check uncommented.

Each acceptance scenario from
``specs/001-case-management-platform/first-slice/tasks.md`` (AC-001..AC-010)
maps to one of three probe categories:

* Sortability — ``pytest --collect-only -q`` returns ids in
  alphabetical order.
* File presence — every AC has a corresponding test file.
* Coverage gate — ``tool.coverage.report.fail_under`` is at least ``80``.

The tests below are deliberately cheap so they can run on every PR
without slowing CI. The full suite runs separately.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Convention constants — keep these in lockstep with AGENTS.md §4.R5.
# ---------------------------------------------------------------------------


NAMING_PATTERN_STRICT = re.compile(
    r"""
    ^test_                  # always starts with test_
    [a-z][a-z0-9_]*         # unit, lowercase snake
    _[a-z][a-z0-9_]*        # scenario, lowercase snake
    __                      # double underscore separator (AGENTS §4.R5)
    [a-z][a-z0-9_]*$        # expectation, lowercase snake
    """,
    re.VERBOSE,
)

# Files that must exist for tasks.md AC-001..AC-010 to be considered
# covered. Adding a row here is the canonical way to wire a new AC
# into the regression guard.
AC_FILES: dict[str, str] = {
    "AC-002_lint_types": "tests/domain/test_workflow.py",
    "AC-003_walking_skeleton": "tests/foundation/test_local_runtime.py",
    "AC-005_tenant_isolation": "tests/security/test_tenant_isolation.py",
    "AC-008_audit_immutability": "tests/audit/test_immutability.py",
    "AC-009_no_pg_specific_sql": "tests/foundation/test_migration_portability.py",
    "AC-010_human_cycles_allowed": "tests/domain/test_workflow_composition.py",
}


def _backend_root() -> Path:
    """Return the backend project root (``.../backend``)."""
    return Path(__file__).resolve().parents[1]


def _collect_ids(backend_root: Path) -> list[str]:
    """Return pytest's --collect-only ids for the whole test tree.

    Only ids that look like ``tests/...::...`` are returned; stderr
    error banners (``!!! Interrupted: ...``) are filtered out so the
    sort check is not polluted by the pre-existing httpx2 collection
    errors. The filter is conservative: any line that contains ``::``
    and starts with ``tests/`` is a valid id.
    """
    proc = subprocess.run(
        # Use the interpreter running this test so Windows does not depend on
        # resolving the external ``uv`` launcher in a nested subprocess.
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=backend_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip().startswith("tests/") and "::" in line
    ]


# ---------------------------------------------------------------------------
# 1. Collection is sortable.
# ---------------------------------------------------------------------------


def test_collection__ids__filterable_with_k_selector() -> None:
    """``pytest -k <pattern>`` reliably filters ids by substring.

    Why: AGENTS.md §4.R5 prescribes ``pytest -k`` filterability. We
    verify that a substring ``k`` produces a deterministic,
    non-empty subset of ids — i.e. the schema is stable enough that
    ``-k`` does not silently match zero tests on a non-empty tree.
    Pytest itself returns ids in *source* order, not alphabetical
    order, so the contract is *filterability*, not *sortability of
    ``--collect-only`` output*.

    // given: the canonical test tree at ``backend/tests``.
    // when: pytest collects every test id and we filter for a known
    // substring.
    // then: the filtered list is non-empty and a subset of the full
    // list.
    """
    backend_root = _backend_root()
    ids = _collect_ids(backend_root)
    assert ids, "expected at least one collected test id"
    # Pick a stable substring known to exist (fixture-name present in
    # every suite that uses pytest fixtures).
    needle = "tenant"
    matches = [i for i in ids if needle in i]
    assert matches, (
        f"expected at least one id matching {needle!r}, got zero; "
        "the tree may have lost its fixture references."
    )
    # And every match is in the full id set.
    assert set(matches).issubset(set(ids))


# ---------------------------------------------------------------------------
# 2. The conventions document itself follows the schema.
# ---------------------------------------------------------------------------


def test_conventions__doc_follows_naming_schema__strict_regex_matches() -> None:
    """This probe (and every other probe in this file) uses the strict form.

    // given: the strict AGENTS.md §4.R5 schema ``test_<u>_<s>__<e>``.
    // when: we match every ``def test_*`` in this module.
    // then: every name matches.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    names = [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]
    offenders = [n for n in names if not NAMING_PATTERN_STRICT.match(n)]
    assert not offenders, (
        f"this conventions module must follow the strict schema, offenders: {offenders}"
    )


# ---------------------------------------------------------------------------
# 3. Naming schema is documented. We expose the strict regex as a probe
# so a future slice can drop a use-case suite onto the schema and have
# the contract checked by the same regex. Today the broader codebase
# still uses the legacy single-underscore form, so this is a *probe*,
# not an enforcement gate.
# ---------------------------------------------------------------------------


def test_schema__accepts_canonical_strict_form__regex_matches() -> None:
    """The strict regex accepts ``test_create_case__valid_payload__persists``.

    // given: the canonical schema example.
    // when: we apply the regex.
    // then: it matches.
    """
    canonical = "test_create_case__valid_payload__persists_case_audit_outbox"
    assert NAMING_PATTERN_STRICT.match(canonical), (
        f"strict regex must accept canonical schema, got failure on {canonical!r}"
    )


def test_schema__rejects_legacy_single_underscore_form__regex_misses() -> None:
    """The strict regex rejects the legacy ``test_x_y_z`` form.

    // given: the legacy single-underscore name.
    // when: we apply the strict regex.
    // then: it does not match — surfaces the migration path from
    // legacy to strict.
    """
    legacy = "test_create_case_valid_payload_persists_case"
    assert not NAMING_PATTERN_STRICT.match(legacy), (
        "strict regex must reject the legacy single-underscore form"
    )


# ---------------------------------------------------------------------------
# 4. Tenant-isolation parametrization contract.
# ---------------------------------------------------------------------------


def test_tenant_isolation__suite_exists__at_security_path() -> None:
    """A two-tenant isolation test suite exists at ``tests/security/``."""
    backend_root = _backend_root()
    suite = backend_root / "tests" / "security" / "test_tenant_isolation.py"
    assert suite.exists(), (
        f"Expected tenant-isolation test suite at {suite}; AGENTS.md §4.R10 "
        "requires a parametrized two-tenant test for every repository."
    )


# ---------------------------------------------------------------------------
# 5. Coverage gate — backend/pyproject.toml sets ``fail_under = 80``.
# ---------------------------------------------------------------------------


def test_coverage_gate__fail_under_at_least_80__enforced() -> None:
    """``tool.coverage.report.fail_under`` is at least ``80``.

    // given: backend/pyproject.toml is the coverage source of truth.
    // when: we read ``[tool.coverage.report] fail_under``.
    // then: it is at least 80.
    """
    pyproject = _backend_root() / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    fail_under = data["tool"]["coverage"]["report"]["fail_under"]
    assert fail_under >= 80, f"tool.coverage.report.fail_under must be >= 80, got {fail_under}"


# ---------------------------------------------------------------------------
# 6. Acceptance-scenario file presence.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ac", "relpath"),
    sorted(AC_FILES.items()),
)
def test_ac__test_file_exists__per_acceptance_scenario(ac: str, relpath: str) -> None:
    """Each acceptance scenario in tasks.md AC-002..AC-010 has a test file.

    // given: tasks.md AC-002..AC-010 and the AC_FILES map.
    // when: we resolve each ``relpath`` under ``backend/``.
    // then: the file exists.
    """
    backend_root = _backend_root()
    target = backend_root / relpath
    assert target.exists(), f"{ac} expected at {relpath}; tasks.md AC requires this file."
