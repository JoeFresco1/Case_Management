#!/usr/bin/env python3
"""Create a deterministic source inventory for a Git snapshot.

The inventory reads tracked blobs from Git, so working-tree changes and local
runtime files cannot affect the result. Generated and test source files are
reported separately from handwritten production and tooling sources.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any


LANGUAGES = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".css": "CSS",
    ".go": "Go",
    ".html": "HTML",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".mjs": "JavaScript",
    ".php": "PHP",
    ".ps1": "PowerShell",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".scss": "SCSS",
    ".sh": "Shell",
    ".sql": "SQL",
    ".swift": "Swift",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".vue": "Vue",
    ".yaml": "YAML",
    ".yml": "YAML",
}

GENERATED_PARTS = {
    "__generated__",
    "generated",
    "gen",
    "dist",
    "build",
    "node_modules",
}
GENERATED_NAME = re.compile(r"(?:\.gen\.|\.generated\.|\.min\.)", re.IGNORECASE)
TEST_NAME = re.compile(r"(?:^test_.*\.py$|.*(?:\.test|\.spec)\.[^.]+$|.*_test\.py$)", re.I)


def run_git(root: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    """Run Git and return stdout, raising a readable error on failure."""
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(arguments)} failed: {message}")
    return result.stdout


def parse_tree(root: Path, snapshot: str) -> tuple[str, list[dict[str, str]]]:
    """Return the resolved commit and sorted tracked blob entries."""
    commit = run_git(root, "rev-parse", "--verify", f"{snapshot}^{{commit}}").decode().strip()
    raw_entries = run_git(root, "ls-tree", "-r", "-z", "--full-tree", commit)
    entries: list[dict[str, str]] = []
    for entry in raw_entries.split(b"\0"):
        if not entry:
            continue
        header, raw_path = entry.split(b"\t", 1)
        mode, object_type, object_id = header.decode("ascii").split()
        if object_type == "blob":
            entries.append(
                {"mode": mode, "oid": object_id, "path": raw_path.decode("utf-8", "surrogateescape")}
            )
    entries.sort(key=lambda item: item["path"])
    return commit, entries


def component_for(path: str) -> str:
    """Map a source path to its top-level product or tooling component."""
    parts = path.split("/")
    if parts[0] == "backend" and len(parts) > 1:
        if len(parts) > 2 and parts[1] in {"app", "tests"}:
            return "/".join(parts[:3])
        return "/".join(parts[:2])
    if parts[0] == "frontend" and len(parts) > 2 and parts[1] == "src":
        return "/".join(parts[:3])
    if len(parts) > 1:
        return parts[0]
    return "repository"


def exclusion_reason(path: str) -> str | None:
    """Return the stable generated/test exclusion reason, if any."""
    parts = path.lower().split("/")
    name = parts[-1]
    if any(part in GENERATED_PARTS for part in parts):
        return "generated"
    if GENERATED_NAME.search(name) or name.startswith("openapi."):
        return "generated"
    if any(part in {"test", "tests", "__tests__", "testing"} for part in parts):
        return "test"
    if name in {"test-setup.ts", "test-setup.tsx", "setup-tests.ts", "setup-tests.tsx"}:
        return "test"
    if TEST_NAME.match(name):
        return "test"
    return None


def line_count(content: bytes) -> int:
    """Count nonblank physical UTF-8 lines; comments remain included as LOC."""
    text = content.decode("utf-8", errors="replace")
    return sum(1 for line in text.splitlines() if line.strip())


def read_snapshot_file(root: Path, commit: str, path: str) -> bytes:
    """Read a file blob from the selected commit, not from the worktree."""
    return run_git(root, "show", f"{commit}:{path}")


def read_blobs(root: Path, entries: list[dict[str, str]]) -> dict[str, bytes]:
    """Read all candidate source blobs in one Git process."""
    candidates = [entry for entry in entries if Path(entry["path"]).suffix.lower() in LANGUAGES]
    if not candidates:
        return {}
    request = "".join(f"{entry['oid']}\n" for entry in candidates).encode("ascii")
    response = run_git(root, "cat-file", "--batch", input_bytes=request)
    cursor = 0
    blobs: dict[str, bytes] = {}
    for entry in candidates:
        header_end = response.find(b"\n", cursor)
        if header_end < 0:
            raise RuntimeError("git cat-file --batch returned a truncated header")
        oid, object_type, raw_size = response[cursor:header_end].decode("ascii").split()
        if oid != entry["oid"] or object_type != "blob":
            raise RuntimeError(f"unexpected Git object while reading {entry['path']}")
        size = int(raw_size)
        content_start = header_end + 1
        content_end = content_start + size
        blobs[entry["oid"]] = response[content_start:content_end]
        if response[content_end : content_end + 1] != b"\n":
            raise RuntimeError(f"git cat-file --batch returned a truncated blob for {entry['path']}")
        cursor = content_end + 1
    return blobs


def configured_checks(root: Path, commit: str) -> dict[str, Any]:
    """Extract check commands and configuration from the two requested manifests."""
    python_project = tomllib.loads(
        read_snapshot_file(root, commit, "backend/pyproject.toml").decode("utf-8")
    )
    frontend = json.loads(read_snapshot_file(root, commit, "frontend/package.json"))
    tool = python_project.get("tool", {})
    pytest = tool.get("pytest", {}).get("ini_options", {})
    return {
        "backend": {
            "ruff": {
                "configured": "ruff" in tool,
                "line_length": tool.get("ruff", {}).get("line-length"),
                "target_version": tool.get("ruff", {}).get("target-version"),
                "configured_lint_rules": tool.get("ruff", {}).get("lint", {}).get("select", []),
            },
            "mypy": {
                "configured": "mypy" in tool,
                "strict": tool.get("mypy", {}).get("strict"),
                "files": tool.get("mypy", {}).get("files", []),
            },
            "pytest": {
                "configured": "pytest" in tool,
                "testpaths": pytest.get("testpaths", []),
                "addopts": pytest.get("addopts", []),
                "coverage_fail_under": tool.get("coverage", {}).get("report", {}).get("fail_under"),
            },
            "import_linter": {
                "configured": "importlinter" in tool,
                "contracts": [
                    contract.get("name")
                    for contract in tool.get("importlinter", {}).get("contracts", [])
                ],
            },
        },
        "frontend": {
            "package_manager": frontend.get("packageManager"),
            "scripts": {
                name: frontend.get("scripts", {})[name]
                for name in sorted(frontend.get("scripts", {}))
                if name in {"build", "lint", "typecheck", "test"}
            },
        },
    }


def build_inventory(root: Path, snapshot: str) -> dict[str, Any]:
    """Build the stable repository inventory for one immutable Git snapshot."""
    commit, entries = parse_tree(root, snapshot)
    blobs = read_blobs(root, entries)
    sources: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    language_counts: Counter[str] = Counter()
    component_counts: Counter[str] = Counter()

    for entry in entries:
        path = entry["path"]
        language = LANGUAGES.get(Path(path).suffix.lower())
        if language is None:
            continue
        content = blobs[entry["oid"]]
        loc = line_count(content)
        reason = exclusion_reason(path)
        record = {
            "path": path,
            "language": language,
            "loc": loc,
            "component": component_for(path),
        }
        if reason:
            exclusions.append({**record, "reason": reason})
            continue
        sources.append(record)
        language_counts[language] += 1
        component_counts[record["component"]] += 1

    sources.sort(key=lambda item: item["path"])
    exclusions.sort(key=lambda item: (item["reason"], item["path"]))
    return {
        "schema_version": 1,
        "snapshot": {"requested": snapshot, "commit": commit},
        "loc_definition": "Nonblank physical source lines; comments are included.",
        "languages": {
            name: {"files": language_counts[name], "loc": sum(x["loc"] for x in sources if x["language"] == name)}
            for name in sorted(language_counts)
        },
        "components": {
            name: {
                "files": component_counts[name],
                "loc": sum(x["loc"] for x in sources if x["component"] == name),
            }
            for name in sorted(component_counts)
        },
        "configured_checks": configured_checks(root, commit),
        "source_files": sources,
        "excluded_sources": exclusions,
        "exclusion_totals": {
            "generated": sum(item["reason"] == "generated" for item in exclusions),
            "test": sum(item["reason"] == "test" for item in exclusions),
        },
    }


def main() -> int:
    """Write the inventory JSON for the selected Git snapshot."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default="f010640", help="Git commit or ref (default: f010640)")
    parser.add_argument(
        "--output",
        default=".review/index/repository.json",
        help="output path relative to the repository root",
    )
    arguments = parser.parse_args()
    try:
        root = Path(run_git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
        inventory = build_inventory(root, arguments.snapshot)
        output = (root / arguments.output).resolve()
        output.relative_to(root.resolve())
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        print(f"inventory failed: {error}", file=sys.stderr)
        return 1
    print(f"Wrote {arguments.output}: {len(inventory['source_files'])} source files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
