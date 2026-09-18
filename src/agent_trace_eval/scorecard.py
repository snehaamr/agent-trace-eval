"""Per-task eval scorecard vs the checked-in baseline.

Used in CI to explain *which* tasks flipped, not only that the gate failed.
Optionally upserts a GitHub PR comment (GITHUB_TOKEN + pull_request event).
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .gate import DEFAULT_BASELINE, load_baseline
from .runner import DEFAULT_REPORT, REPO_ROOT

MARKER = "<!-- eval-scorecard -->"


def task_index(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["task_id"]: row for row in report.get("scores") or []}


def baseline_tasks(baseline: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not baseline:
        return {}
    return dict(baseline.get("tasks") or {})


def render_scorecard(report: dict[str, Any], baseline: dict[str, Any] | None) -> str:
    summary = report["summary"]
    current = task_index(report)
    previous = baseline_tasks(baseline)
    flips: list[str] = []
    lines = [
        MARKER,
        "## Eval scorecard",
        "",
        f"success **{summary['success_rate']:.1%}** "
        f"({summary['n_pass']}/{summary['n_tasks']}) · "
        f"**${summary['usd_per_task']:.6f}/task** · "
        f"tool recall {summary['tool_recall']:.2f} · "
        f"groundedness {summary['groundedness']:.2f}",
        "",
    ]
    if baseline:
        d_success = summary["success_rate"] - float(baseline.get("success_rate") or 0)
        d_usd = summary["usd_per_task"] - float(baseline.get("usd_per_task") or 0)
        lines.append(
            f"vs baseline: success {d_success:+.1%} · $/task {d_usd:+.6f}"
        )
        lines.append("")
    lines.extend(
        [
            "| Task | Was | Now | Decision | Δ $ | Tools |",
            "|---|:---:|:---:|---|---:|---|",
        ]
    )
    all_ids = sorted(set(current) | set(previous))
    for task_id in all_ids:
        now = current.get(task_id)
        was = previous.get(task_id)
        now_ok = "—" if now is None else ("yes" if now["success"] else "NO")
        was_ok = "—" if was is None else ("yes" if was.get("success") else "NO")
        if was is not None and now is not None and bool(was.get("success")) != bool(now["success"]):
            flips.append(task_id)
            now_ok = f"**{now_ok}**"
        decision = (now or {}).get("actual_decision") or (now or {}).get("decision") or "—"
        cost_now = float((now or {}).get("cost_usd") or 0)
        cost_was = float((was or {}).get("cost_usd") or 0) if was else cost_now
        delta = cost_now - cost_was
        tools = ",".join((now or {}).get("called_tools") or []) or "—"
        lines.append(
            f"| `{task_id}` | {was_ok} | {now_ok} | {decision} | {delta:+.6f} | `{tools}` |"
        )
    lines.append("")
    if flips:
        lines.append("**Flipped tasks:** " + ", ".join(f"`{t}`" for t in flips))
        lines.append("")
        lines.append("Waterfalls for failed tasks are uploaded as CI artifacts when this runs on GitHub Actions.")
    else:
        lines.append("No task flipped vs baseline.")
    lines.append("")
    return "\n".join(lines)


def tasks_payload(report: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for row in report.get("scores") or []:
        out[row["task_id"]] = {
            "success": row["success"],
            "cost_usd": row["cost_usd"],
            "decision": row.get("actual_decision"),
            "called_tools": row.get("called_tools") or [],
            "guardrail_ok": row.get("guardrail_ok"),
        }
    return out


def write_baseline(report: dict[str, Any], path: Path | None = None) -> Path:
    path = path or DEFAULT_BASELINE
    payload = dict(report["summary"])
    payload["tasks"] = tasks_payload(report)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _pr_number() -> int | None:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not Path(event_path).exists():
        return None
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    pr = event.get("pull_request") or {}
    if pr.get("number"):
        return int(pr["number"])
    return None


def upsert_pr_comment(body: str) -> str | None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    number = _pr_number()
    if not token or not repo or not number:
        return None
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "agent-trace-eval-scorecard",
    }

    def request(method: str, url: str, payload: dict | None = None) -> dict | list:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method, headers={**headers, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"github {method} {url} -> {exc.code} {detail}") from exc

    comments = request("GET", f"{api}/repos/{repo}/issues/{number}/comments?per_page=100")
    existing = next((c for c in comments if MARKER in (c.get("body") or "")), None)
    if existing:
        request("PATCH", existing["url"], {"body": body})
        return existing["url"]
    created = request("POST", f"{api}/repos/{repo}/issues/{number}/comments", {"body": body})
    return str(created.get("html_url") or created.get("url") or "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write per-task eval scorecard vs baseline")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "artifacts" / "scorecard.md")
    parser.add_argument("--comment", action="store_true", help="upsert a PR comment when running in Actions")
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args(argv)

    if not args.report.exists():
        print(f"no report at {args.report}; run python -m agent_trace_eval first", flush=True)
        return 1
    report = json.loads(args.report.read_text(encoding="utf-8"))
    baseline = load_baseline()
    if args.write_baseline:
        write_baseline(report)
    md = render_scorecard(report, baseline)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(md + "\n", encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(md + "\n")
    print(md)
    if args.comment:
        url = upsert_pr_comment(md)
        if url:
            print(f"posted comment {url}")
        else:
            print("skip comment (not a pull_request event or missing token)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
