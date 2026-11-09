"""Execute the pinned, read-only full review plan through lightweight-dag."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.review import store

REVIEW = ROOT / ".review"
PLAN = REVIEW / "plan" / "full-dag.json"
UNITS = REVIEW / "units" / "full"
OUTPUTS = REVIEW / "workers" / "full"
LOGS = REVIEW / "logs" / "full"
RUNS = REVIEW / "runs" / "full"
SCHEMA = ROOT / "tools" / "review" / "result.schema.json"
MODEL = "gpt-6-luna"
EFFORT = "medium"


def read_json(path: Path) -> dict[str, Any]:
    """Read one local review artifact."""
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    """Atomically save a review artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def edge_key(edge: dict[str, str]) -> str:
    """Give a graph edge a short, stable coverage key."""
    payload = json.dumps(edge, sort_keys=True, separators=(",", ":"))
    return "edge-" + hashlib.sha256(payload.encode()).hexdigest()[:20]


def unit_for(task: dict[str, Any], snapshot: str) -> dict[str, Any]:
    """Make an executable review unit from a planned agent node."""
    scope = task["scope"]
    return {
        "schema_version": 1,
        "unit_id": task["id"],
        "title": task["title"],
        "phase": task["phase"],
        "snapshot_commit": snapshot,
        "depends_on": task["deps"],
        "scope": {
            "files": scope["files"],
            "edges": [{"key": edge_key(edge), "edge": edge} for edge in scope["edges"]],
            "finding_id": scope.get("finding_id"),
        },
    }


def prompt_for(unit: dict[str, Any]) -> str:
    """Constrain Luna to pinned source and structured, actionable findings."""
    instructions = [
        "Perform a static code review of the immutable Git commit in the unit JSON.",
        "Do not edit, create, delete, or format source files. Do not run code or tests.",
        "Read scoped source using git show <snapshot_commit>:<path>; ignore working-tree source.",
        "Read predecessor artifacts under .review/workers/full only when they help trace this scope.",
        "Return exactly one JSON object matching tools/review/result.schema.json.",
        "Use schema_version 1, the exact snapshot_commit and unit_id as worker_id.",
        "Include a coverage.files entry for EVERY scoped file, and coverage.edges for EVERY scoped edge key.",
        "For subsystem tasks include coverage.subsystems with key unit_id; for e2e tasks include coverage.flows with key unit_id.",
        "Use reviewed only when you examined the item; otherwise use partial or blocked with a specific note.",
        "Report actionable defects with source-line evidence. Set new finding_id to F-000000000000000000000000; the runner replaces it with a stable ID.",
        "Set new findings' verifier_status to unverified, verifier_note to an empty string, and duplicate_of to null.",
        "Every finding needs evidence and a location with line_end null for a single line.",
        "Product decision: when live role assignments are configured, they are authoritative; revocation removes a built-in role's permissions on the next request even if the identity claim still names it.",
    ]
    finding_id = unit["scope"].get("finding_id")
    if finding_id:
        report = read_json(REVIEW / "report.json")
        finding = next((f for f in report["findings"] if f["finding_id"] == finding_id), None)
        if finding is None:
            raise ValueError(f"unknown pilot finding: {finding_id}")
        instructions.extend(
            [
                "Independently assess this existing finding. Include it in findings with its exact identity fields (rule_id, title, location) and finding_id.",
                "Set verifier_status to confirmed, rejected, or needs_context and explain in verifier_note.",
                "Live role assignments are authoritative when configured; revoking a built-in role must remove its permissions on the next request.",
                "Existing finding: " + json.dumps(finding, sort_keys=True),
            ]
        )
    return "\n".join(instructions + ["Review unit: " + json.dumps(unit, sort_keys=True)])


def validate_output(result: Any, unit: dict[str, Any]) -> dict[str, Any]:
    """Check schema, pinned identity, and full scoped coverage."""
    inventory_commit, inventory_files = store._load_inventory(store.DEFAULT_INVENTORY)
    if inventory_commit != unit["snapshot_commit"]:
        raise ValueError("review unit and inventory snapshots differ")
    # Keep omissions visible and machine-readable instead of either rejecting
    # a useful partial review or silently claiming the omitted scope was read.
    if isinstance(result, dict) and isinstance(result.get("coverage"), dict):
        coverage = result["coverage"]
        for kind, key_name, expected in (
            ("files", "path", unit["scope"]["files"]),
            ("edges", "key", [row["key"] for row in unit["scope"]["edges"]]),
        ):
            rows = coverage.get(kind)
            if isinstance(rows, list):
                present = {row.get(key_name) for row in rows if isinstance(row, dict)}
                rows.extend(
                    {key_name: key, "status": "partial", "note": "Worker omitted this scoped item; it was not counted as reviewed."}
                    for key in expected if key not in present
                )
    finding_id = unit["scope"].get("finding_id")
    if finding_id and isinstance(result, dict):
        # A verifier's decision applies to the exact finding supplied in its
        # prompt. Preserve that finding's stable identity and copy only the
        # verifier's verdict/note from the model response.
        report = read_json(REVIEW / "report.json")
        original = next((f for f in report["findings"] if f["finding_id"] == finding_id), None)
        if original is None:
            raise ValueError(f"verifier target is absent from report: {finding_id}")
        result["worker_id"] = unit["unit_id"]
        candidates = [f for f in result.get("findings", [])
                      if isinstance(f, dict) and
                      (f.get("rule_id") == original["rule_id"] or f.get("title") == original["title"])]
        if len(candidates) != 1:
            raise ValueError(f"verifier response did not identify exactly one target: {finding_id}")
        response = candidates[0]
        verdict = response.get("verifier_status")
        note = response.get("verifier_note", "")
        if verdict not in {"confirmed", "rejected", "needs_context"}:
            raise ValueError(f"verifier response has no valid verdict for {finding_id}")
        fields = ("finding_id", "rule_id", "severity", "title", "detail", "location", "evidence", "duplicate_of")
        preserved = {key: json.loads(json.dumps(original[key])) for key in fields if key in original}
        preserved["verifier_status"] = verdict
        preserved["verifier_note"] = note
        result["findings"] = [preserved] + [f for f in result["findings"] if f is not response]
    if isinstance(result, dict):
        for finding in result.get("findings", []):
            if not isinstance(finding, dict) or not isinstance(finding.get("location"), dict):
                continue
            location = finding["location"]
            if all(isinstance(finding.get(k), str) for k in ("rule_id", "title")) and isinstance(location.get("path"), str) and type(location.get("line_start")) is int:
                finding["finding_id"] = store.stable_finding_id(
                    unit["snapshot_commit"], finding["rule_id"], location["path"],
                    location["line_start"], location.get("line_end"), finding["title"],
                )
    valid = store.validate_result(result, inventory_files)
    if valid["snapshot_commit"] != unit["snapshot_commit"] or valid["worker_id"] != unit["unit_id"]:
        raise ValueError("worker returned the wrong snapshot or unit ID")
    expected_files = set(unit["scope"]["files"])
    reported_files = {row["path"] for row in valid["coverage"]["files"]}
    expected_edges = {row["key"] for row in unit["scope"]["edges"]}
    reported_edges = {row["key"] for row in valid["coverage"]["edges"]}
    if expected_files - reported_files or expected_edges - reported_edges:
        raise ValueError(f"coverage normalization failed: files={sorted(expected_files-reported_files)}, edges={sorted(expected_edges-reported_edges)}")
    if finding_id and not any(f["finding_id"] == finding_id and f["verifier_status"] != "unverified" for f in valid["findings"]):
        raise ValueError(f"missing verdict for finding {finding_id}")
    return valid


def run_worker(unit_id: str) -> None:
    """Run at most two bounded Luna attempts for one independent DAG node."""
    unit = read_json(UNITS / f"{unit_id}.json")
    if unit["unit_id"] != unit_id:
        raise ValueError("unit ID mismatch")
    output = OUTPUTS / f"{unit_id}.json"
    LOGS.mkdir(parents=True, exist_ok=True)
    for attempt in (1, 2):
        log = ""
        attempt_path = RUNS / unit_id / f"attempt-{attempt:02d}.json"
        attempt_path.parent.mkdir(parents=True, exist_ok=True)
        attempt_path.unlink(missing_ok=True)
        command = [
            "codex", "exec", "--model", MODEL, "-c", f'model_reasoning_effort="{EFFORT}"',
            "--sandbox", "read-only", "--output-schema", str(SCHEMA),
            "--output-last-message", str(attempt_path), "--json", "--cd", str(ROOT),
            prompt_for(unit),
        ]
        started = time.monotonic()
        try:
            completed = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                       errors="replace", timeout=900, check=False)
            log = completed.stdout
            if completed.returncode:
                raise ValueError(f"Codex exited {completed.returncode}")
            valid = validate_output(read_json(attempt_path), unit)
            write_json(output, valid)
            print(f"accepted {unit_id}: {output.relative_to(ROOT).as_posix()}", flush=True)
            return
        except (OSError, ValueError, subprocess.TimeoutExpired, store.InvalidResult) as error:
            log += f"\nrunner error: {error}\n"
            print(f"attempt {attempt}/2 rejected for {unit_id}: {error}", file=sys.stderr, flush=True)
        finally:
            (LOGS / f"{unit_id}.attempt-{attempt:02d}.log").write_text(log, encoding="utf-8")
            attempt_path.unlink(missing_ok=True)
            print(f"{unit_id} attempt {attempt} elapsed {time.monotonic()-started:.1f}s", flush=True)
    output.unlink(missing_ok=True)
    raise RuntimeError(f"both Luna attempts failed for {unit_id}")


def plan_tasks() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate the pinned graph and return tasks in dependency order."""
    plan = read_json(PLAN)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if commit != plan["snapshot_commit"]:
        raise ValueError(f"checkout {commit} differs from review snapshot {plan['snapshot_commit']}")
    tasks = {task["id"]: task for task in plan["tasks"]}
    if len(tasks) != 433 or len(tasks) != len(plan["tasks"]):
        raise ValueError("full review plan is not the expected 433-node unique graph")
    ordered: list[dict[str, Any]] = []
    pending = dict(tasks)
    while pending:
        ready = sorted(k for k, v in pending.items() if all(d in tasks and d not in pending for d in v["deps"]))
        if not ready:
            raise ValueError("full review plan has a cycle or unknown dependency")
        for key in ready:
            ordered.append(pending.pop(key))
    return plan, ordered


def dag_json(*args: str) -> Any:
    """Call the local DAG CLI with structured output and fail on errors."""
    executable = shutil.which("dag.cmd" if os.name == "nt" else "dag")
    if executable is None:
        raise RuntimeError("dag CLI is not on PATH")
    completed = subprocess.run([executable, *args, "--json"], cwd=ROOT, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8", check=False)
    if completed.returncode:
        raise RuntimeError(f"dag {' '.join(args[:3])} failed: {completed.stderr or completed.stdout}")
    return json.loads(completed.stdout)


def materialize() -> None:
    """Archive the pilot and add all planned nodes with their real dependencies."""
    plan, ordered = plan_tasks()
    write_json(REVIEW / "plan" / "pilot-findings.json", {"findings": read_json(REVIEW / "report.json")["findings"]})
    UNITS.mkdir(parents=True, exist_ok=True)
    for task in ordered:
        if task["agent"]:
            write_json(UNITS / f"{task['id']}.json", unit_for(task, plan["snapshot_commit"]))
    dag_json("new-run", "--objective", "Read-only full code review of case_management at " + plan["snapshot_commit"])
    ids: dict[str, str] = {}
    for index, task in enumerate(ordered, 1):
        spec = (
            f"Inputs: pinned commit {plan['snapshot_commit']}, plan node {task['id']}, "
            f"and predecessor artifacts. Outputs: .review/workers/full/{task['id']}.json "
            f"or deterministic review artifact. Acceptance: {task['acceptance']} "
            "Review only; leave source unchanged."
        )
        command = f"python tools/review/full_run.py worker --unit {task['id']}" if task["agent"] else f"python tools/review/full_run.py finalize --phase {task['phase']}"
        arguments = ["add", "--title", task["title"], "--spec", spec, "--cmd", command]
        if task["deps"]:
            arguments += ["--deps", ",".join(ids[d] for d in task["deps"])]
        result = dag_json(*arguments)
        ids[task["id"]] = result["id"]
        if index % 25 == 0 or index == len(ordered):
            print(f"materialized {index}/{len(ordered)} DAG nodes", flush=True)
    write_json(REVIEW / "plan" / "full-dag-ids.json", {"snapshot_commit": plan["snapshot_commit"], "dag_ids": ids})
    dag_json("settings", "--concurrency", "4", "--on-dep-failure", "block", "--gates", "wait")
    print(f"full DAG ready: {len(ids)} nodes", flush=True)


def finalize(phase: str) -> None:
    """Aggregate artifacts once, then publish coverage and findings."""
    plan = read_json(PLAN)
    agent_tasks = [task for task in plan["tasks"] if task["agent"]]
    if phase == "coverage":
        relevant = [task for task in agent_tasks if task["phase"] in {"local", "edge", "unresolved-edge"}]
        missing = [task["id"] for task in relevant if not (OUTPUTS / f"{task['id']}.json").is_file()]
        if missing:
            raise RuntimeError(f"missing review artifacts: {missing[:10]}")
        records = [read_json(OUTPUTS / f"{task['id']}.json") for task in relevant]
        covered_files = {row["path"] for result in records for row in result["coverage"]["files"] if row["status"] == "reviewed"}
        covered_edges = {row["key"] for result in records for row in result["coverage"]["edges"] if row["status"] == "reviewed"}
        expected_files = {path for task in relevant if task["phase"] == "local" for path in task["scope"]["files"]}
        expected_edges = {edge_key(edge) for task in relevant for edge in task["scope"]["edges"]}
        result = {"expected_files": len(expected_files), "reviewed_files": len(covered_files & expected_files),
                  "expected_edges": len(expected_edges), "reviewed_edges": len(covered_edges & expected_edges),
                  "missing_files": sorted(expected_files-covered_files), "missing_edges": sorted(expected_edges-covered_edges)}
        write_json(REVIEW / "coverage-full.json", result)
        if result["missing_files"] or result["missing_edges"]:
            raise RuntimeError("full review coverage is incomplete; see .review/coverage-full.json")
    elif phase == "deduplication":
        for task in agent_tasks:
            path = OUTPUTS / f"{task['id']}.json"
            if path.is_file():
                store.import_result(path, store.DEFAULT_INVENTORY, store.DEFAULT_DATABASE)
        refresh_report()
        report = read_json(REVIEW / "report.json")
        by_location: dict[tuple[str, int], list[str]] = {}
        for finding in report["findings"]:
            key = (finding["location"]["path"], finding["location"]["line_start"])
            by_location.setdefault(key, []).append(finding["finding_id"])
        write_json(REVIEW / "duplicate-candidates.json", {
            "same_line_candidates": [ids for ids in by_location.values() if len(ids) > 1]
        })
    elif phase == "verifier-expansion":
        report = read_json(REVIEW / "report.json")
        pilot = {row["finding_id"] for row in read_json(REVIEW / "plan" / "pilot-findings.json")["findings"]}
        new = [row for row in report["findings"] if row["finding_id"] not in pilot]
        write_json(REVIEW / "new-findings-to-verify.json", {"findings": new})
        units_by_id: dict[str, dict[str, Any]] = {}
        for finding in new:
            unit_id = "verify-" + finding["finding_id"]
            unit = {
                "schema_version": 1, "unit_id": unit_id, "title": "Verify newly found defect",
                "phase": "finding-verifier", "snapshot_commit": plan["snapshot_commit"],
                "depends_on": [], "scope": {"files": [finding["location"]["path"]],
                                            "edges": [], "finding_id": finding["finding_id"]},
            }
            write_json(UNITS / f"{unit_id}.json", unit)
            units_by_id[unit_id] = unit
        remaining: list[str] = []
        for unit_id, unit in units_by_id.items():
            existing = OUTPUTS / f"{unit_id}.json"
            try:
                validate_output(read_json(existing), unit)
            except (OSError, ValueError, store.InvalidResult):
                remaining.append(unit_id)
        print(f"verifying {len(remaining)} findings; reusing {len(new)-len(remaining)} validated results", flush=True)
        # The DAG runner is set to one slot for this phase, so these four
        # workers preserve the configured four-call ceiling.
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(run_worker, unit_id): unit_id for unit_id in remaining}
            for future in as_completed(futures):
                future.result()
        for finding in new:
            store.import_result(OUTPUTS / f"verify-{finding['finding_id']}.json",
                                store.DEFAULT_INVENTORY, store.DEFAULT_DATABASE)
        refresh_report()
    elif phase == "handoff":
        for task in agent_tasks:
            if task["phase"] == "finding-verifier":
                store.import_result(OUTPUTS / f"{task['id']}.json",
                                    store.DEFAULT_INVENTORY, store.DEFAULT_DATABASE)
        refresh_report()
        print(f"review handoff: {REVIEW / 'report.json'}")
    else:
        raise ValueError(f"unknown deterministic phase {phase}")


def refresh_report() -> None:
    """Build the canonical report using its script-compatible import path."""
    completed = subprocess.run([sys.executable, "tools/review/report.py", "--output", str(REVIEW / "report.json")],
                               cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr or completed.stdout)


def main() -> int:
    """Dispatch setup, worker, and deterministic DAG phases."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("materialize")
    worker = commands.add_parser("worker")
    worker.add_argument("--unit", required=True)
    final = commands.add_parser("finalize")
    final.add_argument("--phase", required=True)
    args = parser.parse_args()
    try:
        if args.command == "materialize":
            materialize()
        elif args.command == "worker":
            run_worker(args.unit)
        else:
            finalize(args.phase)
        return 0
    except (OSError, ValueError, RuntimeError, store.InvalidResult) as error:
        print(f"full review runner failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
