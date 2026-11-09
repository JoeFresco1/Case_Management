#!/usr/bin/env python3
"""Run configured frontend checks and save a deterministic, resumable baseline.

The runner uses repository manifests and the pinned review inventory to decide
which checks are valid. Logs and the normalized baseline stay under ``.review``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INVENTORY = Path(".review/index/repository.json")
BASELINE_PATH = Path(".review/baseline/frontend.json")
LOG_DIRECTORY = Path(".review/logs/frontend")
CHECK_SCRIPTS = (("typecheck", "typecheck"), ("build", "build"), ("vitest", "test"), ("lint", "lint"))
ESLINT_CONFIG_NAMES = (
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yaml",
    ".eslintrc.yml",
)
RUN_ENVIRONMENT = {
    "CI": "1",
    "FORCE_COLOR": "0",
    "NO_COLOR": "1",
    "TZ": "UTC",
    "npm_config_audit": "false",
    "npm_config_fund": "false",
    "npm_config_update_notifier": "false",
}


def sha256(data: bytes) -> str:
    """Return the stable lowercase SHA-256 digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> bytes:
    """Encode JSON with stable key order and whitespace for fingerprinting."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def write_json(path: Path, value: Any) -> None:
    """Atomically write deterministic, indented UTF-8 JSON to a workspace path."""
    destination = REPO_ROOT / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)


def read_json(path: Path) -> dict[str, Any] | None:
    """Read an existing JSON object, returning None when absent or malformed."""
    try:
        value = json.loads((REPO_ROOT / path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def git_commit() -> str:
    """Resolve the current checkout commit for baseline provenance."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, check=False
    )
    if result.returncode:
        raise RuntimeError("cannot resolve current Git commit")
    return result.stdout.decode("ascii").strip()


def input_hashes(inventory_path: Path) -> dict[str, str]:
    """Hash all manifests and frontend configuration relevant to check selection."""
    paths = {
        inventory_path.as_posix(),
        "tools/review/checks_frontend.py",
        ".github/workflows/ci.yml",
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend/pnpm-lock.yaml",
        "frontend/tsconfig.json",
        "frontend/tsconfig.app.json",
        "frontend/tsconfig.node.json",
        "frontend/vite.config.ts",
        "frontend/vitest.config.ts",
    }
    for parent in (Path("."), Path("frontend")):
        for name in ESLINT_CONFIG_NAMES:
            paths.add((parent / name).as_posix())
    digests: dict[str, str] = {}
    for relative in sorted(paths):
        source = REPO_ROOT / relative
        if source.is_file():
            digests[relative] = sha256(source.read_bytes())
    return digests


def eslint_configuration(package: dict[str, Any]) -> tuple[bool, list[str]]:
    """Find ESLint configuration files or package-level configuration."""
    found = [
        relative
        for relative in (name for parent in (".", "frontend") for name in ESLINT_CONFIG_NAMES)
        if (REPO_ROOT / relative).is_file()
    ]
    if package.get("eslintConfig"):
        found.append("frontend/package.json#eslintConfig")
    return bool(found), sorted(found)


def tool_version(command: str) -> str | None:
    """Read a tool version without emitting it into user-visible output."""
    executable = shutil.which(command)
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "--version"],
            cwd=REPO_ROOT / "frontend",
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return result.stdout.decode("utf-8", errors="replace").strip().splitlines()[0]


def load_prior_results() -> dict[str, dict[str, Any]]:
    """Load prior check records to support safe per-command resumption."""
    previous = read_json(BASELINE_PATH)
    checks = previous.get("checks", {}) if previous else {}
    return checks if isinstance(checks, dict) else {}


def can_resume(prior: dict[str, Any] | None, fingerprint: str) -> bool:
    """Accept a prior result only when its fingerprint and both logs still match."""
    if not prior or prior.get("fingerprint") != fingerprint or prior.get("status") != "completed":
        return False
    for stream in ("stdout", "stderr"):
        relative = prior.get(f"{stream}_path")
        expected = prior.get(f"{stream}_sha256")
        if not isinstance(relative, str) or not isinstance(expected, str):
            return False
        try:
            if sha256((REPO_ROOT / relative).read_bytes()) != expected:
                return False
        except OSError:
            return False
    return True


def run_check(
    name: str,
    script: str,
    executable: str,
    inputs: dict[str, str],
    snapshot_commit: str,
    versions: dict[str, str | None],
    previous: dict[str, Any] | None,
    force: bool,
    timeout: int,
) -> dict[str, Any]:
    """Run one npm script or reuse its verified prior result."""
    command = ["npm", "run", script]
    provenance = {
        "command": command,
        "cwd": "frontend",
        "environment": RUN_ENVIRONMENT,
        "inputs_sha256": inputs,
        "snapshot_commit": snapshot_commit,
        "tool_versions": versions,
        "timeout_seconds": timeout,
    }
    fingerprint = sha256(canonical_json(provenance))
    if not force and can_resume(previous, fingerprint):
        print(f"{name}: resumed from matching completed result")
        return previous

    log_root = REPO_ROOT / LOG_DIRECTORY
    log_root.mkdir(parents=True, exist_ok=True)
    stdout_path = LOG_DIRECTORY / f"{name}.stdout.log"
    stderr_path = LOG_DIRECTORY / f"{name}.stderr.log"
    environment = os.environ.copy()
    environment.update(RUN_ENVIRONMENT)
    status = "completed"
    try:
        result = subprocess.run(
            f'npm run "{script}"',
            cwd=REPO_ROOT / "frontend",
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            shell=True,
        )
        stdout = result.stdout
        stderr = result.stderr
        exit_code: int | None = result.returncode
    except subprocess.TimeoutExpired as error:
        status = "timed_out"
        stdout = error.stdout if isinstance(error.stdout, bytes) else b""
        stderr = error.stderr if isinstance(error.stderr, bytes) else b""
        exit_code = None
    except OSError as error:
        status = "unavailable"
        stdout = b""
        stderr = f"{type(error).__name__}: {error}\n".encode("utf-8", errors="replace")
        exit_code = None

    (REPO_ROOT / stdout_path).write_bytes(stdout)
    (REPO_ROOT / stderr_path).write_bytes(stderr)
    return {
        **provenance,
        "fingerprint": fingerprint,
        "status": status,
        "exit_code": exit_code,
        "stdout_path": stdout_path.as_posix(),
        "stdout_sha256": sha256(stdout),
        "stderr_path": stderr_path.as_posix(),
        "stderr_sha256": sha256(stderr),
    }


def build_plan(
    package: dict[str, Any], inventory: dict[str, Any], ci_text: str, eslint_found: bool
) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
    """Select configured commands and explicit skips from manifests and CI."""
    scripts = package.get("scripts", {})
    frontend_ci = ci_text.split("  frontend:", 1)[-1] if "  frontend:" in ci_text else ""
    inventory_scripts = (
        inventory.get("configured_checks", {}).get("frontend", {}).get("scripts", {})
    )
    commands: list[tuple[str, str]] = []
    skipped: list[dict[str, str]] = []
    for name, script in CHECK_SCRIPTS:
        if name == "lint" and not eslint_found:
            skipped.append({"check": name, "reason": "missing_eslint_config"})
            continue
        if not isinstance(scripts, dict) or not isinstance(scripts.get(script), str):
            skipped.append({"check": name, "reason": "missing_package_script"})
            continue
        if not isinstance(inventory_scripts, dict) or script not in inventory_scripts:
            skipped.append({"check": name, "reason": "not_in_pinned_inventory"})
            continue
        if name in {"typecheck", "build"} and not (REPO_ROOT / "frontend/tsconfig.json").is_file():
            skipped.append({"check": name, "reason": "missing_typescript_config"})
            continue
        if name in {"build", "vitest"} and not (REPO_ROOT / "frontend/vite.config.ts").is_file():
            skipped.append({"check": name, "reason": "missing_vite_config"})
            continue
        if name == "vitest" and "vitest" not in {
            *package.get("devDependencies", {}).keys(),
            *package.get("dependencies", {}).keys(),
        }:
            skipped.append({"check": name, "reason": "missing_vitest_dependency"})
            continue
        if name in {"typecheck", "build"} and f"npm run {script}" not in frontend_ci:
            skipped.append({"check": name, "reason": "not_configured_in_ci"})
            continue
        commands.append((name, script))
    return commands, skipped


def main() -> int:
    """Run selected frontend checks and write their normalized baseline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--timeout", type=int, default=1800, help="per-check timeout in seconds")
    parser.add_argument("--force", action="store_true", help="rerun completed checks")
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    inventory_path = args.inventory
    if inventory_path.is_absolute():
        try:
            inventory_path = inventory_path.resolve().relative_to(REPO_ROOT)
        except ValueError:
            parser.error("--inventory must be inside the repository")
    package_path = REPO_ROOT / "frontend/package.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
        inventory = json.loads((REPO_ROOT / inventory_path).read_text(encoding="utf-8"))
        ci_text = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as error:
        print(f"frontend check setup failed: {error}", file=sys.stderr)
        return 2
    if not isinstance(package, dict) or not isinstance(inventory, dict):
        print("frontend check setup failed: expected JSON objects", file=sys.stderr)
        return 2

    eslint_found, eslint_configs = eslint_configuration(package)
    commands, skipped = build_plan(package, inventory, ci_text, eslint_found)
    npm_path = shutil.which("npm")
    if not npm_path:
        unavailable = [{"check": name, "reason": "npm_unavailable"} for name, _ in commands]
        skipped.extend(unavailable)
        commands = []
    npm = npm_path or "npm"
    versions = {"node": tool_version("node"), "npm": tool_version("npm")}
    hashes = input_hashes(inventory_path)
    snapshot_commit = inventory.get("snapshot", {}).get("commit")
    if not isinstance(snapshot_commit, str):
        print("frontend check setup failed: inventory has no snapshot commit", file=sys.stderr)
        return 2
    if git_commit() != snapshot_commit:
        print("frontend check setup failed: checkout does not match inventory snapshot", file=sys.stderr)
        return 2

    prior = load_prior_results()
    checks: dict[str, dict[str, Any]] = {}
    for name, script in commands:
        checks[name] = run_check(
            name,
            script,
            npm,
            hashes,
            snapshot_commit,
            versions,
            prior.get(name),
            args.force,
            args.timeout,
        )
    baseline = {
        "schema_version": 1,
        "snapshot": {"commit": snapshot_commit},
        "inputs_sha256": hashes,
        "tool_versions": versions,
        "checks": checks,
        "skipped": sorted(skipped, key=lambda item: item["check"]),
        "eslint": {
            "configured": eslint_found,
            "configurations": eslint_configs,
            "reason": None if eslint_found else "missing_eslint_config",
        },
    }
    write_json(BASELINE_PATH, baseline)
    failed = any(
        item.get("status") != "completed" or item.get("exit_code") != 0
        for item in checks.values()
    )
    print(f"frontend baseline: {BASELINE_PATH.as_posix()}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
