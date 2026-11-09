"""Validate and import isolated review-worker results into a resumable SQLite store."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_INVENTORY = Path(".review/index/repository.json")
DEFAULT_DATABASE = Path(".review/findings.sqlite3")
SEVERITIES = {"critical", "high", "medium", "low", "info"}
VERIFIER_STATUSES = {"unverified", "confirmed", "rejected", "needs_context"}
COVERAGE_STATUSES = {"reviewed", "partial", "blocked", "not_applicable"}
WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
FINDING_ID = re.compile(r"^F-[0-9a-f]{24}$")


class InvalidResult(ValueError):
    """Raised when a worker artifact fails schema or repository validation."""


def _require_object(value: Any, label: str, fields: set[str], required: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidResult(f"{label} must be an object")
    extras = set(value) - fields
    missing = required - set(value)
    if extras or missing:
        raise InvalidResult(f"{label} keys invalid; missing={sorted(missing)}, extra={sorted(extras)}")
    return value


def _text(value: Any, label: str, minimum: int = 1, maximum: int = 10000) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum or not value.strip():
        raise InvalidResult(f"{label} must be a non-empty string of at most {maximum} characters")
    return value


def _repo_path(value: Any, label: str) -> str:
    path = _text(value, label, maximum=1000)
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or ".." in parsed.parts or "\\" in path or path.startswith("./"):
        raise InvalidResult(f"{label} must be a normalized repository-relative POSIX path")
    return path


def stable_finding_id(
    snapshot_commit: str,
    rule_id: str,
    path: str,
    line_start: int,
    line_end: int | None,
    title: str,
) -> str:
    """Return the stable issue key derived from snapshot and source identity."""
    identity = [snapshot_commit, rule_id, path, line_start, line_end, " ".join(title.split()).casefold()]
    canonical = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return "F-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _load_inventory(path: Path) -> tuple[str, set[str]]:
    try:
        inventory = json.loads(path.read_text(encoding="utf-8"))
        commit = inventory["snapshot"]["commit"]
        files = {item["path"] for item in inventory["source_files"]}
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise InvalidResult(f"cannot read inventory {path}: {error}") from error
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit) or not all(isinstance(p, str) for p in files):
        raise InvalidResult("inventory has an invalid snapshot commit or source file list")
    return commit, files


def validate_result(result: Any, inventory_files: set[str]) -> dict[str, Any]:
    """Validate structure, stable IDs, and all paths against the snapshot inventory."""
    root = _require_object(
        result,
        "result",
        {"schema_version", "snapshot_commit", "worker_id", "findings", "coverage"},
        {"schema_version", "snapshot_commit", "worker_id", "findings", "coverage"},
    )
    if root["schema_version"] != SCHEMA_VERSION:
        raise InvalidResult(f"unsupported schema_version: {root['schema_version']!r}")
    if not isinstance(root["snapshot_commit"], str) or not COMMIT.fullmatch(root["snapshot_commit"]):
        raise InvalidResult("snapshot_commit must be a full lowercase 40-character Git commit")
    worker_id = _text(root["worker_id"], "worker_id", maximum=100)
    if not WORKER_ID.fullmatch(worker_id):
        raise InvalidResult("worker_id contains unsupported characters")
    if not isinstance(root["findings"], list):
        raise InvalidResult("findings must be an array")
    coverage = _require_object(root["coverage"], "coverage", {"files", "edges", "subsystems", "flows"}, {"files", "edges", "subsystems", "flows"})
    seen_findings: set[str] = set()
    for index, item in enumerate(root["findings"]):
        label = f"findings[{index}]"
        finding = _require_object(
            item,
            label,
            {"finding_id", "rule_id", "severity", "title", "detail", "location", "evidence", "verifier_status", "verifier_note", "duplicate_of"},
            {"finding_id", "rule_id", "severity", "title", "detail", "location", "verifier_status"},
        )
        rule_id = _text(finding["rule_id"], f"{label}.rule_id", maximum=120)
        severity = finding["severity"]
        if severity not in SEVERITIES:
            raise InvalidResult(f"{label}.severity is invalid")
        title = _text(finding["title"], f"{label}.title", maximum=300)
        _text(finding["detail"], f"{label}.detail")
        location = _require_object(finding["location"], f"{label}.location", {"path", "line_start", "line_end"}, {"path", "line_start"})
        path = _repo_path(location["path"], f"{label}.location.path")
        if path not in inventory_files:
            raise InvalidResult(f"{label} path is absent from the snapshot source inventory: {path}")
        line_start, line_end = location["line_start"], location.get("line_end")
        if type(line_start) is not int or line_start < 1 or (line_end is not None and (type(line_end) is not int or line_end < line_start)):
            raise InvalidResult(f"{label} line range is invalid")
        expected = stable_finding_id(root["snapshot_commit"], rule_id, path, line_start, line_end, title)
        if finding["finding_id"] != expected:
            raise InvalidResult(f"{label}.finding_id does not match stable identity; expected {expected}")
        if expected in seen_findings:
            raise InvalidResult(f"duplicate finding_id in worker artifact: {expected}")
        seen_findings.add(expected)
        if finding["verifier_status"] not in VERIFIER_STATUSES:
            raise InvalidResult(f"{label}.verifier_status is invalid")
        if finding.get("duplicate_of") is not None:
            if not isinstance(finding["duplicate_of"], str) or not FINDING_ID.fullmatch(finding["duplicate_of"]):
                raise InvalidResult(f"{label}.duplicate_of is invalid")
            if finding["duplicate_of"] == expected:
                raise InvalidResult(f"{label} cannot duplicate itself")
        if "evidence" in finding and (not isinstance(finding["evidence"], list) or any(not isinstance(x, str) or len(x) > 2000 for x in finding["evidence"])):
            raise InvalidResult(f"{label}.evidence must be an array of short strings")
        if "verifier_note" in finding and (not isinstance(finding["verifier_note"], str) or len(finding["verifier_note"]) > 4000):
            raise InvalidResult(f"{label}.verifier_note is invalid")
    for kind in ("files", "edges", "subsystems", "flows"):
        rows = coverage[kind]
        if not isinstance(rows, list):
            raise InvalidResult(f"coverage.{kind} must be an array")
        seen: set[str] = set()
        for index, item in enumerate(rows):
            label = f"coverage.{kind}[{index}]"
            fields = {"path", "status", "note"} if kind == "files" else {"key", "status", "note"}
            required = {"path", "status"} if kind == "files" else {"key", "status"}
            row = _require_object(item, label, fields, required)
            key = _repo_path(row["path"], f"{label}.path") if kind == "files" else _text(row["key"], f"{label}.key", maximum=300)
            if kind == "files" and key not in inventory_files:
                raise InvalidResult(f"{label} path is absent from the snapshot source inventory: {key}")
            if key in seen:
                raise InvalidResult(f"duplicate coverage key in {kind}: {key}")
            seen.add(key)
            if row["status"] not in COVERAGE_STATUSES:
                raise InvalidResult(f"{label}.status is invalid")
            if "note" in row and (not isinstance(row["note"], str) or len(row["note"]) > 2000):
                raise InvalidResult(f"{label}.note is invalid")
    return root


def _connect(database: Path) -> sqlite3.Connection:
    """Open the local store and create its stable, normalized schema."""
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS workers (
            worker_id TEXT NOT NULL,
            snapshot_commit TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            PRIMARY KEY (worker_id, snapshot_commit)
        );
        CREATE TABLE IF NOT EXISTS findings (
            finding_id TEXT PRIMARY KEY,
            snapshot_commit TEXT NOT NULL,
            rule_id TEXT NOT NULL,
            severity TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT NOT NULL,
            path TEXT NOT NULL,
            line_start INTEGER NOT NULL,
            line_end INTEGER,
            evidence_json TEXT NOT NULL,
            duplicate_of TEXT
        );
        CREATE TABLE IF NOT EXISTS verifier_statuses (
            worker_id TEXT NOT NULL,
            snapshot_commit TEXT NOT NULL,
            finding_id TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT NOT NULL,
            PRIMARY KEY (worker_id, snapshot_commit, finding_id),
            FOREIGN KEY (worker_id, snapshot_commit) REFERENCES workers(worker_id, snapshot_commit) ON DELETE CASCADE,
            FOREIGN KEY (finding_id) REFERENCES findings(finding_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS worker_findings (
            worker_id TEXT NOT NULL,
            snapshot_commit TEXT NOT NULL,
            finding_id TEXT NOT NULL,
            detail TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            PRIMARY KEY (worker_id, snapshot_commit, finding_id),
            FOREIGN KEY (worker_id, snapshot_commit) REFERENCES workers(worker_id, snapshot_commit) ON DELETE CASCADE,
            FOREIGN KEY (finding_id) REFERENCES findings(finding_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS duplicate_links (
            worker_id TEXT NOT NULL,
            snapshot_commit TEXT NOT NULL,
            finding_id TEXT NOT NULL,
            duplicate_of TEXT NOT NULL,
            PRIMARY KEY (worker_id, snapshot_commit, finding_id, duplicate_of),
            FOREIGN KEY (worker_id, snapshot_commit) REFERENCES workers(worker_id, snapshot_commit) ON DELETE CASCADE,
            FOREIGN KEY (finding_id) REFERENCES findings(finding_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS coverage (
            worker_id TEXT NOT NULL,
            snapshot_commit TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('files', 'edges', 'subsystems', 'flows')),
            item_key TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT NOT NULL,
            PRIMARY KEY (worker_id, snapshot_commit, kind, item_key),
            FOREIGN KEY (worker_id, snapshot_commit) REFERENCES workers(worker_id, snapshot_commit) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS ix_findings_snapshot_path ON findings(snapshot_commit, path, line_start);
        CREATE INDEX IF NOT EXISTS ix_coverage_snapshot_kind ON coverage(snapshot_commit, kind, status);
        """
    )
    return connection


def import_result(result_path: Path, inventory_path: Path = DEFAULT_INVENTORY, database_path: Path = DEFAULT_DATABASE) -> bool:
    """Atomically import one worker artifact; return False when unchanged."""
    try:
        raw = result_path.read_bytes()
        result = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidResult(f"cannot read worker result {result_path}: {error}") from error
    inventory_commit, inventory_files = _load_inventory(inventory_path)
    result = validate_result(result, inventory_files)
    if result["snapshot_commit"] != inventory_commit:
        raise InvalidResult("worker snapshot_commit does not match the inventory snapshot")
    digest = hashlib.sha256(raw).hexdigest()
    db = _connect(database_path)
    try:
        with db:
            existing = db.execute(
                "SELECT artifact_sha256 FROM workers WHERE worker_id=? AND snapshot_commit=?",
                (result["worker_id"], inventory_commit),
            ).fetchone()
            if existing is not None and existing["artifact_sha256"] == digest:
                return False
            db.execute("DELETE FROM workers WHERE worker_id=? AND snapshot_commit=?", (result["worker_id"], inventory_commit))
            db.execute(
                "INSERT INTO workers(worker_id,snapshot_commit,artifact_sha256) VALUES(?,?,?)",
                (result["worker_id"], inventory_commit, digest),
            )
            for finding in result["findings"]:
                location = finding["location"]
                db.execute(
                    """INSERT INTO findings VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(finding_id) DO UPDATE SET
                    detail=MIN(detail, excluded.detail), evidence_json=MIN(evidence_json, excluded.evidence_json),
                    duplicate_of=CASE
                      WHEN duplicate_of IS NULL THEN excluded.duplicate_of
                      WHEN excluded.duplicate_of IS NULL THEN duplicate_of
                      ELSE MIN(duplicate_of, excluded.duplicate_of) END""",
                    (
                        finding["finding_id"], inventory_commit, finding["rule_id"], finding["severity"],
                        finding["title"], finding["detail"], location["path"], location["line_start"],
                        location.get("line_end"), json.dumps(finding.get("evidence", []), ensure_ascii=False, separators=(",", ":")),
                        finding.get("duplicate_of"),
                    ),
                )
                db.execute(
                    "INSERT INTO verifier_statuses VALUES(?,?,?,?,?)",
                    (result["worker_id"], inventory_commit, finding["finding_id"], finding["verifier_status"], finding.get("verifier_note", "")),
                )
                db.execute(
                    "INSERT INTO worker_findings VALUES(?,?,?,?,?)",
                    (result["worker_id"], inventory_commit, finding["finding_id"], finding["detail"], json.dumps(finding.get("evidence", []), ensure_ascii=False, separators=(",", ":"))),
                )
                if finding.get("duplicate_of"):
                    db.execute(
                        "INSERT INTO duplicate_links VALUES(?,?,?,?)",
                        (result["worker_id"], inventory_commit, finding["finding_id"], finding["duplicate_of"]),
                    )
            for kind in ("files", "edges", "subsystems", "flows"):
                for item in result["coverage"][kind]:
                    key = item["path"] if kind == "files" else item["key"]
                    db.execute(
                        "INSERT INTO coverage VALUES(?,?,?,?,?,?)",
                        (result["worker_id"], inventory_commit, kind, key, item["status"], item.get("note", "")),
                    )
            # Remove findings no worker reports after an updated worker artifact.
            db.execute("DELETE FROM findings WHERE snapshot_commit=? AND finding_id NOT IN (SELECT finding_id FROM verifier_statuses WHERE snapshot_commit=?)", (inventory_commit, inventory_commit))
    finally:
        db.close()
    return True


def main() -> int:
    """Provide a small CLI for repeatable worker-result imports."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path, help="worker JSON artifact under .review/workers/")
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args()
    try:
        imported = import_result(args.result, args.inventory, args.database)
    except InvalidResult as error:
        print(f"review import rejected: {error}", file=sys.stderr)
        return 2
    print("imported" if imported else "unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
