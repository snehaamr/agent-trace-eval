"""Run the labeled task set, score it, and optionally enforce the CI gate."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry.trace import SpanKind

from .agent import run_agent
from .gate import check_gate, load_baseline, load_thresholds
from .llm import build_llm
from .metrics import TaskScore, guardrail_stats, score_task
from .metrics_export import write_prometheus
from .span_diff import diff_shapes, flatten_ops, load_goldens, shapes_from_spans, write_goldens
from .tasks import load_tasks
from .telemetry import genai_span, set_json_attr, setup_tracer
from .world import World

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_JSONL = REPO_ROOT / "artifacts" / "traces.jsonl"
DEFAULT_REPORT = REPO_ROOT / "eval" / "last_report.json"


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * p
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def summarize(scores: list[TaskScore]) -> dict[str, Any]:
    n = len(scores) or 1
    latencies = [s.latency_ms for s in scores]
    summary = {
        "n_tasks": len(scores),
        "n_pass": sum(1 for s in scores if s.success),
        "success_rate": sum(1 for s in scores if s.success) / n,
        "usd_per_task": sum(s.cost_usd for s in scores) / n,
        "total_usd": sum(s.cost_usd for s in scores),
        "schema_validity": sum(1 for s in scores if s.schema_valid) / n,
        "tool_recall": sum(s.tool_recall for s in scores) / n,
        "groundedness": sum(s.groundedness for s in scores) / n,
        "p50_latency_ms": percentile(latencies, 0.50),
        "p95_latency_ms": percentile(latencies, 0.95),
        "mean_latency_ms": statistics.mean(latencies) if latencies else 0.0,
        "mean_input_tokens": sum(s.input_tokens for s in scores) / n,
        "mean_output_tokens": sum(s.output_tokens for s in scores) / n,
    }
    summary.update(guardrail_stats(scores))
    return summary


def markdown_table(scores: list[TaskScore], summary: dict[str, Any]) -> str:
    lines = [
        "| Task | Pass | Decision | Tools | Grounded | $ | ms |",
        "|---|:---:|---|---|---:|---:|---:|",
    ]
    for s in scores:
        mark = "yes" if s.success else "NO"
        tools = ",".join(s.called_tools) if s.called_tools else "—"
        lines.append(
            f"| `{s.task_id}` | {mark} | {s.actual_decision} | `{tools}` | "
            f"{s.groundedness:.2f} | {s.cost_usd:.6f} | {s.latency_ms:.1f} |"
        )
    lines.append("")
    lines.append(
        f"**{summary['n_pass']}/{summary['n_tasks']} passed** · "
        f"success {summary['success_rate']:.1%} · "
        f"${summary['usd_per_task']:.6f}/task · "
        f"tool recall {summary['tool_recall']:.2f} · "
        f"groundedness {summary['groundedness']:.2f} · "
        f"p95 {summary['p95_latency_ms']:.1f} ms"
    )
    return "\n".join(lines)


def run_suite(
    *,
    jsonl_path: Path | None = None,
    enforce_gate: bool = False,
    update_goldens: bool = False,
    enforce_span_diff: bool = False,
    slice: str | None = None,
) -> dict[str, Any]:
    sink: list[dict[str, Any]] = []
    setup_tracer(jsonl_path=str(jsonl_path or DEFAULT_JSONL), sink=sink)
    llm = build_llm()
    tasks = load_tasks(slice=slice)
    scores: list[TaskScore] = []
    with genai_span(
        "invoke_workflow eval-suite",
        operation="invoke_workflow",
        kind=SpanKind.INTERNAL,
        attributes={
            "gen_ai.provider.name": llm.provider,
            "gen_ai.workflow.name": "eval-suite",
            "gen_ai.agent.name": "fastpay-exception-triage",
        },
    ):
        for task in tasks:
            world = World.seed()
            # One trace per task so Jaeger/CI artifacts stay readable.
            token = otel_context.attach(otel_context.Context())
            try:
                with genai_span(
                    f"eval.task {task['id']}",
                    operation="invoke_workflow",
                    kind=SpanKind.INTERNAL,
                    attributes={
                        "gen_ai.provider.name": llm.provider,
                        "gen_ai.workflow.name": task["id"],
                        "gen_ai.conversation.id": task["id"],
                    },
                ) as span:
                    result = run_agent(
                        task["prompt"],
                        world=world,
                        llm=llm,
                        conversation_id=task["id"],
                    )
                    score = score_task(task, result)
                    span.set_attribute("gen_ai.evaluation.name", "task_success")
                    span.set_attribute("gen_ai.evaluation.score.value", 1.0 if score.success else 0.0)
                    span.set_attribute(
                        "gen_ai.evaluation.score.label", "pass" if score.success else "fail"
                    )
                    set_json_attr(span, "gen_ai.evaluation.explanation", "; ".join(score.reasons) or "ok")
                    scores.append(score)
            finally:
                otel_context.detach(token)

    summary = summarize(scores)
    shapes = shapes_from_spans(sink)
    if update_goldens:
        write_goldens(shapes)
    report = {
        "summary": summary,
        "scores": [s.as_dict() for s in scores],
        "markdown": markdown_table(scores, summary),
        "n_spans": len(sink),
        "span_shapes": {k: flatten_ops(v["tree"]) for k, v in shapes.items()},
    }
    write_prometheus(summary, report["scores"])
    DEFAULT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    if enforce_gate:
        gate = check_gate(summary, load_thresholds(), load_baseline())
        report["gate_passed"] = gate.passed
        report["gate_failures"] = [f.message for f in gate.failures]
        gate.raise_for_ci()
    if enforce_span_diff:
        problems = diff_shapes(shapes, load_goldens())
        if problems:
            raise AssertionError("span goldens FAILED:\n  - " + "\n  - ".join(problems))
        report["span_diff_passed"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run FastPay agent eval + optional CI gate")
    parser.add_argument("--gate", action="store_true", help="fail the process if thresholds regress")
    parser.add_argument(
        "--update-goldens",
        action="store_true",
        help="rewrite eval/goldens/*.json from this run's span trees",
    )
    parser.add_argument(
        "--span-diff",
        action="store_true",
        help="fail if GenAI span shapes drifted from eval/goldens",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="rewrite eval/baseline.json from this run (summary + per-task rows)",
    )
    parser.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    parser.add_argument("--slice", choices=["triage", "guardrail"], default=None)
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
    )
    args = parser.parse_args(argv)
    try:
        report = run_suite(
            jsonl_path=args.jsonl,
            enforce_gate=args.gate,
            update_goldens=args.update_goldens,
            enforce_span_diff=args.span_diff,
            slice=args.slice,
        )
    except AssertionError as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.write_baseline:
        from .scorecard import write_baseline

        write_baseline(report)
    if args.format == "json":
        print(json.dumps({k: v for k, v in report.items() if k != "markdown"}, indent=2))
    else:
        print(report["markdown"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
