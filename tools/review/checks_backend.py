#!/usr/bin/env python3
"""Run configured backend quality checks and persist resumable baseline results."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REVIEW = ROOT / ".review"
RUNS = REVIEW / "runs" / "backend"
BASELINE = REVIEW / "baseline" / "backend.json"
KNOWN_WARNING = (
    "Earlier full-suite runs recorded an order-dependent PytestUnraisableExceptionWarning "
    "from aiosqlite.Connection.__del__ (ResourceWarning: connection deleted before being "
    "closed), promoted to an error by filterwarnings = ['error']; see "
    "docs/code-reviews/phase_1_code_review.md and specs/002-leftovers/architecture-runC.md."
)
AIOSQLITE_WARNING = re.compile(
    r"PytestUnraisableExceptionWarning|aiosqlite\.Connection\.__del__|"
    r"connection deleted before being closed",
    re.IGNORECASE,
)


def canonical_json(value: Any) -> str:
    """Serialize JSON in a stable, human-readable form."""
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def atomic_json(path: Path, value: Any) -> None:
    """Write JSON atomically so interrupted runs leave the prior state intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(canonical_json(value), encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def check_commands() -> list[dict[str, Any]]:
    """Read configured check paths and return the fixed backend check sequence."""
    config = tomllib.loads((ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8"))
    pytest_config = config.get("tool", {}).get("pytest", {}).get("ini_options", {})
    coverage_fail_under = config.get("tool", {}).get("coverage", {}).get("report", {}).get("fail_under")
    tests_db_url = "sqlite+aiosqlite:///../.review/backend-tests.sqlite3"
    return [
        {"id": "ruff_check", "command": ["uv", "run", "ruff", "check", "."]},
        {"id": "ruff_format", "command": ["uv", "run", "ruff", "format", "--check", "."]},
        {
            "id": "mypy_strict",
            "command": ["uv", "run", "mypy", "--strict", "app/domain", "app/application"],
        },
        {"id": "import_linter", "command": ["uv", "run", "lint-imports"]},
        {
            "id": "pytest_sqlite",
            "command": ["uv", "run", "pytest", "-q"],
            "environment": {
                "APP_ENV": "test",
                "DATABASE_URL": tests_db_url,
            },
            "configured_coverage_fail_under": coverage_fail_under,
            "pytest_addopts": pytest_config.get("addopts", []),
        },
    ]


def run_fingerprint(commands: list[dict[str, Any]]) -> str:
    """Identify a run by the checked-in backend configuration and exact commands."""
    digest = hashlib.sha256((ROOT / "backend" / "pyproject.toml").read_bytes()).hexdigest()
    identity = {"snapshot": "f0106405bf0fe4abddb3da4057e05e64f9ed1c72", "config_sha256": digest, "checks": commands}
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()[:20]


def extract_result(check: dict[str, Any], exit_status: int, duration_seconds: float, log_text: str) -> dict[str, Any]:
    """Normalize command output into concise diagnostics without changing raw logs."""
    lines = [line.rstrip() for line in log_text.splitlines()]
    summary_lines = [
        line for line in lines
        if re.search(r"\b(passed|failed|errors?|violations?|found|success|no issues)\b", line, re.I)
    ]
    failures = [
        line.strip() for line in lines
        if re.search(
            r"\b(error|failed|failure|violation|traceback|PermissionError|FileExistsError)\b|"
            r"Pytest\w*Warning",
            line,
            re.I,
        )
    ]
    failures = [line.replace(str(ROOT), "<repo>") for line in failures]
    if check["id"] == "pytest_sqlite":
        error_markers = sum(
            line.count("E") + line.count("F")
            for line in re.findall(r"(?m)^([.EFs]+)\s+\[\s*\d+%\]", log_text)
        )
        if error_markers:
            failures.insert(0, f"pytest progress reported {error_markers} error/failure marker(s).")
    if exit_status and not failures:
        failures = [f"Command exited with status {exit_status}; inspect raw log."]
    result: dict[str, Any] = {
        "id": check["id"],
        "command": " ".join(check["command"]),
        "working_directory": "backend",
        "exit_status": exit_status,
        "status": "passed" if exit_status == 0 else "failed",
        "duration_seconds": round(duration_seconds, 3),
        "failures": failures,
        "summary_lines": summary_lines[-20:],
    }
    if "environment" in check:
        result["environment"] = check["environment"]
    if check["id"] == "pytest_sqlite":
        total_match = re.findall(r"(?m)^TOTAL\s+\d+\s+\d+\s+(\d+)%\s*$", log_text)
        result["coverage_percent"] = int(total_match[-1]) if total_match else None
        result["coverage_fail_under"] = check.get("configured_coverage_fail_under")
        result["aiosqlite_warning_observed"] = bool(AIOSQLITE_WARNING.search(log_text))
    return result


def execute_check(check: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Run one check and persist its raw combined log and normalized result."""
    log_path = run_dir / f"{check['id']}.log"
    started = time.monotonic()
    environment = os.environ.copy()
    environment.update(check.get("environment", {}))
    environment["UV_CACHE_DIR"] = (REVIEW / "uv-cache").as_posix()
    with log_path.open("w", encoding="utf-8", newline="\n") as log_file:
        log_file.write(f"$ {' '.join(check['command'])}\n")
        log_file.write("cwd: backend\n\n")
        log_file.flush()
        process = subprocess.run(
            check["command"],
            cwd=ROOT / "backend",
            env=environment,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            check=False,
        )
    duration = time.monotonic() - started
    result = extract_result(check, process.returncode, duration, log_path.read_text(encoding="utf-8", errors="replace"))
    result["raw_log"] = log_path.relative_to(ROOT).as_posix()
    return result


def main() -> int:
    """Run configured checks, resuming completed matching checks by default."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rerun", action="store_true", help="rerun checks even when a matching result exists")
    parser.add_argument("--only", choices=["ruff_check", "ruff_format", "mypy_strict", "import_linter", "pytest_sqlite"])
    arguments = parser.parse_args()

    commands = check_commands()
    fingerprint = run_fingerprint(commands)
    run_dir = RUNS / fingerprint
    state_path = run_dir / "state.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        prior = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except json.JSONDecodeError:
        prior = {}
    results = prior.get("results", {}) if prior.get("fingerprint") == fingerprint else {}

    for check in commands:
        if arguments.only and check["id"] != arguments.only:
            continue
        saved = results.get(check["id"])
        if not arguments.rerun and saved and saved.get("command") == " ".join(check["command"]):
            log_path = ROOT / saved.get("raw_log", "")
            if log_path.is_file():
                refreshed = extract_result(
                    check,
                    int(saved["exit_status"]),
                    float(saved["duration_seconds"]),
                    log_path.read_text(encoding="utf-8", errors="replace"),
                )
                refreshed["raw_log"] = saved["raw_log"]
                for field in ("coverage_percent", "coverage_source"):
                    if refreshed.get(field) is None and saved.get(field) is not None:
                        refreshed[field] = saved[field]
                results[check["id"]] = refreshed
            print(f"RESUME {check['id']}: {saved['status']} (log: {saved['raw_log']})", flush=True)
            continue
        print(f"RUN {check['id']}: {' '.join(check['command'])}", flush=True)
        try:
            result = execute_check(check, run_dir)
        except OSError as error:
            result = extract_result(check, 127, 0.0, f"Unable to start command: {error}\n")
            result["raw_log"] = (run_dir / f"{check['id']}.log").relative_to(ROOT).as_posix()
        results[check["id"]] = result
        atomic_json(state_path, {"schema_version": 1, "fingerprint": fingerprint, "results": results})
        print(f"DONE {check['id']}: {result['status']} ({result['duration_seconds']:.3f}s)", flush=True)

    ordered_results = [results[key] for key in sorted(results)]
    atomic_json(state_path, {"schema_version": 1, "fingerprint": fingerprint, "results": results})
    baseline = {
        "schema_version": 1,
        "snapshot": {"requested": "f010640", "commit": "f0106405bf0fe4abddb3da4057e05e64f9ed1c72"},
        "configuration": "backend/pyproject.toml",
        "checks": ordered_results,
        "known_warnings": [
            {
                "id": "aiosqlite_unclosed_connection",
                "historical_context": KNOWN_WARNING,
                "observed_this_run": next(
                    (item.get("aiosqlite_warning_observed", False) for item in ordered_results if item["id"] == "pytest_sqlite"),
                    None,
                ),
            }
        ],
    }
    atomic_json(BASELINE, baseline)
    print(f"Wrote {BASELINE.relative_to(ROOT).as_posix()}")
    if arguments.only:
        return 0
    return 1 if any(item["status"] != "passed" for item in ordered_results) or len(ordered_results) != len(commands) else 0


if __name__ == "__main__":
    raise SystemExit(main())
