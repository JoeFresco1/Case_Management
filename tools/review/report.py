"""Render deterministic findings and coverage reports from the local review store."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from store import DEFAULT_DATABASE, InvalidResult, _load_inventory, _connect


def build_report(database_path: Path = DEFAULT_DATABASE, inventory_path: Path = Path(".review/index/repository.json")) -> dict[str, Any]:
    """Build a stable aggregate report without timestamps or database row ordering."""
    snapshot, inventory_files = _load_inventory(inventory_path)
    db = _connect(database_path)
    try:
        workers = [row["worker_id"] for row in db.execute("SELECT worker_id FROM workers WHERE snapshot_commit=? ORDER BY worker_id", (snapshot,))]
        findings: list[dict[str, Any]] = []
        rows = db.execute(
            """SELECT f.*, wf.detail AS worker_detail, wf.evidence_json AS worker_evidence,
            v.worker_id, v.status AS verifier_status, v.note AS verifier_note
            FROM findings f JOIN verifier_statuses v USING(finding_id)
            JOIN worker_findings wf ON wf.finding_id=f.finding_id
              AND wf.worker_id=v.worker_id AND wf.snapshot_commit=v.snapshot_commit
            WHERE f.snapshot_commit=? ORDER BY f.path, f.line_start, f.finding_id, v.worker_id""",
            (snapshot,),
        ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            finding = grouped.setdefault(
                row["finding_id"],
                {
                    "finding_id": row["finding_id"], "rule_id": row["rule_id"], "severity": row["severity"],
                    "title": row["title"], "detail": row["worker_detail"],
                    "location": {"path": row["path"], "line_start": row["line_start"], "line_end": row["line_end"]},
                    "evidence": json.loads(row["worker_evidence"]), "duplicate_of": row["duplicate_of"],
                    "reported_by": [], "verifier_statuses": [],
                },
            )
            finding["detail"] = min(finding["detail"], row["worker_detail"])
            finding["evidence"] = min(finding["evidence"], json.loads(row["worker_evidence"]), key=lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            finding["reported_by"].append(row["worker_id"])
            finding["verifier_statuses"].append({"worker_id": row["worker_id"], "status": row["verifier_status"], "note": row["verifier_note"]})
        findings = sorted(grouped.values(), key=lambda item: (item["location"]["path"], item["location"]["line_start"], item["finding_id"]))
        coverage_rows = db.execute(
            "SELECT worker_id,kind,item_key,status,note FROM coverage WHERE snapshot_commit=? ORDER BY kind,item_key,worker_id",
            (snapshot,),
        ).fetchall()
        coverage_by_kind: dict[str, dict[str, dict[str, Any]]] = {kind: {} for kind in ("files", "edges", "subsystems", "flows")}
        for row in coverage_rows:
            record = coverage_by_kind[row["kind"]].setdefault(row["item_key"], {"key": row["item_key"], "statuses": [], "workers": []})
            record["statuses"].append({"worker_id": row["worker_id"], "status": row["status"], "note": row["note"]})
            record["workers"].append(row["worker_id"])
        coverage: dict[str, Any] = {}
        for kind, records in coverage_by_kind.items():
            items = [records[key] for key in sorted(records)]
            status_counts: Counter[str] = Counter(status["status"] for item in items for status in item["statuses"])
            coverage[kind] = {
                "inventory_total": len(inventory_files) if kind == "files" else len(items),
                "reported_unique": len(items),
                "reviewed_unique": sum(any(status["status"] == "reviewed" for status in item["statuses"]) for item in items),
                "status_counts": {status: status_counts[status] for status in sorted(status_counts)},
                "items": items,
            }
        status_counts = Counter(status["status"] for item in findings for status in item["verifier_statuses"])
        duplicate_rows = db.execute(
            "SELECT finding_id,duplicate_of,worker_id FROM duplicate_links WHERE snapshot_commit=? ORDER BY finding_id,duplicate_of,worker_id",
            (snapshot,),
        ).fetchall()
        duplicate_links = [dict(row) for row in duplicate_rows]
        return {
            "schema_version": 1,
            "snapshot_commit": snapshot,
            "workers": workers,
            "findings": findings,
            "finding_count": len(findings),
            "verifier_status_counts": {status: status_counts[status] for status in sorted(status_counts)},
            "duplicate_links": duplicate_links,
            "coverage": coverage,
        }
    finally:
        db.close()


def main() -> int:
    """Write canonical JSON to stdout or a path under .review."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--inventory", type=Path, default=Path(".review/index/repository.json"))
    parser.add_argument("--output", type=Path, default=Path(".review/report.json"))
    args = parser.parse_args()
    try:
        report = build_report(args.database, args.inventory)
        payload = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        output = args.output.resolve()
        output.relative_to(Path.cwd().resolve() / ".review")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    except (InvalidResult, OSError, ValueError, sqlite3.Error) as error:
        print(f"review report failed: {error}", file=sys.stderr)
        return 2
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
