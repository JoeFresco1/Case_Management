"""Build deterministic, resumable review units from a pinned repository snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REVIEW = ROOT / ".review"
REPOSITORY_MANIFEST = REVIEW / "index" / "repository.json"
DEPENDENCY_MANIFEST = REVIEW / "index" / "dependencies.json"
FINDINGS_SCHEMA = ROOT / "tools" / "review" / "result.schema.json"
OUTPUT_DIRECTORY = REVIEW / "units" / "pilot"
UNIT_SCHEMA_VERSION = 1
RISK_WEIGHTS = {
    "centrality": 0.25, "loc": 0.20, "boundaries": 0.30,
    "prior_findings": 0.15, "test_links": 0.10,
}

PILOTS: tuple[dict[str, Any], ...] = (
    {
        "id": "pilot-domain",
        "title": "Case and case type invariants",
        "purpose": (
            "Review case editing, schema validation, and immutable case type publication behavior."
        ),
        "files": {
            "backend/app/domain/case.py": [
                "Case", "Case.create", "Case.edit", "Case.assert_concurrency_token",
            ],
            "backend/app/domain/case_type.py": [
                "CaseTypeSchema", "CaseTypeVersion.publish", "CaseTypeVersion.assert_mutable",
            ],
        },
        "tests": {
            "backend/tests/domain/test_case_type.py": [
                "test_publish__draft_to_published__stamps_actor_and_time",
                "test_publish__on_published_version__raises_invariant",
                "test_case_type__with_published_version__rejects_cross_tenant",
            ],
        },
    },
    {
        "id": "pilot-edge",
        "title": "Case intake across HTTP, application, and persistence",
        "purpose": (
            "Review request validation through case creation, repository persistence, and "
            "tenant-scoped reads."
        ),
        "files": {
            "backend/app/interfaces/http/routers/cases/create.py": ["create_case"],
            "backend/app/application/cases/create_case.py": [
                "CreateCaseCommand", "handle", "workflow_key_for_case_type",
            ],
            "backend/app/infrastructure/persistence/repositories/case_repository.py": [
                "SqlCaseRepository.add",
                "SqlCaseRepository.next_reference",
                "SqlCaseRepository.get",
            ],
        },
        "tests": {
            "backend/tests/application/cases/test_create_case.py": [
                "test_create_case__valid_payload__persists_case_audit_outbox_atomically",
                "test_create_case__replay_with_same_idempotency_key__does_not_duplicate",
            ],
            "backend/tests/interfaces/test_cases_router.py": [
                "test_view_case__cross_tenant_case_id__returns_404",
                "test_list_cases__default_page_size_is_50",
            ],
        },
    },
    {
        "id": "pilot-subsystem",
        "title": "Workflow transition execution and guards",
        "purpose": (
            "Review transition-state invariants, guard evaluation, and staged workflow actions."
        ),
        "files": {
            "backend/app/domain/workflow.py": [
                "WorkflowDefinition", "WorkflowVersion.publish", "WorkflowVersion.assert_mutable",
            ],
            "backend/app/application/workflow/execute_transition/handle.py": [
                "handle", "_record_transition",
            ],
            "backend/app/application/workflow/execute_transition/_guards.py": [
                "_enforce_state_form_gate", "_build_guard_context", "_evaluate_transition_guard",
            ],
        },
        "tests": {
            "backend/tests/application/test_execute_transition_actions.py": [
                "test_transition__set_field__writes_data_and_top_level",
                "test_transition__authoring_guard_false__blocks",
            ],
        },
    },
    {
        "id": "pilot-frontend",
        "title": "Case workspace transition client",
        "purpose": (
            "Review the case workspace, transition action UI, and shared HTTP client behavior."
        ),
        "files": {
            "frontend/src/cases/CaseWorkspace.tsx": [
                "CaseWorkspace", "CaseDetail", "caseQueryKeys",
            ],
            "frontend/src/cases/TransitionsPanel.tsx": ["TransitionsPanel"],
            "frontend/src/api/client/http.ts": ["ApiClientError", "request", "requestVoid"],
        },
        "tests": {
            "frontend/src/cases/TransitionsPanel.test.tsx": ["renderPanel"],
            "frontend/src/api/client.test.ts": ["mockFetchOnce"],
        },
    },
    {
        "id": "pilot-security",
        "title": "Authorization and tenant isolation",
        "purpose": (
            "Review default-deny policy evaluation, local identity context, and cross-tenant "
            "repository safeguards."
        ),
        "files": {
            "backend/app/application/authorization.py": [
                "Policy.evaluate", "authorize", "default_policy",
            ],
            "backend/app/domain/permissions.py": ["evaluate_permission"],
            "backend/app/deps.py": ["LocalHeaderIdentityProvider.current_user", "get_current_user"],
        },
        "tests": {
            "backend/tests/security/test_authorization.py": [
                "test_policy_evaluate__unknown_action__denies",
                "test_authorize__raises_forbidden_on_cross_tenant",
            ],
            "backend/tests/security/test_tenant_isolation.py": [
                "test_cross_tenant_read_of_known_id__returns_none",
                "test_cross_tenant_list__returns_only_own_tenant_rows",
            ],
        },
    },
    {
        "id": "pilot-e2e",
        "title": "Case intake vertical path",
        "purpose": (
            "Trace typed case submission through the HTTP and application flow, including workflow "
            "startup and response behavior."
        ),
        "files": {
            "backend/app/interfaces/http/routers/cases/create.py": ["create_case"],
            "backend/app/application/cases/create_case.py": [
                "handle", "workflow_key_for_case_type",
            ],
            "frontend/src/cases/CreateCasePage.tsx": ["CreateCasePage", "CreateCaseTypeSelection"],
            "frontend/src/cases/CreateCaseForm.tsx": ["validateCreateCase", "CreateCaseForm"],
        },
        "tests": {
            "backend/tests/application/cases/test_create_case_workflow.py": [
                "test_create_case__with_workflow__starts_instance_tasks_and_sets_state",
            ],
            "backend/tests/interfaces/test_cases_router.py": [
                "test_view_case__authorized_actor__returns_full_summary",
            ],
        },
    },
)

GENERATED_PARTS = {"__generated__", "generated", "gen", "dist", "build", "node_modules"}
GENERATED_NAME = re.compile(r"(?:\.gen\.|\.generated\.|\.min\.)", re.IGNORECASE)


def canonical_bytes(value: Any) -> bytes:
    """Serialize JSON with stable key order and whitespace for fingerprints."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    """Return a lowercase SHA-256 digest for immutable input bytes."""
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object or raise a concise input error."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {path.relative_to(ROOT).as_posix()}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path.relative_to(ROOT).as_posix()} must contain a JSON object")
    return value


def generated_path(path: str, record: dict[str, Any]) -> bool:
    """Reject inventory-marked and conventional generated source paths."""
    parts = path.lower().split("/")
    return (
        record.get("reason") == "generated"
        or any(part in GENERATED_PARTS for part in parts)
        or GENERATED_NAME.search(parts[-1]) is not None
        or parts[-1].startswith("openapi.")
    )


def _record_finding(
    counts: dict[str, dict[str, int]], source_path: Any, severity: Any
) -> None:
    if isinstance(source_path, str):
        by_severity = counts.setdefault(source_path, {})
        key = str(severity)
        by_severity[key] = by_severity.get(key, 0) + 1


def _read_prior_artifacts(
    counts: dict[str, dict[str, int]], input_hashes: dict[str, str]
) -> None:
    """Load schema-shaped findings arrays from prior JSON review artifacts."""
    ignored_parts = {"index", "baseline", "runs", "logs", "units", "uv-cache"}
    for path in sorted(REVIEW.glob("**/*.json")):
        relative = path.relative_to(ROOT).as_posix()
        if set(path.relative_to(REVIEW).parts) & ignored_parts or path == FINDINGS_SCHEMA:
            continue
        try:
            raw = path.read_bytes()
            artifact = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(artifact, dict) or not isinstance(artifact.get("findings"), list):
            continue
        input_hashes[relative] = sha256_bytes(raw)
        for finding in artifact["findings"]:
            if not isinstance(finding, dict):
                continue
            location = finding.get("location")
            source_path = (
                location.get("path") if isinstance(location, dict) else finding.get("path")
            )
            severity = finding.get("severity", "info")
            _record_finding(counts, source_path, severity)


def _read_prior_database(
    counts: dict[str, dict[str, int]], input_hashes: dict[str, str]
) -> None:
    """Read path and severity columns from an optional findings SQLite table."""
    database = REVIEW / "findings.sqlite3"
    if not database.is_file():
        return
    raw = database.read_bytes()
    input_hashes[database.relative_to(ROOT).as_posix()] = sha256_bytes(raw)
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "findings" not in tables:
            return
        columns = {row[1] for row in connection.execute("PRAGMA table_info(findings)")}
        path_column = next((name for name in ("path", "file_path") if name in columns), None)
        severity_column = "severity" if "severity" in columns else None
        if not path_column:
            return
        selected = [path_column] + ([severity_column] if severity_column else [])
        query = (
            "SELECT "
            + ", ".join(f'"{name}"' for name in selected)
            + " FROM findings"
        )
        for row in connection.execute(query):
            _record_finding(counts, row[0], row[1] if severity_column else "info")
    finally:
        connection.close()


def prior_findings() -> tuple[dict[str, dict[str, int]], dict[str, str]]:
    """Collect findings from prior review artifacts and optional local SQLite state."""
    counts: dict[str, dict[str, int]] = {}
    input_hashes: dict[str, str] = {}
    _read_prior_artifacts(counts, input_hashes)
    _read_prior_database(counts, input_hashes)
    return counts, input_hashes


def _unit_symbols(spec: dict[str, Any], path: str) -> list[str]:
    """Return the configured exact symbols for one selected source file."""
    values = spec.get(path, [])
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"invalid symbol selection for {path}")
    return sorted(set(values))


def _boundary_flags(path: str, content: bytes) -> dict[str, bool]:
    """Identify security, persistence, and asynchronous boundaries from code and path."""
    lowered = path.lower()
    text = content.decode("utf-8", errors="replace")
    return {
        "security": (
            any(
                term in lowered
                for term in (
                    "security", "authorization", "permission", "deps.py", "tenant", "audit"
                )
            )
            or any(term in text for term in ("authorize(", "tenant_id", "PermissionDenied"))
        ),
        "persistence": (
            any(
                term in lowered
                for term in ("persistence", "repository", "_repo", "client/http")
            )
            or any(
                term in text
                for term in ("AsyncSession", "session.execute", "session.commit", "fetch(")
            )
        ),
        "async": (
            re.search(rb"\basync\s+def\b|\bawait\s+|\basync\s+function\b", content)
            is not None
        ),
    }


def _boundary_scores(
    paths: list[str], contents: dict[str, bytes]
) -> tuple[dict[str, bool], float]:
    """Summarize whether a unit crosses security, persistence, and async boundaries."""
    flags = [_boundary_flags(path, contents[path]) for path in paths]
    boundaries = {
        key: any(flag[key] for flag in flags)
        for key in ("security", "persistence", "async")
    }
    return boundaries, sum(boundaries.values()) / 3


def _finding_risk(
    paths: list[str], findings: dict[str, dict[str, int]]
) -> tuple[dict[str, int], int, float]:
    """Return prior finding severity totals, weighted count, and normalized risk."""
    finding_counts: dict[str, int] = {}
    for path in paths:
        for severity, count in findings.get(path, {}).items():
            finding_counts[severity] = finding_counts.get(severity, 0) + count
    weights = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
    finding_weight = sum(
        count * weights.get(severity, 1) for severity, count in finding_counts.items()
    )
    score = min(1.0, math.log1p(finding_weight) / math.log1p(20))
    return finding_counts, finding_weight, score


def _centrality_score(
    paths: list[str], nodes_by_path: dict[str, dict[str, Any]], repository_max_degree: int
) -> tuple[list[int], float]:
    """Normalize mean file degree against the most connected snapshot file."""
    degree = [
        int(nodes_by_path[path].get("fan_in", 0)) + int(nodes_by_path[path].get("fan_out", 0))
        for path in paths
    ]
    mean_degree = sum(degree) / max(1, len(degree))
    score = min(
        1.0,
        math.log1p(mean_degree) / math.log1p(max(1, repository_max_degree)),
    )
    return degree, score


def _component_scores(
    production_files: list[dict[str, Any]],
    test_files: list[dict[str, Any]],
    nodes_by_path: dict[str, dict[str, Any]],
    contents: dict[str, bytes],
    findings: dict[str, dict[str, int]],
    repository_max_degree: int,
) -> dict[str, Any]:
    """Compute weighted risk parts together so the score formula stays inspectable."""
    paths = [item["path"] for item in production_files]
    degree, centrality = _centrality_score(paths, nodes_by_path, repository_max_degree)
    total_loc = sum(item["loc"] for item in production_files + test_files)
    loc_score = min(1.0, total_loc / 1800)
    boundaries, boundary_score = _boundary_scores(paths, contents)
    finding_counts, finding_weight, prior_findings_score = _finding_risk(paths, findings)
    test_link_coverage = min(1.0, len(test_files) / max(1, len(production_files)))
    test_gap_score = 1.0 - test_link_coverage
    weighted_score = (
        centrality * 0.25
        + loc_score * 0.20
        + boundary_score * 0.30
        + prior_findings_score * 0.15
        + test_gap_score * 0.10
    )
    return {
        "score": round(weighted_score * 100, 2),
        "components": {
            "centrality": round(centrality, 4),
            "centrality_mean_degree": round(sum(degree) / max(1, len(degree)), 4),
            "loc": round(loc_score, 4),
            "handwritten_loc": total_loc,
            "boundaries": {**boundaries, "score": round(boundary_score, 4)},
            "prior_findings": {
                "score": round(prior_findings_score, 4),
                "weighted_count": finding_weight,
                "by_severity": dict(sorted(finding_counts.items())),
            },
            "test_links": {
                "score": round(test_gap_score, 4),
                "coverage": round(test_link_coverage, 4),
                "linked_test_files": len(test_files),
                "production_files": len(production_files),
            },
            "weights": RISK_WEIGHTS,
        },
    }


def _load_build_context() -> dict[str, Any]:
    """Load validated snapshot manifests and deterministic input fingerprints."""
    repository = read_json(REPOSITORY_MANIFEST)
    dependencies = read_json(DEPENDENCY_MANIFEST)
    read_json(FINDINGS_SCHEMA)
    snapshot = repository.get("snapshot", {}).get("commit")
    if not isinstance(snapshot, str) or dependencies.get("snapshot", {}).get("commit") != snapshot:
        raise ValueError(
            "repository and dependency manifests must refer to the same immutable snapshot"
        )
    source_rows = {row["path"]: row for row in repository.get("source_files", [])}
    excluded_rows = {row["path"]: row for row in repository.get("excluded_sources", [])}
    nodes_by_path = {row["path"]: row for row in dependencies.get("nodes", [])}
    edges = dependencies.get("edges", [])
    adjacency: dict[str, int] = {}
    for edge in edges:
        source, target = edge.get("source"), edge.get("target")
        if isinstance(source, str) and isinstance(target, str):
            adjacency[source] = adjacency.get(source, 0) + 1
            adjacency[target] = adjacency.get(target, 0) + 1
    # why: older dependency manifests omit fan counts, so derive them from the resolved edges.
    for path, node in nodes_by_path.items():
        if "fan_in" not in node or "fan_out" not in node:
            node["fan_in"] = adjacency.get(path, 0)
            node["fan_out"] = adjacency.get(path, 0)
    repository_max_degree = max(
        (
            int(node.get("fan_in", 0)) + int(node.get("fan_out", 0))
            for node in nodes_by_path.values()
        ),
        default=1,
    )

    findings, finding_hashes = prior_findings()
    base_hashes = {
        "repository_manifest": sha256_bytes(REPOSITORY_MANIFEST.read_bytes()),
        "dependency_manifest": sha256_bytes(DEPENDENCY_MANIFEST.read_bytes()),
        "findings_schema": sha256_bytes(FINDINGS_SCHEMA.read_bytes()),
        "generator": sha256_bytes(Path(__file__).read_bytes()),
        **finding_hashes,
    }
    return {
        "snapshot": snapshot,
        "source_rows": source_rows,
        "excluded_rows": excluded_rows,
        "nodes_by_path": nodes_by_path,
        "repository_max_degree": repository_max_degree,
        "findings": findings,
        "base_hashes": base_hashes,
    }


def _snapshot_file_records(
    pilot: dict[str, Any],
    snapshot: str,
    role: str,
    selection: dict[str, list[str]],
    inventory: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """Validate one source or test selection and read its pinned Git blobs."""
    records: list[dict[str, Any]] = []
    contents: dict[str, bytes] = {}
    for path in sorted(selection):
        record = inventory.get(path)
        if record is None:
            raise ValueError(f"{pilot['id']} references a path absent from the inventory: {path}")
        if role == "test" and record.get("reason") != "test":
            raise ValueError(f"{pilot['id']} test link is not an inventoried test: {path}")
        if generated_path(path, record):
            raise ValueError(f"{pilot['id']} references generated code: {path}")
        symbols = _unit_symbols(selection, path)
        if not symbols:
            raise ValueError(f"{pilot['id']} requires at least one exact symbol for {path}")
        records.append({"path": path, "loc": record["loc"], "symbols": symbols})
        try:
            blob = subprocess.run(
                ["git", "show", f"{snapshot}:{path}"], cwd=ROOT, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ).stdout
        except (OSError, subprocess.CalledProcessError) as error:
            raise ValueError(f"cannot read {path} from snapshot {snapshot}: {error}") from error
        contents[path] = blob
    return records, contents


def _build_pilot(pilot: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Build and enforce size limits for one configured pilot unit."""
    production_files, source_contents = _snapshot_file_records(
        pilot, context["snapshot"], "source", pilot["files"], context["source_rows"]
    )
    test_files, test_contents = _snapshot_file_records(
        pilot, context["snapshot"], "test", pilot["tests"], context["excluded_rows"]
    )
    contents = source_contents | test_contents
    all_files = production_files + test_files
    file_count = len(all_files)
    loc = sum(item["loc"] for item in all_files)
    if file_count > 6 or loc > 1800:
        raise ValueError(f"{pilot['id']} exceeds pilot limits: {file_count} files, {loc} LOC")
    risk = _component_scores(
        production_files,
        test_files,
        context["nodes_by_path"],
        contents,
        context["findings"],
        context["repository_max_degree"],
    )
    unit = {
        "schema_version": UNIT_SCHEMA_VERSION,
        "unit_id": pilot["id"],
        "title": pilot["title"],
        "purpose": pilot["purpose"],
        "snapshot_commit": context["snapshot"],
        "scope": {
            "files": production_files,
            "test_links": test_files,
            "handwritten_file_count": file_count,
            "handwritten_loc": loc,
        },
        "risk": risk,
        "inputs_sha256": dict(sorted(context["base_hashes"].items())),
    }
    unit["unit_fingerprint"] = sha256_bytes(canonical_bytes(unit))
    return unit


def build_units() -> list[dict[str, Any]]:
    """Validate inputs and construct all six stable pilot unit records."""
    context = _load_build_context()
    return [_build_pilot(pilot, context) for pilot in PILOTS]


def write_resumable(units: list[dict[str, Any]]) -> tuple[int, int]:
    """Atomically write changed units and leave byte-identical artifacts untouched."""
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    for unit in units:
        path = OUTPUT_DIRECTORY / f"{unit['unit_id']}.json"
        content = json.dumps(unit, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            skipped += 1
            continue
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
        written += 1
    return written, skipped


def main() -> int:
    """Generate pilot units, reporting input errors without partial artifacts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        units = build_units()
        if args.check:
            stale = []
            for unit in units:
                path = OUTPUT_DIRECTORY / f"{unit['unit_id']}.json"
                expected = json.dumps(unit, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                    stale.append(path.relative_to(ROOT).as_posix())
            if stale:
                raise ValueError("pilot artifacts are missing or stale: " + ", ".join(stale))
            print(f"pilot units current: {len(units)}")
            return 0
        written, skipped = write_resumable(units)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"review units rejected: {error}", file=sys.stderr)
        return 2
    print(f"pilot units generated: {written}; unchanged: {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
