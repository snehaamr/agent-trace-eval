#!/usr/bin/env python3
"""Render a Grafana-like metrics board from eval/last_report.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "eval" / "last_report.json"
OUT = ROOT / "docs" / "grafana_eval_board.html"


def render(report: dict) -> str:
    s = report["summary"]
    scores = report.get("scores") or []
    max_cost = max((row["cost_usd"] for row in scores), default=0.0001) or 0.0001
    rows = []
    for row in scores:
        width = 100.0 * row["cost_usd"] / max_cost
        color = "#73bf69" if row["success"] else "#e02f44"
        rows.append(
            f'<div class="barrow"><span class="name">{row["task_id"]}</span>'
            f'<div class="track"><i style="width:{width:.1f}%;background:{color}"></i></div>'
            f'<span class="usd">${row["cost_usd"]:.6f}</span></div>'
        )
    bars = "\n".join(rows)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>agent-trace-eval · Grafana-style board</title>
<style>
  body {{ margin: 0; background: #111217; color: #d8d9da; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
  header {{ padding: 16px 24px; border-bottom: 1px solid #2c2c32; }}
  h1 {{ margin: 0; font-size: 16px; font-weight: 600; }}
  .sub {{ color: #8e8e8e; font-size: 12px; margin-top: 4px; }}
  .stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; padding: 16px 24px; }}
  .stat {{ background: #181b1f; border: 1px solid #2c2c32; border-radius: 4px; padding: 14px 16px; }}
  .stat .k {{ font-size: 11px; color: #8e8e8e; text-transform: uppercase; }}
  .stat .v {{ font-size: 28px; font-weight: 600; margin-top: 6px; }}
  .ok {{ color: #73bf69; }}
  .panel {{ margin: 0 24px 24px; background: #181b1f; border: 1px solid #2c2c32; border-radius: 4px; padding: 12px 16px; }}
  .panel h2 {{ margin: 0 0 12px; font-size: 13px; font-weight: 600; color: #9fa0a2; }}
  .barrow {{ display: grid; grid-template-columns: 220px 1fr 90px; gap: 8px; align-items: center; margin: 4px 0; font-size: 12px; }}
  .track {{ background: #111217; height: 10px; border-radius: 2px; }}
  .track i {{ display: block; height: 10px; border-radius: 2px; }}
  .usd {{ text-align: right; color: #9fa0a2; font-variant-numeric: tabular-nums; }}
</style>
</head>
<body>
<header>
  <h1>agent-trace-eval</h1>
  <div class="sub">Prometheus gauges from the last suite run · same numbers Grafana scrapes via artifacts/metrics.prom</div>
</header>
<div class="stats">
  <div class="stat"><div class="k">Success rate</div><div class="v ok">{s['success_rate']:.1%}</div></div>
  <div class="stat"><div class="k">$ / task</div><div class="v">${s['usd_per_task']:.6f}</div></div>
  <div class="stat"><div class="k">Guardrail recall</div><div class="v ok">{s.get('guardrail_recall', 0):.1%}</div></div>
  <div class="stat"><div class="k">GenAI tokens in / out</div><div class="v">{s.get('mean_input_tokens', 0):.0f} / {s.get('mean_output_tokens', 0):.0f}</div></div>
</div>
<div class="panel">
  <h2>USD per labeled task (green = pass)</h2>
  {bars}
</div>
</body>
</html>
"""


def main() -> int:
    report_path = Path(sys.argv[1]) if len(sys.argv) > 1 else REPORT
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else OUT
    report = json.loads(report_path.read_text(encoding="utf-8"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(report), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
