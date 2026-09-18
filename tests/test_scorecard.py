from agent_trace_eval.scorecard import render_scorecard, tasks_payload


def _report():
    return {
        "summary": {
            "n_tasks": 2,
            "n_pass": 1,
            "success_rate": 0.5,
            "usd_per_task": 0.0004,
            "tool_recall": 1.0,
            "groundedness": 1.0,
        },
        "scores": [
            {
                "task_id": "nsf-retry",
                "success": True,
                "actual_decision": "retry_later",
                "cost_usd": 0.0002,
                "called_tools": ["lookup_payment"],
                "guardrail_ok": True,
            },
            {
                "task_id": "refund-failed-nsf",
                "success": False,
                "actual_decision": "refund",
                "cost_usd": 0.0006,
                "called_tools": ["lookup_payment", "issue_refund"],
                "guardrail_ok": False,
            },
        ],
    }


def test_scorecard_flags_flips():
    baseline = {
        "success_rate": 1.0,
        "usd_per_task": 0.0002,
        "tasks": {
            "nsf-retry": {"success": True, "cost_usd": 0.0002, "decision": "retry_later"},
            "refund-failed-nsf": {"success": True, "cost_usd": 0.0003, "decision": "refuse"},
        },
    }
    md = render_scorecard(_report(), baseline)
    assert "Flipped tasks" in md
    assert "`refund-failed-nsf`" in md
    assert "<!-- eval-scorecard -->" in md
    assert "vs baseline" in md


def test_scorecard_no_flips():
    report = _report()
    report["scores"][1]["success"] = True
    report["summary"]["success_rate"] = 1.0
    report["summary"]["n_pass"] = 2
    baseline = {
        "success_rate": 1.0,
        "usd_per_task": 0.0004,
        "tasks": tasks_payload(report),
    }
    md = render_scorecard(report, baseline)
    assert "No task flipped" in md
