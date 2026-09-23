"""Fail if any specs/NNN-*/ folder is missing architecture.md.

Walks the specs/ directory, finds every immediate child whose name
matches the `NNN-...` slice convention, and asserts that each one
contains an `architecture.md` file with at least one Mermaid code
fence. The check enforces AGENTS.md §4.R9:

    "CI checks that architecture.md exists before marking slice done."

Stdlib only so it can run before `uv sync` or before any other tooling
is installed. Exit code 0 on success, 1 on failure.

Usage:

    python scripts/check_architecture_md.py            # default: walk repo root
    python scripts/check_architecture_md.py /path/to/repo   # override

The script never modifies the working tree.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SLICE_PATTERN = re.compile(r"^\d{3}-[a-z0-9][a-z0-9-]*$")
MERMAID_FENCE = re.compile(r"```mermaid")


def find_specs_root(start: Path) -> Path | None:
    """Locate the specs/ directory by walking up from `start`."""
    for candidate in (start, *start.parents):
        specs = candidate / "specs"
        if specs.is_dir():
            return specs
    return None


def discover_slices(specs_root: Path) -> list[Path]:
    """Return immediate child folders of specs/ that look like slices."""
    return sorted(
        child
        for child in specs_root.iterdir()
        if child.is_dir() and SLICE_PATTERN.match(child.name)
    )


def check_slice(slice_dir: Path) -> list[str]:
    """Return a list of human-readable problems with this slice."""
    problems: list[str] = []
    arch = slice_dir / "architecture.md"
    if not arch.exists():
        problems.append(f"{slice_dir.name}: missing architecture.md")
        return problems
    text = arch.read_text(encoding="utf-8")
    if not MERMAID_FENCE.search(text):
        problems.append(f"{slice_dir.name}: architecture.md has no ```mermaid fence")
    return problems


def main(argv: list[str]) -> int:
    repo_root = Path(argv[1]).resolve() if len(argv) > 1 else Path.cwd().resolve()
    specs_root = find_specs_root(repo_root)
    if specs_root is None:
        print(f"error: could not locate specs/ from {repo_root}", file=sys.stderr)
        return 2
    slices = discover_slices(specs_root)
    if not slices:
        print(f"error: no NNN-* slice folders found under {specs_root}", file=sys.stderr)
        return 2
    failures: list[str] = []
    for slice_dir in slices:
        failures.extend(check_slice(slice_dir))
    if failures:
        print("architecture.md gate failed:", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        print(
            f"\n{len(failures)} problem(s) across {len(slices)} slice(s). "
            "See AGENTS.md §4.R9 and docs/decisions/ADR-020-final.md.",
            file=sys.stderr,
        )
        return 1
    print(f"architecture.md gate OK: {len(slices)} slice(s) checked.")
    for slice_dir in slices:
        print(f"  - {slice_dir.name}/architecture.md")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))