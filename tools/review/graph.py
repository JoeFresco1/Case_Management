"""Build a deterministic dependency graph from review inventory and CodeGraph.

The inventory pins source selection to an immutable Git snapshot. CodeGraph
annotations are reused only for files whose content hash matches that snapshot;
imports and unresolved references are otherwise extracted deterministically.
Runtime output is written under ``.review`` using an atomic replace so an
interrupted invocation can be resumed by running the same command again.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import posixpath
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INVENTORY = ".review/index/repository.json"
DEFAULT_CODEGRAPH = ".codegraph/codegraph.db"
DEFAULT_OUTPUT = ".review/index/dependencies.json"
TS_IMPORT = re.compile(
    r"(?ms)^\s*(?:import\s+|export\s+)[\s\S]*?['\"]([^'\"]+)['\"]|"
    r"\bimport\(\s*['\"]([^'\"]+)['\"]\s*\)|"
    r"\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)"
)
TS_DECLARATION = re.compile(r"\b(?:export\s+)?(interface|type)\s+([A-Za-z_$][\w$]*)")
TS_ROUTE = re.compile(r"\bpath\s*=\s*['\"]([^'\"]+)['\"]")


def run_git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    """Read immutable source blobs from Git, raising a clear error on failure."""
    result = subprocess.run(
        ["git", *args], cwd=root, input=input_bytes, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def snapshot_blobs(root: Path, commit: str, paths: set[str]) -> dict[str, bytes]:
    """Read all requested snapshot files in one Git batch process."""
    raw_tree = run_git(root, "ls-tree", "-r", "-z", "--full-tree", commit)
    object_ids: dict[str, str] = {}
    for raw_entry in raw_tree.split(b"\0"):
        if not raw_entry:
            continue
        header, raw_path = raw_entry.split(b"\t", 1)
        mode, object_type, object_id = header.decode("ascii").split()
        path = raw_path.decode("utf-8", "surrogateescape")
        if object_type == "blob" and path in paths:
            object_ids[path] = object_id
    missing = paths - object_ids.keys()
    if missing:
        raise RuntimeError(f"inventory paths missing from snapshot: {', '.join(sorted(missing)[:5])}")
    request = "".join(f"{object_ids[path]}\n" for path in sorted(paths)).encode("ascii")
    response = run_git(root, "cat-file", "--batch", input_bytes=request)
    blobs: dict[str, bytes] = {}
    cursor = 0
    for path in sorted(paths):
        header_end = response.find(b"\n", cursor)
        if header_end < 0:
            raise RuntimeError("git cat-file --batch returned a truncated header")
        oid, object_type, raw_size = response[cursor:header_end].decode("ascii").split()
        if oid != object_ids[path] or object_type != "blob":
            raise RuntimeError(f"unexpected Git object while reading {path}")
        size = int(raw_size)
        content_start = header_end + 1
        content_end = content_start + size
        blobs[path] = response[content_start:content_end]
        if response[content_end:content_end + 1] != b"\n":
            raise RuntimeError(f"git cat-file --batch returned a truncated blob for {path}")
        cursor = content_end + 1
    return blobs


def load_codegraph(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Load file hashes and structural annotations from the existing CodeGraph DB."""
    if not path.is_file():
        raise FileNotFoundError(f"CodeGraph index not found: {path}")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        files = {
            row["path"]: dict(row)
            for row in connection.execute("SELECT path, content_hash FROM files ORDER BY path")
        }
        nodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in connection.execute(
            "SELECT kind, name, qualified_name, file_path, start_line, decorators "
            "FROM nodes WHERE kind IN ('route', 'class', 'interface', 'type_alias', 'function') "
            "ORDER BY file_path, start_line, kind, name"
        ):
            nodes[row["file_path"]].append(dict(row))
        metadata = {
            row["key"]: row["value"]
            for row in connection.execute("SELECT key, value FROM project_metadata ORDER BY key")
        }
    finally:
        connection.close()
    return files, nodes, metadata


def module_names(path: str) -> set[str]:
    """Return stable Python module names for repository source roots."""
    posix = PurePosixPath(path)
    if posix.suffix != ".py":
        return set()
    stem = posix.with_suffix("")
    if stem.name == "__init__":
        stem = stem.parent
    parts = stem.parts
    names = {".".join(parts)}
    if len(parts) >= 2 and parts[0] == "backend":
        names.add(".".join(parts[1:]))
        if len(parts) >= 3 and parts[1] == "app":
            names.add(".".join(parts[1:]))
    return names


def python_imports(path: str, content: bytes, module_paths: dict[str, str]) -> list[dict[str, Any]]:
    """Extract Python imports and resolve only to files in this inventory."""
    try:
        tree = ast.parse(content.decode("utf-8"), filename=path)
    except (SyntaxError, UnicodeDecodeError):
        return []
    current_module = next(iter(sorted(module_names(path))), "")
    if path.endswith("/__init__.py"):
        current_package = current_module
    else:
        current_package = current_module.rpartition(".")[0]
    result: list[dict[str, Any]] = []
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            for alias in item.names:
                result.append(_python_edge(alias.name, item.lineno, module_paths))
        elif isinstance(item, ast.ImportFrom):
            base = item.module or ""
            if item.level:
                parents = current_package.split(".") if current_package else []
                keep = max(0, len(parents) - item.level + 1)
                base = ".".join(parents[:keep] + ([base] if base else []))
            edge = _python_edge(base, item.lineno, module_paths)
            # ``from package import child`` may refer to a sibling module.
            if edge["target"] is None:
                for alias in item.names:
                    if alias.name == "*":
                        continue
                    candidate = f"{base}.{alias.name}" if base else alias.name
                    if candidate in module_paths:
                        edge = _python_edge(candidate, item.lineno, module_paths)
                        break
            result.append(edge)
    return sorted(result, key=lambda edge: (edge["line"], edge["specifier"], edge["target"] or ""))


def _python_edge(specifier: str, line: int, module_paths: dict[str, str]) -> dict[str, Any]:
    """Create one Python import edge with an explicit resolution status."""
    target = module_paths.get(specifier)
    root_name = specifier.split(".", 1)[0]
    if root_name in sys.stdlib_module_names:
        resolution = "stdlib"
    elif root_name in {"app", "backend", "tests", "scripts", "tools", "alembic"}:
        resolution = "unresolved"
    else:
        resolution = "package"
    return {
        "kind": "import",
        "specifier": specifier,
        "line": line,
        "target": target,
        "resolution": "resolved" if target else resolution,
    }


def typescript_imports(content: bytes, source_paths: set[str], path: str) -> list[dict[str, Any]]:
    """Extract static, dynamic, and CommonJS TypeScript imports deterministically."""
    text = content.decode("utf-8", errors="replace")
    result = []
    for match in TS_IMPORT.finditer(text):
        specifier = next(value for value in match.groups() if value is not None)
        target = resolve_typescript(path, specifier, source_paths)
        line = text.count("\n", 0, match.start()) + 1
        resolution = "resolved" if target else ("package" if not specifier.startswith((".", "/")) else "unresolved")
        result.append({"kind": "import", "specifier": specifier, "line": line,
                       "target": target, "resolution": resolution})
    return sorted(result, key=lambda edge: (edge["line"], edge["specifier"]))


def resolve_typescript(path: str, specifier: str, source_paths: set[str]) -> str | None:
    """Resolve relative TS imports and the repository's ``@/`` source alias."""
    if specifier.startswith("@/"):
        base = PurePosixPath("frontend/src") / specifier[2:]
    elif specifier.startswith("."):
        base = PurePosixPath(path).parent / specifier
    else:
        return None
    normalized = posixpath.normpath(str(base))
    candidates = [normalized, f"{normalized}.ts", f"{normalized}.tsx", f"{normalized}.js",
                  f"{normalized}.jsx", f"{normalized}/index.ts", f"{normalized}/index.tsx"]
    return next((candidate for candidate in candidates if candidate in source_paths), None)


def classify_source(path: str, content: bytes, cg_nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Identify routes, model declarations, and executable entry points."""
    text = content.decode("utf-8", errors="replace")
    if path.endswith(".py"):
        routes, models, entry_points = _classify_python(path, text)
    else:
        routes, models, entry_points = _classify_typescript(path, text)
    for node in cg_nodes:
        if node["kind"] == "route":
            item = {"path": node["name"], "line": node["start_line"], "source": "codegraph"}
            if item not in routes:
                routes.append(item)
    return {
        "routes": sorted(routes, key=lambda item: (item.get("line", 0), item.get("path", ""), item.get("method", ""))),
        "models": sorted(models, key=lambda item: (item["line"], item["name"])),
        "entry_points": sorted(set(entry_points)),
    }


def _classify_python(path: str, text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Find literal FastAPI routes, model classes, and Python entry points."""
    routes: list[dict[str, Any]] = []
    models: list[dict[str, Any]] = []
    entry_points: list[str] = []
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError:
        tree = ast.Module(body=[], type_ignores=[])
    route_methods = {"get", "post", "put", "patch", "delete", "options", "head"}
    model_bases = {"BaseModel", "SQLModel", "DeclarativeBase", "Base", "Model"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            routes.extend(_function_routes(node, route_methods))
            if node.name == "main":
                entry_points.append(f"main:{node.lineno}")
        elif isinstance(node, ast.ClassDef) and _is_model_class(node, model_bases):
            models.append({"name": node.name, "line": node.lineno, "kind": "class"})
        elif isinstance(node, ast.If) and _is_main_guard(node.test):
            entry_points.append(f"__main__:{node.lineno}")
    if re.search(r"\b\w+\s*=\s*FastAPI\s*\(", text):
        entry_points.append("fastapi_app")
    return routes, models, entry_points


def _function_routes(node: ast.FunctionDef | ast.AsyncFunctionDef,
                     route_methods: set[str]) -> list[dict[str, Any]]:
    """Return a function's FastAPI route decorators with literal paths."""
    routes: list[dict[str, Any]] = []
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            continue
        if decorator.func.attr not in route_methods:
            continue
        path_arg = decorator.args[0] if decorator.args else None
        if isinstance(path_arg, ast.Constant) and isinstance(path_arg.value, str):
            routes.append({"method": decorator.func.attr.upper(), "path": path_arg.value,
                           "name": node.name, "line": node.lineno})
    return routes


def _is_model_class(node: ast.ClassDef, model_bases: set[str]) -> bool:
    """Recognize common Pydantic and SQLAlchemy model base classes."""
    names = {base.id if isinstance(base, ast.Name) else
             base.attr if isinstance(base, ast.Attribute) else "" for base in node.bases}
    return bool(names & model_bases)


def _classify_typescript(path: str, text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Find TypeScript interface/type models, JSX routes, and app entry points."""
    models = [{"name": match.group(2), "line": text.count("\n", 0, match.start()) + 1,
               "kind": match.group(1)} for match in TS_DECLARATION.finditer(text)]
    routes = [{"method": "ROUTE", "path": match.group(1),
               "line": text.count("\n", 0, match.start()) + 1} for match in TS_ROUTE.finditer(text)]
    entries = ["frontend_bootstrap"] if path in {"frontend/src/main.tsx", "frontend/src/main.ts"} else []
    return routes, models, entries


def _is_main_guard(test: ast.expr) -> bool:
    """Recognize the conventional Python executable module guard."""
    return isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "__name__"


def _snapshot_inputs(root: Path, inventory_path: Path, codegraph_path: Path) -> tuple[
    dict[str, Any], dict[str, bytes], dict[str, list[dict[str, Any]]], dict[str, str], set[str], dict[str, Any]
]:
    """Load inventory blobs and hash-matched CodeGraph annotations."""
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    commit = inventory["snapshot"]["commit"]
    excluded = inventory.get("excluded_sources", [])
    source_paths = {record["path"] for record in inventory["source_files"]}
    all_paths = set(source_paths)
    all_paths.update(record["path"] for record in excluded)
    blobs = snapshot_blobs(root, commit, all_paths)
    codegraph_files, codegraph_nodes, cg_metadata = load_codegraph(codegraph_path)
    reusable_nodes: dict[str, list[dict[str, Any]]] = {}
    matching_files = 0
    for path in sorted(source_paths):
        file_info = codegraph_files.get(path)
        if file_info and hashlib.sha256(blobs[path]).hexdigest() == file_info["content_hash"]:
            reusable_nodes[path] = codegraph_nodes.get(path, [])
            matching_files += 1
        else:
            reusable_nodes[path] = []
    module_paths: dict[str, str] = {}
    for path in sorted(all_paths):
        if path.endswith(".py"):
            for name in module_names(path):
                module_paths.setdefault(name, path)
    cg_summary = {"metadata": cg_metadata, "matched_snapshot_files": matching_files,
                  "stale_or_unmatched_files": len(source_paths) - matching_files}
    return inventory, blobs, reusable_nodes, module_paths, source_paths, cg_summary


def _build_nodes(inventory: dict[str, Any], blobs: dict[str, bytes],
                 cg_nodes: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Create sorted graph nodes and attach static classifications."""
    nodes: list[dict[str, Any]] = []
    for record in inventory["source_files"]:
        path = record["path"]
        classification = classify_source(path, blobs[path], cg_nodes[path])
        nodes.append({"id": path, **record, **classification,
                      "codegraph_kinds": sorted({item["kind"] for item in cg_nodes[path]})})
    return nodes


def _build_import_edges(inventory: dict[str, Any], blobs: dict[str, bytes],
                        module_paths: dict[str, str], source_paths: set[str]) -> tuple[
                            list[dict[str, Any]], list[dict[str, Any]]
                        ]:
    """Extract imports, separating unresolved project references."""
    edges: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for record in inventory["source_files"]:
        path = record["path"]
        language = record["language"]
        if language == "Python":
            imports = python_imports(path, blobs[path], module_paths)
        elif language == "TypeScript":
            imports = typescript_imports(blobs[path], source_paths, path)
        else:
            imports = []
        for item in imports:
            edge = {"source": path, **item}
            edges.append(edge)
            if item["resolution"] == "unresolved":
                unresolved.append(edge.copy())
    edges.sort(key=lambda item: (item["source"], item["line"], item["specifier"], item["target"] or ""))
    unresolved.sort(key=lambda item: (item["source"], item["line"], item["specifier"]))
    return edges, unresolved


def _attach_fan_counts(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    """Attach resolved import fan-in and fan-out counts to graph nodes."""
    in_count: Counter[str] = Counter()
    out_count: Counter[str] = Counter()
    for edge in edges:
        if edge["target"] is not None:
            out_count[edge["source"]] += 1
            in_count[edge["target"]] += 1
    for node in nodes:
        node["fan_in"] = in_count[node["id"]]
        node["fan_out"] = out_count[node["id"]]


def build_graph(root: Path, inventory_path: Path, codegraph_path: Path) -> dict[str, Any]:
    """Build graph nodes and edges for one inventory snapshot."""
    inventory, blobs, cg_nodes, module_paths, source_paths, cg_summary = _snapshot_inputs(
        root, inventory_path, codegraph_path
    )
    nodes = _build_nodes(inventory, blobs, cg_nodes)
    edges, unresolved = _build_import_edges(inventory, blobs, module_paths, source_paths)
    _attach_fan_counts(nodes, edges)
    excluded = inventory.get("excluded_sources", [])
    return {
        "schema_version": 1,
        "snapshot": inventory["snapshot"],
        "inputs": {"inventory": str(inventory_path.relative_to(root)),
                   "codegraph": str(codegraph_path.relative_to(root))},
        "codegraph": cg_summary,
        "nodes": nodes,
        "exclusions": sorted(excluded, key=lambda item: (item["reason"], item["path"])),
        "edges": edges,
        "unresolved_edges": unresolved,
        "summary": {"nodes": len(nodes), "exclusions": len(excluded), "edges": len(edges),
                    "resolved_edges": sum(edge["target"] is not None for edge in edges),
                    "unresolved_edges": len(unresolved),
                    "routes": sum(len(node["routes"]) for node in nodes),
                    "models": sum(len(node["models"]) for node in nodes),
                    "entry_points": sum(len(node["entry_points"]) for node in nodes)},
    }


def write_atomic(path: Path, document: dict[str, Any]) -> None:
    """Write stable UTF-8 JSON atomically, allowing safe reruns after interruption."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent,
                                     prefix=f".{path.name}.", suffix=".tmp", delete=False) as temp:
        temp.write(payload)
        temp_path = Path(temp.name)
    temp_path.replace(path)


def main(argv: list[str] | None = None) -> int:
    """Build the dependency index for the supplied inventory and CodeGraph DB."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--inventory", type=Path, default=Path(DEFAULT_INVENTORY))
    parser.add_argument("--codegraph", type=Path, default=Path(DEFAULT_CODEGRAPH))
    parser.add_argument("--output", type=Path, default=Path(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        inventory = (root / args.inventory).resolve()
        codegraph = (root / args.codegraph).resolve()
        output = (root / args.output).resolve()
        for path in (inventory, codegraph, output):
            path.relative_to(root)
        graph = build_graph(root, inventory, codegraph)
        write_atomic(output, graph)
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError, sqlite3.Error) as error:
        print(f"dependency graph failed: {error}", file=sys.stderr)
        return 1
    print(f"Wrote {args.output}: {graph['summary']['nodes']} nodes, "
          f"{graph['summary']['edges']} import edges")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
