"""Export eval + GenAI usage as Prometheus text (and optional OTel metrics)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROM = REPO_ROOT / "artifacts" / "metrics.prom"


def prometheus_text(summary: dict[str, Any], scores: list[dict[str, Any]] | None = None) -> str:
    lines = [
        "# HELP eval_success_rate Fraction of labeled tasks that passed.",
        "# TYPE eval_success_rate gauge",
        f"eval_success_rate {float(summary.get('success_rate') or 0):.6f}",
        "# HELP eval_usd_per_task Estimated USD per task at gpt-4o-mini list prices.",
        "# TYPE eval_usd_per_task gauge",
        f"eval_usd_per_task {float(summary.get('usd_per_task') or 0):.8f}",
        "# HELP eval_tool_recall Mean required-tool recall.",
        "# TYPE eval_tool_recall gauge",
        f"eval_tool_recall {float(summary.get('tool_recall') or 0):.6f}",
        "# HELP eval_groundedness Mean groundedness vs tool output.",
        "# TYPE eval_groundedness gauge",
        f"eval_groundedness {float(summary.get('groundedness') or 0):.6f}",
        "# HELP eval_guardrail_recall Recall of expected hard blocks.",
        "# TYPE eval_guardrail_recall gauge",
        f"eval_guardrail_recall {float(summary.get('guardrail_recall') or 0):.6f}",
        "# HELP eval_guardrail_precision Precision of hard blocks.",
        "# TYPE eval_guardrail_precision gauge",
        f"eval_guardrail_precision {float(summary.get('guardrail_precision') or 0):.6f}",
        "# HELP eval_p95_latency_ms p95 wall-clock latency of a task.",
        "# TYPE eval_p95_latency_ms gauge",
        f"eval_p95_latency_ms {float(summary.get('p95_latency_ms') or 0):.3f}",
        "# HELP gen_ai_client_token_usage Mean tokens per task by type.",
        "# TYPE gen_ai_client_token_usage gauge",
        f'gen_ai_client_token_usage{{gen_ai_token_type="input"}} {float(summary.get("mean_input_tokens") or 0):.3f}',
        f'gen_ai_client_token_usage{{gen_ai_token_type="output"}} {float(summary.get("mean_output_tokens") or 0):.3f}',
    ]
    if scores:
        lines += [
            "# HELP eval_task_success 1 if the labeled task passed.",
            "# TYPE eval_task_success gauge",
        ]
        for row in scores:
            val = 1 if row.get("success") else 0
            lines.append(f'eval_task_success{{task_id="{row["task_id"]}"}} {val}')
    return "\n".join(lines) + "\n"


def write_prometheus(
    summary: dict[str, Any],
    scores: list[dict[str, Any]] | None = None,
    path: Path | None = None,
) -> Path:
    path = path or DEFAULT_PROM
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prometheus_text(summary, scores), encoding="utf-8")
    return path
