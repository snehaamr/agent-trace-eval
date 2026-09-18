from pathlib import Path

from agent_trace_eval.outbox import complete, enqueue, get_job, pickup
from agent_trace_eval.worker import run_one


def test_enqueue_then_worker(tmp_path, monkeypatch):
    db = tmp_path / "jobs.sqlite"
    job_id = enqueue({"slice": None}, path=db)
    assert get_job(job_id, path=db).status == "pending"

    def fake_suite(**kwargs):
        return {
            "summary": {"success_rate": 1.0, "n_pass": 1, "n_tasks": 1},
            "scores": [{"task_id": "nsf-retry", "success": True}],
            "n_spans": 3,
        }

    monkeypatch.setattr("agent_trace_eval.worker.run_suite", fake_suite)
    assert run_one(db=db) == 0
    job = get_job(job_id, path=db)
    assert job.status == "succeeded"
    assert job.result["summary"]["success_rate"] == 1.0
    assert pickup(path=db) is None


def test_pickup_is_exclusive(tmp_path):
    db = tmp_path / "jobs.sqlite"
    enqueue({}, path=db)
    first = pickup(path=db)
    second = pickup(path=db)
    assert first is not None
    assert second is None
    complete(first.id, {"summary": {}}, path=db)
    assert get_job(first.id, path=db).status == "succeeded"
