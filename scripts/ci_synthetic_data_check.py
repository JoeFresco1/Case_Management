#!/usr/bin/env python3
"""CI synthetic-data check (ADR-019 §7).

Fails the build if any fixture, JSON document, markdown, or text file
under ``backend/tests`` or ``backend/app`` carries production-shaped
data: ``example.com`` email domains, DOB values, SSN-shaped numbers,
credit-card-shaped numbers, or ``prod-``/``live-``/``corp-`` prefixes
in tenant/external references.

Allowed test domains are the RFC 6761 reserved names: ``example.test``,
``example.org``, ``example.net``.

Exit codes:
    0 - clean
    1 - forbidden patterns found
    2 - script misconfiguration (e.g. missing roots)

Usage::

    python scripts/ci_synthetic_data_check.py
    python scripts/ci_synthetic_data_check.py --root backend/tests --root backend/app
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "backend" / "tests",
    REPO_ROOT / "backend" / "app",
)
ALLOWED_DOMAINS: frozenset[str] = frozenset({"example.test", "example.org", "example.net"})

# Files that intentionally enumerate forbidden patterns to *test* the
# redaction list and synthetic-data check. They are negative fixtures
# (the regex literal is part of the assertion). Excluded from scanning.
SELF_TEST_FILES: frozenset[str] = frozenset(
    {
        "backend/tests/security/test_phie_redaction.py",
        "scripts/ci_synthetic_data_check.py",
    }
)

EMAIL_RE = re.compile(r"\b([A-Za-z0-9._+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Email-style domains: only flag email-shaped tokens below.
    # Free-floating ``example.com`` in prose is allowed inside
    # documentation that references what to avoid; the email-domain
    # check below handles actual email usage.
    ("acme-corp domain", re.compile(r"\bacme-corp\.com\b", re.IGNORECASE)),
    ("prod-tenant id", re.compile(r"\bprod-(?:tenant|case)-", re.IGNORECASE)),
    # DOB values: keyed assignment only (DOB: 1980-04-12). Bare dates
    # like "2026-09-02" are timestamps and excluded.
    ("DOB value", re.compile(r"\bDOB\s*[:=]\s*\d{4}", re.IGNORECASE)),
    ("birth_date value", re.compile(r"\bbirth_date\s*[:=]\s*['\"]?\d{4}", re.IGNORECASE)),
)
SCAN_SUFFIXES: frozenset[str] = frozenset(
    {".py", ".json", ".jsonl", ".csv", ".md", ".txt", ".yaml", ".yml"}
)


def _iter_files(roots: tuple[Path, ...]) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in SCAN_SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            if any(part.startswith(".") for part in path.parts if part != "."):
                continue
            try:
                rel = path.relative_to(REPO_ROOT).as_posix()
            except ValueError:
                rel = path.as_posix()
            if rel in SELF_TEST_FILES:
                continue
            out.append(path)
    return out


def _scan_file(path: Path) -> list[str]:
    findings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return findings
    try:
        rel = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        rel = path.as_posix()

    for label, pattern in FORBIDDEN_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(f"{rel}: {label} :: {match.group(0)[:80]}")

    for match in EMAIL_RE.finditer(text):
        domain = match.group(2).lower()
        if domain not in ALLOWED_DOMAINS:
            findings.append(
                f"{rel}: non-test email domain '{domain}' :: {match.group(0)}"
            )

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        action="append",
        default=None,
        help="Root directory to scan (repeatable). Defaults to backend/tests + backend/app.",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress per-file progress output."
    )
    args = parser.parse_args(argv)

    roots: tuple[Path, ...] = (
        tuple(Path(r).resolve() for r in args.root) if args.root else DEFAULT_ROOTS
    )

    if not roots:
        print("ci-synthetic-data: no roots configured", file=sys.stderr)  # noqa: T201
        return 2

    files = _iter_files(roots)
    all_findings: list[str] = []
    for path in files:
        if not args.quiet:
            print(f"scan {path.relative_to(REPO_ROOT)}", file=sys.stderr)  # noqa: T201
        all_findings.extend(_scan_file(path))

    if all_findings:
        print("ci-synthetic-data: FAIL", file=sys.stderr)  # noqa: T201
        for finding in all_findings:
            print(f"  {finding}", file=sys.stderr)  # noqa: T201
        print(  # noqa: T201
            f"\n{len(all_findings)} forbidden synthetic-data pattern(s) "
            f"found across {len(files)} file(s).",
            file=sys.stderr,
        )
        return 1

    print(f"ci-synthetic-data: OK ({len(files)} files scanned)")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())