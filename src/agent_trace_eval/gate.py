"""CI thresholds: fail the build if quality or $ / task regresses."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_THRESHOLDS = Path(__file__).resolve().parents[2] / "eval" / "thresholds.yaml"
DEFAULT_BASELINE = Path(__file__).resolve().parents[2] / "eval" / "baseline.json"


@dataclass
class GateFailure:
    metric: str
    actual: float
    limit: float
    message: str


@dataclass
class GateResult:
    passed: bool
    failures: list[GateFailure] = field(default_factory=list)

    def raise_for_ci(self) -> None:
        if self.passed:
            return
        lines = ["eval gate FAILED:"]
        lines.extend(f"  - {f.message}" for f in self.failures)
        raise AssertionError("\n".join(lines))


def load_thresholds(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_THRESHOLDS
    with p.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_baseline(path: Path | None = None) -> dict[str, Any] | None:
    p = path or DEFAULT_BASELINE
    if not p.exists():
        return None
    import json

    return json.loads(p.read_text(encoding="utf-8"))


def check_gate(
    summary: dict[str, Any],
    thresholds: dict[str, Any] | None = None,
    baseline: dict[str, Any] | None = None,
) -> GateResult:
    th = thresholds or load_thresholds()
    failures: list[GateFailure] = []

    def _fail(metric: str, actual: float, limit: float, msg: str) -> None:
        failures.append(GateFailure(metric, actual, limit, msg))

    success = float(summary["success_rate"])
    usd = float(summary["usd_per_task"])
    schema = float(summary["schema_validity"])
    recall = float(summary["tool_recall"])
    grounded = float(summary["groundedness"])
    p95 = float(summary["p95_latency_ms"])

    if success < float(th["min_success_rate"]):
        _fail(
            "success_rate",
            success,
            th["min_success_rate"],
            f"success_rate {success:.3f} < min {th['min_success_rate']}",
        )
    if usd > float(th["max_usd_per_task"]):
        _fail(
            "usd_per_task",
            usd,
            th["max_usd_per_task"],
            f"$/task {usd:.6f} > max {th['max_usd_per_task']}",
        )
    if schema < float(th["min_schema_validity"]):
        _fail(
            "schema_validity",
            schema,
            th["min_schema_validity"],
            f"schema_validity {schema:.3f} < min {th['min_schema_validity']}",
        )
    if recall < float(th["min_tool_recall"]):
        _fail(
            "tool_recall",
            recall,
            th["min_tool_recall"],
            f"tool_recall {recall:.3f} < min {th['min_tool_recall']}",
        )
    if grounded < float(th["min_groundedness"]):
        _fail(
            "groundedness",
            grounded,
            th["min_groundedness"],
            f"groundedness {grounded:.3f} < min {th['min_groundedness']}",
        )
    g_recall = float(summary.get("guardrail_recall") or 1.0)
    g_prec = float(summary.get("guardrail_precision") or 1.0)
    min_g_recall = float(th.get("min_guardrail_recall", 1.0))
    min_g_prec = float(th.get("min_guardrail_precision", 0.8))
    if g_recall < min_g_recall:
        _fail(
            "guardrail_recall",
            g_recall,
            min_g_recall,
            f"guardrail_recall {g_recall:.3f} < min {min_g_recall} "
            f"(fn={summary.get('guardrail_fn')})",
        )
    if g_prec < min_g_prec:
        _fail(
            "guardrail_precision",
            g_prec,
            min_g_prec,
            f"guardrail_precision {g_prec:.3f} < min {min_g_prec} "
            f"(fp={summary.get('guardrail_fp')})",
        )
    if p95 > float(th["max_p95_latency_ms"]):
        _fail(
            "p95_latency_ms",
            p95,
            th["max_p95_latency_ms"],
            f"p95 latency {p95:.1f}ms > max {th['max_p95_latency_ms']}ms",
        )

    if baseline:
        floor = float(baseline["success_rate"]) - float(th.get("max_success_drop", 0.02))
        if success < floor:
            _fail(
                "success_rate_regression",
                success,
                floor,
                f"success_rate {success:.3f} dropped vs baseline {baseline['success_rate']:.3f}",
            )
        cost_ceiling = float(baseline["usd_per_task"]) * float(th.get("max_cost_ratio", 1.25))
        if usd > cost_ceiling:
            _fail(
                "usd_per_task_regression",
                usd,
                cost_ceiling,
                f"$/task {usd:.6f} exceeded baseline {baseline['usd_per_task']:.6f} * "
                f"{th.get('max_cost_ratio', 1.25)}",
            )

    return GateResult(passed=not failures, failures=failures)
