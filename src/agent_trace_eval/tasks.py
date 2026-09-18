"""Load labeled eval tasks from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_TASKS = Path(__file__).resolve().parents[2] / "tasks" / "payments_exceptions.yaml"


def load_tasks(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or DEFAULT_TASKS
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    tasks = data["tasks"] if isinstance(data, dict) else data
    if not isinstance(tasks, list) or not tasks:
        raise ValueError(f"no tasks in {p}")
    ids = [t["id"] for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate task ids")
    return tasks
