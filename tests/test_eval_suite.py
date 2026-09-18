from pathlib import Path

from agent_trace_eval.gate import check_gate, load_baseline, load_thresholds
from agent_trace_eval.retrieval import retrieve_playbooks
from agent_trace_eval.runner import run_suite
from agent_trace_eval.world import World


def test_agent_module_stays_small():
    path = Path("src/agent_trace_eval/agent.py")
    code_lines = [
        ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.strip().startswith("#")
    ]
    assert len(code_lines) <= 220, f"agent.py grew to {len(code_lines)} non-empty lines"


def test_nsf_playbook_ranks_first():
    docs = retrieve_playbooks("NSF ACH failed retry", World.seed().playbooks, top_k=3)
    assert docs
    assert docs[0]["id"] == "pb_nsf"


def test_r10_playbook_ranks_first():
    docs = retrieve_playbooks("ACH_RETURN R10 unauthorized", World.seed().playbooks, top_k=3)
    assert docs[0]["id"] == "pb_ach_r10"


def test_eval_suite_meets_ci_gate():
    report = run_suite(enforce_gate=False)
    summary = report["summary"]
    failed = [s for s in report["scores"] if not s["success"]]
    gate = check_gate(summary, load_thresholds(), load_baseline())
    assert gate.passed, (
        "gate failed:\n"
        + "\n".join(f.message for f in gate.failures)
        + "\nfailed tasks:\n"
        + "\n".join(f"{s['task_id']}: {s['reasons']} tools={s['called_tools']}" for s in failed)
    )
    assert summary["n_tasks"] >= 20
    assert summary["schema_validity"] == 1.0


def test_span_shapes_match_goldens():
    report = run_suite(enforce_span_diff=True, enforce_gate=False)
    assert report.get("span_diff_passed") is True
    from agent_trace_eval.span_diff import load_goldens

    assert len(load_goldens()) >= 20
