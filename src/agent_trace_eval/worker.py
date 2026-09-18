"""Eval worker: pick a pending outbox job and run the suite. No sleep() in request path."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .outbox import DEFAULT_DB, complete, enqueue, fail, get_job, pickup
from .runner import run_suite


def run_one(*, db: Path | None = None) -> int:
    job = pickup(path=db)
    if job is None:
        print("outbox empty")
        return 0
    payload = job.payload or {}
    try:
        report = run_suite(
            enforce_gate=bool(payload.get("gate")),
            enforce_span_diff=bool(payload.get("span_diff")),
            slice=payload.get("slice"),
        )
    except Exception as exc:
        fail(job.id, str(exc), path=db)
        print(f"job {job.id} failed: {exc}", file=sys.stderr)
        return 1
    # Drop bulky span trees from the stored result.
    stored = {
        "summary": report["summary"],
        "scores": report["scores"],
        "n_spans": report.get("n_spans"),
        "gate_passed": report.get("gate_passed"),
    }
    complete(job.id, stored, path=db)
    print(f"job {job.id} succeeded  success={report['summary']['success_rate']:.1%}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enqueue or run FastPay eval jobs")
    parser.add_argument("command", choices=["enqueue", "once", "status"], nargs="?", default="once")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--slice", choices=["triage", "guardrail"], default=None)
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--span-diff", action="store_true")
    parser.add_argument("--job-id", type=int, default=None)
    args = parser.parse_args(argv)

    if args.command == "enqueue":
        job_id = enqueue(
            {"slice": args.slice, "gate": args.gate, "span_diff": args.span_diff},
            path=args.db,
        )
        print(job_id)
        return 0
    if args.command == "status":
        if args.job_id is None:
            print("--job-id is required for status", file=sys.stderr)
            return 2
        job = get_job(args.job_id, path=args.db)
        if job is None:
            print("not found", file=sys.stderr)
            return 1
        print(json.dumps({"id": job.id, "status": job.status, "attempts": job.attempts, "error": job.error}))
        return 0
    return run_one(db=args.db)


if __name__ == "__main__":
    raise SystemExit(main())
