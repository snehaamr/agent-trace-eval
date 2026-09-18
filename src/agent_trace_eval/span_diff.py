"""Canonical span trees for CI: fail if the GenAI trace *shape* regresses.

Compared fields are operation names, tool names, and guardrail decisions —
not timestamps, IDs, or token counts.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLDENS = REPO_ROOT / "eval" / "goldens"

KEEP_ATTRS = (
    "gen_ai.operation.name",
    "gen_ai.tool.name",
    "guardrail.name",
    "guardrail.decision",
    "gen_ai.evaluation.score.label",
)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _task_id(spans: list[dict[str, Any]]) -> str | None:
    for span in spans:
        name = span.get("name") or ""
        if name.startswith("eval.task "):
            return name.split(" ", 1)[1]
        attrs = span.get("attributes") or {}
        workflow = attrs.get("gen_ai.workflow.name")
        if workflow and workflow != "eval-suite" and name.startswith("eval.task"):
            return str(workflow)
    return None


def _node(span: dict[str, Any]) -> dict[str, Any]:
    attrs = span.get("attributes") or {}
    kept = {k: attrs[k] for k in KEEP_ATTRS if k in attrs and attrs[k] not in (None, "")}
    return {
        "name": span.get("name"),
        "op": attrs.get("gen_ai.operation.name"),
        "attrs": kept,
        "children": [],
    }


def tree_for_trace(spans: list[dict[str, Any]]) -> dict[str, Any] | None:
    by_id = {s["span_id"]: s for s in spans if s.get("span_id")}
    children: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    for span in spans:
        children[span.get("parent_span_id")].append(span)

    def sort_key(span: dict[str, Any]) -> tuple:
        return (span.get("start_time_unix_nano") or 0, span.get("name") or "")

    roots = [s for s in spans if not s.get("parent_span_id") or s.get("parent_span_id") not in by_id]
    if not roots:
        return None

    def walk(span: dict[str, Any]) -> dict[str, Any]:
        node = _node(span)
        kids = sorted(children.get(span.get("span_id") or "", []), key=sort_key)
        node["children"] = [walk(child) for child in kids]
        return node

    # Prefer the eval.task root when present.
    roots_sorted = sorted(roots, key=sort_key)
    chosen = next((r for r in roots_sorted if (r.get("name") or "").startswith("eval.task ")), roots_sorted[0])
    return walk(chosen)


def shapes_from_spans(spans: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for span in spans:
        grouped[span.get("trace_id") or ""].append(span)
    out: dict[str, dict[str, Any]] = {}
    for _tid, group in grouped.items():
        task_id = _task_id(group)
        if not task_id:
            continue
        tree = tree_for_trace(group)
        if tree is None:
            continue
        out[task_id] = {"task_id": task_id, "tree": tree}
    return out


def flatten_ops(node: dict[str, Any]) -> list[str]:
    op = node.get("op") or node.get("name") or ""
    tool = (node.get("attrs") or {}).get("gen_ai.tool.name")
    label = f"{op}:{tool}" if tool else str(op)
    names = [label]
    for child in node.get("children") or []:
        names.extend(flatten_ops(child))
    return names


def write_goldens(shapes: dict[str, dict[str, Any]], directory: Path | None = None) -> Path:
    directory = directory or DEFAULT_GOLDENS
    directory.mkdir(parents=True, exist_ok=True)
    for task_id, shape in shapes.items():
        path = directory / f"{task_id}.json"
        path.write_text(json.dumps(shape, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # Drop stale goldens for deleted tasks.
    wanted = set(shapes)
    for path in directory.glob("*.json"):
        if path.stem not in wanted:
            path.unlink()
    return directory


def load_goldens(directory: Path | None = None) -> dict[str, dict[str, Any]]:
    directory = directory or DEFAULT_GOLDENS
    out: dict[str, dict[str, Any]] = {}
    if not directory.exists():
        return out
    for path in sorted(directory.glob("*.json")):
        out[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return out


def diff_shapes(
    actual: dict[str, dict[str, Any]],
    goldens: dict[str, dict[str, Any]],
) -> list[str]:
    problems: list[str] = []
    extra = sorted(set(actual) - set(goldens))
    missing = sorted(set(goldens) - set(actual))
    if extra:
        problems.append(f"tasks without goldens (run --update-goldens): {extra}")
    if missing:
        problems.append(f"goldens with no matching task: {missing}")
    for task_id in sorted(set(actual) & set(goldens)):
        got = flatten_ops(actual[task_id]["tree"])
        want = flatten_ops(goldens[task_id]["tree"])
        if got != want:
            problems.append(f"{task_id}: ops {got} != golden {want}")
        elif actual[task_id]["tree"] != goldens[task_id]["tree"]:
            problems.append(f"{task_id}: span tree attributes changed")
    return problems
