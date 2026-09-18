"""Load labeled eval tasks from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

TASKS_DIR = Path(__file__).resolve().parents[2] / "tasks"
DEFAULT_TASK_FILES = [
    TASKS_DIR / "payments_exceptions.yaml",
    TASKS_DIR / "guardrail_redteam.yaml",
]


def load_tasks(
    path: Path | None = None,
    *,
    slice: str | None = None,
) -> list[dict[str, Any]]:
    files = [path] if path else [p for p in DEFAULT_TASK_FILES if p.exists()]
    tasks: list[dict[str, Any]] = []
    for p in files:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        chunk = data["tasks"] if isinstance(data, dict) else data
        if not chunk:
            continue
        tasks.extend(chunk)
    if not tasks:
        raise ValueError("no tasks loaded")
    ids = [t["id"] for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate task ids")
    if slice:
        tasks = [t for t in tasks if _slice_of(t) == slice]
    return tasks


def _slice_of(task: dict[str, Any]) -> str:
    if task.get("slice"):
        return str(task["slice"])
    if "guardrail_should_block" in (task.get("expected") or {}):
        return "guardrail"
    return "triage"
