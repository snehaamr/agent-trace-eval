from agent_trace_eval.metrics import TaskScore, guardrail_stats


def _score(**kwargs) -> TaskScore:
    defaults = dict(
        task_id="t",
        success=True,
        decision_ok=True,
        tool_recall=1.0,
        tool_forbidden=False,
        schema_valid=True,
        groundedness=1.0,
        guardrail_ok=True,
        refund_ok=True,
        latency_ms=1.0,
        cost_usd=0.0,
        input_tokens=1,
        output_tokens=1,
    )
    defaults.update(kwargs)
    return TaskScore(**defaults)


def test_guardrail_recall_catches_missed_block():
    scores = [
        _score(task_id="a", expected_block=True, guardrail_blocked=True, slice="guardrail"),
        _score(task_id="b", expected_block=True, guardrail_blocked=False, slice="guardrail"),
    ]
    stats = guardrail_stats(scores)
    assert stats["guardrail_fn"] == 1
    assert stats["guardrail_recall"] == 0.5


def test_guardrail_precision_catches_false_positive():
    scores = [
        _score(task_id="ok", expected_block=False, guardrail_blocked=False, slice="triage"),
        _score(task_id="fp", expected_block=False, guardrail_blocked=True, slice="triage"),
        _score(task_id="tp", expected_block=True, guardrail_blocked=True, slice="guardrail"),
    ]
    stats = guardrail_stats(scores)
    assert stats["guardrail_fp"] == 1
    assert stats["guardrail_precision"] == 0.5
