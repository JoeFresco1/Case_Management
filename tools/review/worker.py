"""Run isolated Codex reviews for immutable pilot units and import validated results."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.review import store, units

REVIEW = ROOT / ".review"
PILOT_UNITS = REVIEW / "units" / "pilot"
WORKER_OUTPUTS = REVIEW / "workers" / "pilot"
WORKER_LOGS = REVIEW / "logs" / "pilot"
WORKER_REQUESTS = REVIEW / "requests" / "pilot"
ATTEMPT_OUTPUTS = REVIEW / "runs" / "pilot"
DEFAULT_TIMEOUT_SECONDS = 900
MAX_ATTEMPTS = 2
UNIT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


class WorkerError(RuntimeError):
    """Raised when a pilot unit cannot be run or verified safely."""


def _utc_now() -> str:
    """Return a timezone-aware UTC timestamp suitable for request logs."""
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, value: Any) -> None:
    """Atomically write JSON under the review artifact tree."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_unit(unit_id: str) -> dict[str, Any]:
    """Load a generated pilot unit and ensure its id and snapshot are well-formed."""
    if not UNIT_ID.fullmatch(unit_id):
        raise WorkerError(f"invalid unit id: {unit_id!r}")
    path = PILOT_UNITS / f"{unit_id}.json"
    try:
        unit = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkerError(f"cannot read pilot unit {unit_id}: {error}") from error
    if not isinstance(unit, dict) or unit.get("unit_id") != unit_id:
        raise WorkerError(f"pilot unit file has an unexpected id: {path}")
    if unit.get("schema_version") != units.UNIT_SCHEMA_VERSION:
        raise WorkerError(f"unsupported pilot unit schema for {unit_id}")
    if not re.fullmatch(r"[0-9a-f]{40}", str(unit.get("snapshot_commit", ""))):
        raise WorkerError(f"pilot unit {unit_id} has no full snapshot commit")
    return unit


def _prompt(unit: dict[str, Any]) -> str:
    """Build a review-only prompt that constrains findings to the pilot scope."""
    return "\n".join(
        (
            "Perform a static code review for the immutable Git snapshot identified below.",
            "Do not edit, create, delete, or format any file. Do not run code or tests.",
            (
                "Read only the exact production and linked test paths listed in the unit JSON, "
                "using `git show <snapshot_commit>:<path>` so working-tree contents are ignored."
            ),
            (
                "Report only actionable defects supported by concrete source evidence. Do not "
                "invent usage metrics, requests, or repository behavior."
            ),
            "Return exactly one JSON object conforming to the supplied output schema.",
            (
                "Set schema_version to 1, snapshot_commit to the exact commit below, and worker_id "
                "to the exact unit_id below. Include coverage for every production file in scope."
            ),
            "Finding locations must use repository-relative POSIX paths and snapshot line numbers.",
            (
                "For each finding set finding_id to F-000000000000000000000000 and "
                "duplicate_of to null; the worker computes stable finding IDs and a later "
                "verifier handles duplicates. Set line_end to null for a single-line location. "
                "Include note in every coverage entry (empty string is allowed), and include "
                "evidence and verifier_note in every finding (use [] and empty string when absent)."
            ),
            "Pilot unit JSON:",
            json.dumps(unit, ensure_ascii=False, sort_keys=True),
        )
    )


def _command(output_path: Path, prompt: str) -> list[str]:
    """Return the read-only Codex CLI command for one attempt."""
    return [
        "codex",
        "exec",
        "--model",
        "gpt-6-luna",
        "-c",
        'model_reasoning_effort="medium"',
        "--sandbox",
        "read-only",
        "--output-schema",
        str((ROOT / "tools" / "review" / "result.schema.json").resolve()),
        "--output-last-message",
        str(output_path.resolve()),
        "--json",
        "--cd",
        str(ROOT),
        prompt,
    ]


def _observed_usage(log_text: str) -> list[dict[str, Any]]:
    """Extract usage-bearing Codex JSON events without deriving absent metrics."""
    observed: list[dict[str, Any]] = []
    for line in log_text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        info = event.get("info")
        has_usage_event = event_type in {
            "token_count",
            "usage",
            "request_started",
            "request_completed",
        }
        has_usage_info = isinstance(info, dict) and any(
            "usage" in key or "token" in key for key in info
        )
        if has_usage_event or has_usage_info:
            observed.append(event)
    return observed


def _validate_output(path: Path, unit: dict[str, Any]) -> dict[str, Any]:
    """Parse and validate the model response before it is made importable."""
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkerError(
            f"malformed or missing model JSON for {unit['unit_id']}: {error}"
        ) from error
    inventory_commit, inventory_files = store._load_inventory(store.DEFAULT_INVENTORY)
    if inventory_commit != unit["snapshot_commit"]:
        raise WorkerError(
            f"pilot snapshot {unit['snapshot_commit']} does not match inventory {inventory_commit}"
        )
    # The model supplies evidence and location; the worker owns the stable hash.
    for finding in result.get("findings", []) if isinstance(result, dict) else []:
        if not isinstance(finding, dict):
            continue
        location = finding.get("location")
        if not isinstance(location, dict):
            continue
        rule_id, title = finding.get("rule_id"), finding.get("title")
        path, line_start = location.get("path"), location.get("line_start")
        line_end = location.get("line_end")
        if (
            isinstance(rule_id, str)
            and isinstance(title, str)
            and isinstance(path, str)
            and type(line_start) is int
            and (line_end is None or type(line_end) is int)
        ):
            finding["finding_id"] = store.stable_finding_id(
                unit["snapshot_commit"], rule_id, path, line_start, line_end, title
            )
    try:
        validated = store.validate_result(result, inventory_files)
    except store.InvalidResult as error:
        raise WorkerError(f"model result failed schema validation: {error}") from error
    if validated["snapshot_commit"] != unit["snapshot_commit"]:
        raise WorkerError("model result snapshot does not match its pilot unit")
    if validated["worker_id"] != unit["unit_id"]:
        raise WorkerError("model result worker_id does not match its pilot unit")
    required_files = {item["path"] for item in unit["scope"]["files"]}
    reported_files = {item["path"] for item in validated["coverage"]["files"]}
    missing = required_files - reported_files
    if missing:
        raise WorkerError(
            f"model result omits pilot file coverage: {', '.join(sorted(missing))}"
        )
    return validated


def _invoke_codex(
    command: list[str], timeout_seconds: float
) -> tuple[str, float, int | None, bool, str, str | None]:
    """Run Codex with a timeout and return raw observable process data."""
    started_at = _utc_now()
    started = time.monotonic()
    timed_out = False
    exit_code: int | None = None
    error_message: str | None = None
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        exit_code = completed.returncode
        log_text = completed.stdout
    except subprocess.TimeoutExpired as error:
        timed_out = True
        raw_output = error.stdout or ""
        log_text = (
            raw_output.decode("utf-8", errors="replace")
            if isinstance(raw_output, bytes)
            else raw_output
        )
        error_message = f"exceeded {timeout_seconds:.3f}s timeout"
    except OSError as error:
        log_text = f"Unable to start Codex: {error}\n"
        error_message = str(error)
    duration = round(time.monotonic() - started, 3)
    return started_at, duration, exit_code, timed_out, log_text, error_message


def _execute_attempt(
    unit: dict[str, Any], attempt_number: int, timeout_seconds: float
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run one Codex attempt, retain its log, and validate its temporary output."""
    unit_id = unit["unit_id"]
    attempt_id = f"attempt-{attempt_number:02d}"
    attempt_output = ATTEMPT_OUTPUTS / unit_id / f"{attempt_id}.json"
    log_path = WORKER_LOGS / f"{unit_id}.{attempt_id}.log"
    attempt_output.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    attempt_output.unlink(missing_ok=True)
    command = _command(attempt_output, _prompt(unit))
    started_at, elapsed, exit_code, timed_out, log_text, error_message = _invoke_codex(
        command, timeout_seconds
    )
    log_path.write_text(log_text, encoding="utf-8")
    attempt: dict[str, Any] = {
        "attempt": attempt_number,
        "started_at": started_at,
        "duration_seconds": elapsed,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "log_file": log_path.relative_to(ROOT).as_posix(),
        "request": {
            "executable": "codex",
            "operation": "exec",
            "model": "gpt-6-luna",
            "reasoning_effort": "medium",
            "sandbox": "read-only",
            "output_schema": "tools/review/result.schema.json",
            "output_last_message": attempt_output.relative_to(ROOT).as_posix(),
            "json_events": True,
        },
        "observed_usage_events": _observed_usage(log_text),
    }
    if error_message:
        attempt["error"] = error_message
    try:
        if error_message or exit_code != 0:
            raise WorkerError(error_message or f"Codex exited with status {exit_code}")
        result = _validate_output(attempt_output, unit)
    except WorkerError as error:
        attempt["status"] = "rejected"
        attempt["error"] = str(error)
        result = None
    else:
        attempt["status"] = "accepted"
    finally:
        attempt_output.unlink(missing_ok=True)
    return attempt, result


def run_unit(unit_id: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> int:
    """Run Codex for one unit with bounded retries and isolated artifact files."""
    if timeout_seconds < 1:
        raise WorkerError("timeout must be at least one second")
    unit = _load_unit(unit_id)
    final_output = WORKER_OUTPUTS / f"{unit_id}.json"
    request_log_path = WORKER_REQUESTS / f"{unit_id}.json"
    request: dict[str, Any] = {
        "unit_id": unit_id,
        "snapshot_commit": unit["snapshot_commit"],
        "model": "gpt-6-luna",
        "reasoning_effort": "medium",
        "sandbox": "read-only",
        "timeout_seconds": timeout_seconds,
        "max_attempts": MAX_ATTEMPTS,
        "attempts": [],
    }
    deadline = time.monotonic() + timeout_seconds
    for attempt_number in range(1, MAX_ATTEMPTS + 1):
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            break
        attempt, result = _execute_attempt(unit, attempt_number, remaining_seconds)
        request["attempts"].append(attempt)
        if result is not None:
            # Only schema-checked output is written to the importable result path.
            _write_json(final_output, result)
            _write_json(request_log_path, request)
            print(f"accepted {unit_id}: {final_output.relative_to(ROOT).as_posix()}")
            return 0
        _write_json(request_log_path, request)
        print(
            f"attempt {attempt_number}/{MAX_ATTEMPTS} rejected for {unit_id}: {attempt['error']}",
            file=sys.stderr,
        )
    final_output.unlink(missing_ok=True)
    return 2


def verify_pilot() -> int:
    """Validate all pilot artifacts, then import them into SQLite serially."""
    validated: list[tuple[Path, dict[str, Any]]] = []
    try:
        for pilot in units.PILOTS:
            unit_id = pilot["id"]
            unit = _load_unit(unit_id)
            output = WORKER_OUTPUTS / f"{unit_id}.json"
            result = _validate_output(output, unit)
            validated.append((output, result))

        # SQLite permits only one writer here; imports intentionally stay in this loop.
        for output, _result in validated:
            store.import_result(output, store.DEFAULT_INVENTORY, store.DEFAULT_DATABASE)
    except (
        WorkerError,
        store.InvalidResult,
        OSError,
        ValueError,
        sqlite3.Error,
    ) as error:
        print(f"pilot verification failed: {error}", file=sys.stderr)
        return 2
    print(f"pilot verification imported: {len(validated)} unit results")
    return 0


def main() -> int:
    """Dispatch one-unit review runs and serial pilot verification/import."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="review one pilot unit")
    run_parser.add_argument("--unit", required=True, help="pilot unit id")
    run_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    verify_parser = commands.add_parser(
        "verify", help="validate and import completed pilot outputs"
    )
    verify_parser.add_argument("--pilot", action="store_true", required=True)
    args = parser.parse_args()
    os.chdir(ROOT)
    try:
        if args.command == "run":
            return run_unit(args.unit, args.timeout)
        if args.command == "verify":
            return verify_pilot()
    except (WorkerError, OSError, ValueError, sqlite3.Error) as error:
        print(f"review worker failed: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
