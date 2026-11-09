"""Build a review DAG plan from the pinned inventory without changing DAG run state."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from tools.review import units

ROOT = Path(__file__).resolve().parents[2]
REVIEW = ROOT / ".review"
OUTPUT = REVIEW / "plan" / "full-dag.json"
MAX_FILES = 6
MAX_LOC = 1800
MAX_EDGES = 12
MODEL = {"name": "gpt-6-luna", "reasoning_effort": "medium", "sandbox": "read-only"}

INTEGRATIONS: dict[str, tuple[str, ...]] = {
    "tenant-authorization": (
        "authorization",
        "identity",
        "tenant",
        "policy",
        "deps.py",
    ),
    "audit-outbox": ("audit", "outbox", "messaging", "events.py"),
    "workflow-tasks": ("workflow", "tasks", "queue", "sla"),
    "documents-capture": ("document", "capture", "storage", "imports"),
    "frontend-api": ("frontend/src/api/", "backend/app/interfaces/http/"),
    "retention-governance": ("retention", "records", "governance", "legal_hold"),
}

FLOWS: dict[str, tuple[str, ...]] = {
    "case-intake": ("cases/create", "create_case", "case_repository", "CreateCase"),
    "workflow-transition": ("execute_transition", "workflow", "task_repository"),
    "document-upload": ("documents/upload", "document_store", "upload_document"),
    "decision-close": ("decision", "close_case", "timeline"),
}


def _read_inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Load pinned manifests and the pilot report; fail if snapshots differ."""
    repository = units.read_json(units.REPOSITORY_MANIFEST)
    dependencies = units.read_json(units.DEPENDENCY_MANIFEST)
    report = units.read_json(REVIEW / "report.json")
    snapshot = repository["snapshot"]["commit"]
    if (
        dependencies["snapshot"]["commit"] != snapshot
        or report["snapshot_commit"] != snapshot
    ):
        raise ValueError(
            "inventory, dependency graph, and pilot report snapshots differ"
        )
    return repository, dependencies, report


def _risk(path: str, row: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> int:
    """Rank paths reproducibly using size, graph centrality, and sensitive boundaries."""
    node = nodes.get(path, {})
    degree = int(node.get("fan_in", 0)) + int(node.get("fan_out", 0))
    boundary = sum(
        term in path.lower()
        for term in (
            "auth",
            "tenant",
            "audit",
            "workflow",
            "persistence",
            "outbox",
            "secrets",
        )
    )
    return min(degree, 100) * 2 + min(int(row["loc"]), 1000) // 20 + boundary * 40


def _task(
    task_id: str,
    phase: str,
    title: str,
    deps: list[str],
    *,
    files: list[str] | None = None,
    edges: list[dict[str, str]] | None = None,
    finding_id: str | None = None,
    agent: bool = True,
) -> dict[str, Any]:
    """Create one self-contained, explicitly dependent plan node."""
    scope: dict[str, Any] = {"files": sorted(files or []), "edges": edges or []}
    if finding_id is not None:
        scope["finding_id"] = finding_id
    return {
        "id": task_id,
        "phase": phase,
        "title": title,
        "deps": sorted(set(deps)),
        "scope": scope,
        "agent": MODEL if agent else None,
        "acceptance": (
            "Return one structured finding artifact with source-line evidence and coverage "
            "for every scoped item; do not edit source."
            if agent
            else "Produce a deterministic coverage or aggregation artifact from prior outputs."
        ),
    }


def _local_units(
    rows: list[dict[str, Any]], nodes: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    """Assign every handwritten file to one local unit of at most six files/1800 LOC."""
    by_component: dict[str, list[dict[str, Any]]] = defaultdict(list)
    excluded: list[str] = []
    for row in rows:
        path = row["path"]
        if units.generated_path(path, row) or path.endswith("/pnpm-lock.yaml"):
            excluded.append(path)
        else:
            by_component[row["component"]].append(row)
    tasks: list[dict[str, Any]] = []
    owner: dict[str, str] = {}
    for component, members in sorted(by_component.items()):
        batch: list[dict[str, Any]] = []
        loc = 0
        for row in sorted(members, key=lambda item: item["path"]):
            if row["loc"] > MAX_LOC:
                raise ValueError(
                    f"oversize source needs a line-range review unit: {row['path']}"
                )
            if batch and (len(batch) == MAX_FILES or loc + row["loc"] > MAX_LOC):
                _append_local(tasks, owner, batch, component)
                batch, loc = [], 0
            batch.append(row)
            loc += row["loc"]
        if batch:
            _append_local(tasks, owner, batch, component)
    # Higher-risk units run first without changing deterministic ownership.
    tasks.sort(
        key=lambda task: (
            -max(
                _risk(p, next(r for r in rows if r["path"] == p), nodes)
                for p in task["scope"]["files"]
            ),
            task["id"],
        )
    )
    return tasks, owner, sorted(excluded)


def _append_local(
    tasks: list[dict[str, Any]],
    owner: dict[str, str],
    batch: list[dict[str, Any]],
    component: str,
) -> None:
    """Append one local review node and record exclusive file ownership."""
    task_id = f"local-{len(tasks) + 1:04d}"
    paths = [row["path"] for row in batch]
    tasks.append(
        _task(task_id, "local", f"Review {component} source batch", [], files=paths)
    )
    owner.update({path: task_id for path in paths})


def _second_pass(
    local: list[dict[str, Any]],
    rows: dict[str, dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Independently review the highest-risk tenth of local file groups."""
    ranked = sorted(
        local,
        key=lambda task: (
            -max(_risk(p, rows[p], nodes) for p in task["scope"]["files"]),
            task["id"],
        ),
    )
    count = max(1, (len(ranked) + 9) // 10)
    return [
        _task(
            f"second-{index:04d}",
            "independent-second-pass",
            f"Independent review of {original['id']}",
            [],
            files=original["scope"]["files"],
        )
        for index, original in enumerate(ranked[:count], 1)
    ]


def _edge_units(
    edges: list[dict[str, Any]], owner: dict[str, str]
) -> list[dict[str, Any]]:
    """Cover resolved cross-batch edges in bounded source-neighborhood groups."""
    by_source: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for edge in edges:
        source, target = edge.get("source"), edge.get("target")
        if (
            edge.get("resolution") == "resolved"
            and source in owner
            and target in owner
            and owner[source] != owner[target]
        ):
            by_source[owner[source]].add(
                (source, target, str(edge.get("kind", "link")))
            )
    tasks: list[dict[str, Any]] = []
    for source_unit, records in sorted(by_source.items()):
        ordered = sorted(records)
        for start in range(0, len(ordered), MAX_EDGES):
            chunk = ordered[start : start + MAX_EDGES]
            task_id = f"edge-{len(tasks) + 1:04d}"
            files = sorted(
                {path for source, target, _kind in chunk for path in (source, target)}
            )
            deps = [owner[path] for path in files]
            edge_rows = [
                {"source": source, "target": target, "kind": kind}
                for source, target, kind in chunk
            ]
            tasks.append(
                _task(
                    task_id,
                    "edge",
                    f"Trace dependency neighborhood of {source_unit}",
                    deps,
                    files=files,
                    edges=edge_rows,
                )
            )
    return tasks


def _unresolved_units(
    edges: list[dict[str, Any]], owner: dict[str, str]
) -> list[dict[str, Any]]:
    """Examine unresolved imports instead of hiding them from edge coverage."""
    by_source: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for edge in edges:
        source = edge.get("source")
        if edge.get("resolution") == "unresolved" and source in owner:
            by_source[owner[source]].add(
                (
                    source,
                    str(edge.get("specifier", "")),
                    str(edge.get("kind", "import")),
                )
            )
    tasks: list[dict[str, Any]] = []
    for source_unit, records in sorted(by_source.items()):
        ordered = sorted(records)
        for start in range(0, len(ordered), MAX_EDGES):
            chunk = ordered[start : start + MAX_EDGES]
            edge_rows = [
                {"source": source, "specifier": specifier, "kind": kind}
                for source, specifier, kind in chunk
            ]
            tasks.append(
                _task(
                    f"unresolved-{len(tasks) + 1:03d}",
                    "unresolved-edge",
                    f"Resolve or classify imports from {source_unit}",
                    [source_unit],
                    files=sorted({source for source, _specifier, _kind in chunk}),
                    edges=edge_rows,
                )
            )
    return tasks


def _subsystem_units(
    rows: dict[str, dict[str, Any]],
    owner: dict[str, str],
    edge_units: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Review each multi-batch component after local and relevant edge units."""
    components: dict[str, set[str]] = defaultdict(set)
    for path, task_id in owner.items():
        components[rows[path]["component"]].add(task_id)
    tasks: list[dict[str, Any]] = []
    for component, local_ids in sorted(components.items()):
        if len(local_ids) < 2:
            continue
        relevant_edges = [
            task["id"]
            for task in edge_units
            if any(
                rows[path]["component"] == component for path in task["scope"]["files"]
            )
        ]
        paths = [path for path in owner if rows[path]["component"] == component]
        # A subsystem node points to the complete component, not an unbounded pasted source blob.
        tasks.append(
            _task(
                f"subsystem-{len(tasks) + 1:03d}",
                "subsystem",
                f"Review {component} subsystem behavior",
                sorted(local_ids | set(relevant_edges)),
                files=paths,
            )
        )
    return tasks


def _themed_units(
    themes: dict[str, tuple[str, ...]],
    phase: str,
    rows: dict[str, dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    owner: dict[str, str],
    subsystem: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Select bounded, high-centrality entry points for cross-cut and E2E traces."""
    tasks: list[dict[str, Any]] = []
    for name, terms in themes.items():
        matches = [
            path
            for path in owner
            if any(term.lower() in path.lower() for term in terms)
        ]
        matches.sort(key=lambda path: (-_risk(path, rows[path], nodes), path))
        selected = matches[:12]
        if not selected:
            continue
        deps = {owner[path] for path in selected}
        deps.update(
            task["id"]
            for task in subsystem
            if any(path in task["scope"]["files"] for path in selected)
        )
        tasks.append(
            _task(
                f"{phase}-{name}",
                phase,
                f"Trace {name.replace('-', ' ')} boundaries",
                sorted(deps),
                files=selected,
            )
        )
    return tasks


def build_plan(task_cap: int) -> dict[str, Any]:
    """Return a deterministic, capped graph and honest coverage gaps."""
    if task_cap < 1:
        raise ValueError("task cap must be positive")
    repository, dependencies, report = _read_inputs()
    rows = {row["path"]: row for row in repository["source_files"]}
    nodes = {node["path"]: node for node in dependencies["nodes"]}
    local, owner, excluded = _local_units(list(rows.values()), nodes)
    second = _second_pass(local, rows, nodes)
    edge = _edge_units(dependencies["edges"], owner)
    unresolved = _unresolved_units(dependencies["edges"], owner)
    subsystem = _subsystem_units(rows, owner, edge)
    integration = _themed_units(
        INTEGRATIONS, "integration", rows, nodes, owner, subsystem
    )
    e2e = _themed_units(FLOWS, "e2e", rows, nodes, owner, subsystem)
    review_tasks = local + second + edge + unresolved + subsystem + integration + e2e
    coverage = _task(
        "coverage-ledger",
        "coverage",
        "Verify file and edge coverage",
        [task["id"] for task in local + edge + unresolved],
        agent=False,
    )
    dedup = _task(
        "deduplicate-findings",
        "deduplication",
        "Group duplicate findings",
        [task["id"] for task in review_tasks] + [coverage["id"]],
        agent=False,
    )
    verifiers = [
        _task(
            f"verify-{finding['finding_id']}",
            "finding-verifier",
            f"Independently verify {finding['finding_id']}",
            [dedup["id"]],
            files=[finding["location"]["path"]],
            finding_id=finding["finding_id"],
        )
        for finding in report["findings"]
    ]
    factory = _task(
        "expand-new-finding-verifiers",
        "verifier-expansion",
        "Create a verifier for every new full-run finding",
        [dedup["id"]],
        agent=False,
    )
    handoff = _task(
        "review-handoff",
        "handoff",
        "Publish verified review coverage and findings",
        [factory["id"]] + [task["id"] for task in verifiers],
        agent=False,
    )
    candidates = review_tasks + [coverage, dedup] + verifiers + [factory, handoff]
    accepted: list[dict[str, Any]] = []
    accepted_ids: set[str] = set()
    for task in candidates:
        if len(accepted) >= task_cap:
            break
        if all(dep in accepted_ids for dep in task["deps"]):
            accepted.append(task)
            accepted_ids.add(task["id"])
    reviewed_files = {
        path
        for task in accepted
        if task["phase"] == "local"
        for path in task["scope"]["files"]
    }
    reviewed_edges = {
        tuple(item.values())
        for task in accepted
        if task["phase"] == "edge"
        for item in task["scope"]["edges"]
    }
    all_edges = {
        tuple(item.values()) for task in edge for item in task["scope"]["edges"]
    }
    reviewed_unresolved = {
        tuple(item.values())
        for task in accepted
        if task["phase"] == "unresolved-edge"
        for item in task["scope"]["edges"]
    }
    all_unresolved = {
        tuple(item.values()) for task in unresolved for item in task["scope"]["edges"]
    }
    return {
        "schema_version": 1,
        "snapshot_commit": repository["snapshot"]["commit"],
        "source_inventory_count": len(rows),
        "excluded_from_local_review": excluded,
        "limits": {
            "task_cap": task_cap,
            "files_per_local_unit": MAX_FILES,
            "loc_per_local_unit": MAX_LOC,
            "edges_per_edge_unit": MAX_EDGES,
            "parallel_workers": 4,
        },
        "summary": {
            "planned_tasks": len(candidates),
            "included_tasks": len(accepted),
            "phase_counts": dict(sorted(Counter(t["phase"] for t in accepted).items())),
            "planned_model_requests": sum(t["agent"] is not None for t in accepted),
            "uncovered_local_files": len(set(owner) - reviewed_files),
            "uncovered_cross_batch_edges": len(all_edges - reviewed_edges),
            "uncovered_unresolved_edges": len(all_unresolved - reviewed_unresolved),
        },
        "uncovered_files": sorted(set(owner) - reviewed_files),
        "uncovered_edges": [
            list(edge_key) for edge_key in sorted(all_edges - reviewed_edges)
        ],
        "uncovered_unresolved_edges": [
            list(edge_key) for edge_key in sorted(all_unresolved - reviewed_unresolved)
        ],
        "tasks": accepted,
    }


def main() -> int:
    """Write the dry-run plan only; never initialize, approve, or execute a DAG."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-cap", type=int, default=1000)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    try:
        plan = build_plan(args.task_cap)
        output = args.output.resolve()
        output.relative_to(REVIEW.resolve())
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"full review plan rejected: {error}\n")
    print(json.dumps(plan["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
