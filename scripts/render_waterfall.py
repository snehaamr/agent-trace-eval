#!/usr/bin/env python3
"""Render a Jaeger-like waterfall HTML from artifacts/traces.jsonl."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSONL = ROOT / "artifacts" / "traces.jsonl"
DEFAULT_HTML = ROOT / "docs" / "jaeger_waterfall.html"

COLORS = {
    "invoke_agent": "#b45aff",
    "invoke_workflow": "#3d6df2",
    "chat": "#39c5cf",
    "execute_tool": "#d6a840",
    "retrieval": "#5cc45c",
    "apply_guardrail": "#e5534b",
}


def load_spans(path: Path) -> list[dict]:
    spans = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            spans.append(json.loads(line))
    return spans


def pick_trace(spans: list[dict]) -> str | None:
    """Prefer settlement-mismatch (LLM + tool + retrieval + guardrail + page)."""
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        by_trace[s["trace_id"]].append(s)
    preferred = None
    scored = []
    for tid, group in by_trace.items():
        names = " ".join(s["name"] for s in group)
        attrs = " ".join(json.dumps(s.get("attributes") or {}) for s in group)
        if "settlement-mismatch-page" in attrs:
            preferred = tid
        score = sum(
            key in names
            for key in ("invoke_agent", "chat", "execute_tool", "retrieval", "apply_guardrail")
        )
        scored.append((score, len(group), tid))
    if preferred:
        return preferred
    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][2]


def render(spans: list[dict], trace_id: str) -> str:
    group = [s for s in spans if s["trace_id"] == trace_id]
    group.sort(key=lambda s: s["start_time_unix_nano"] or 0)
    t0 = min(s["start_time_unix_nano"] for s in group)
    t1 = max(s["end_time_unix_nano"] or s["start_time_unix_nano"] for s in group)
    width_ns = max(t1 - t0, 1)
    rows = []
    for s in group:
        start = s["start_time_unix_nano"] - t0
        dur = max((s["end_time_unix_nano"] or s["start_time_unix_nano"]) - s["start_time_unix_nano"], 1)
        left = 100.0 * start / width_ns
        w = max(0.8, 100.0 * dur / width_ns)
        op = (s.get("attributes") or {}).get("gen_ai.operation.name") or s["name"].split(" ")[0]
        color = COLORS.get(op, "#888")
        dur_ms = dur / 1_000_000
        attrs = s.get("attributes") or {}
        interesting = {
            k: attrs[k]
            for k in sorted(attrs)
            if k.startswith("gen_ai.") or k.startswith("guardrail.")
        }
        rows.append(
            f"""<div class="row">
  <div class="label" title="{s['name']}">{s['name']}</div>
  <div class="track">
    <div class="bar" style="left:{left:.2f}%;width:{w:.2f}%;background:{color}"
         title="{s['name']} {dur_ms:.2f}ms"></div>
  </div>
  <div class="dur">{dur_ms:.2f}ms</div>
  <pre class="attrs">{json.dumps(interesting, indent=2)[:1200]}</pre>
</div>"""
        )
    body = "\n".join(rows)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>agent-trace-eval · trace {trace_id[:16]}</title>
<style>
  :root {{ font-family: ui-sans-serif, system-ui, sans-serif; }}
  body {{ margin: 0; background: #1a1c2a; color: #e8e8f0; }}
  header {{ padding: 16px 24px; background: #12141f; border-bottom: 1px solid #2c3150; }}
  h1 {{ font-size: 18px; margin: 0 0 6px; font-weight: 600; }}
  .sub {{ color: #9aa0c3; font-size: 12px; }}
  .legend {{ display: flex; gap: 12px; padding: 12px 24px; font-size: 12px; color: #c5c9e8; }}
  .swatch {{ width: 10px; height: 10px; display: inline-block; border-radius: 2px; margin-right: 4px; }}
  .row {{ display: grid; grid-template-columns: 260px 1fr 80px; gap: 8px; padding: 6px 24px;
          border-bottom: 1px solid #23263a; align-items: start; }}
  .label {{ font-size: 12px; color: #d0d4f0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .track {{ position: relative; height: 18px; background: #12141f; border-radius: 3px; margin-top: 2px; }}
  .bar {{ position: absolute; top: 2px; height: 14px; border-radius: 3px; min-width: 4px; }}
  .dur {{ font-size: 11px; color: #8b90b3; text-align: right; }}
  .attrs {{ display: none; grid-column: 1 / -1; background: #12141f; color: #b9c0e8; font-size: 11px;
            padding: 8px; border-radius: 4px; overflow: auto; }}
  .row:hover .attrs {{ display: block; }}
</style>
</head>
<body>
<header>
  <h1>Jaeger-style waterfall · FastPay exception triage</h1>
  <div class="sub">trace_id={trace_id} · {len(group)} spans · hover a row for gen_ai.* attributes</div>
</header>
<div class="legend">
  <span><i class="swatch" style="background:#b45aff"></i>invoke_agent</span>
  <span><i class="swatch" style="background:#39c5cf"></i>chat</span>
  <span><i class="swatch" style="background:#d6a840"></i>execute_tool</span>
  <span><i class="swatch" style="background:#5cc45c"></i>retrieval</span>
  <span><i class="swatch" style="background:#e5534b"></i>apply_guardrail</span>
  <span><i class="swatch" style="background:#3d6df2"></i>invoke_workflow</span>
</div>
{body}
</body>
</html>
"""


def main() -> int:
    jsonl = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_JSONL
    html_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_HTML
    spans = load_spans(jsonl)
    tid = pick_trace(spans)
    if not tid:
        print("no spans", file=sys.stderr)
        return 1
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render(spans, tid), encoding="utf-8")
    print(html_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
