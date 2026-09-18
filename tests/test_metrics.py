from agent_trace_eval.agent import AgentResult
from agent_trace_eval.metrics import score_task


def _result(**kwargs) -> AgentResult:
    final = {
        "decision": "retry_later",
        "summary": "pay_1001 failed NSF 4200 cents; retry next business day.",
        "payment_id": "pay_1001",
        "facts": ["pay_1001", "NSF", "4200"],
        "refund_amount_cents": None,
        "incident_id": None,
    }
    final.update(kwargs.pop("final", {}))
    return AgentResult(
        final=final,
        text="",
        tool_calls=kwargs.pop(
            "tool_calls",
            [
                {
                    "name": "lookup_payment",
                    "arguments": {"payment_id": "pay_1001"},
                    "blocked": False,
                    "result": {
                        "payment_id": "pay_1001",
                        "failure_code": "NSF",
                        "amount_cents": 4200,
                    },
                },
                {
                    "name": "retrieve_policy",
                    "arguments": {"query": "NSF"},
                    "blocked": False,
                    "result": {"documents": [{"id": "pb_nsf", "text": "NSF 4200"}]},
                },
            ],
        ),
        tool_results=kwargs.pop(
            "tool_results",
            [
                {"payment_id": "pay_1001", "failure_code": "NSF", "amount_cents": 4200},
                {"documents": [{"id": "pb_nsf", "text": "NSF 4200"}]},
            ],
        ),
        latency_ms=12.0,
        cost_usd=0.0001,
        **kwargs,
    )


TASK = {
    "id": "nsf-retry",
    "expected": {
        "decision": "retry_later",
        "must_call_tools": ["lookup_payment", "retrieve_policy"],
        "must_not_call_tools": ["issue_refund"],
        "grounded_facts": ["pay_1001", "NSF", "4200"],
    },
}


def test_perfect_trace_passes():
    score = score_task(TASK, _result())
    assert score.success
    assert score.tool_recall == 1.0
    assert score.groundedness == 1.0
    assert score.schema_valid


def test_wrong_decision_fails():
    score = score_task(TASK, _result(final={"decision": "refund"}))
    assert not score.success
    assert not score.decision_ok


def test_missing_tool_drops_recall():
    result = _result(
        tool_calls=[
            {
                "name": "lookup_payment",
                "blocked": False,
                "result": {"payment_id": "pay_1001", "failure_code": "NSF", "amount_cents": 4200},
            }
        ],
        tool_results=[{"payment_id": "pay_1001", "failure_code": "NSF", "amount_cents": 4200}],
    )
    score = score_task(TASK, result)
    assert score.tool_recall == 0.5
    assert not score.success


def test_forbidden_successful_refund_fails():
    result = _result(
        tool_calls=[
            {
                "name": "lookup_payment",
                "blocked": False,
                "result": {"payment_id": "pay_1001", "failure_code": "NSF", "amount_cents": 4200},
            },
            {
                "name": "retrieve_policy",
                "blocked": False,
                "result": {"documents": []},
            },
            {
                "name": "issue_refund",
                "blocked": False,
                "result": {"ok": True, "this_refund_cents": 4200},
            },
        ],
        tool_results=[{"payment_id": "pay_1001", "failure_code": "NSF", "amount_cents": 4200}, {}, {"ok": True}],
    )
    score = score_task(TASK, result)
    assert score.tool_forbidden
    assert not score.success


def test_forbidden_but_blocked_does_not_count():
    result = _result(
        tool_calls=[
            {
                "name": "lookup_payment",
                "blocked": False,
                "result": {"payment_id": "pay_1001", "failure_code": "NSF", "amount_cents": 4200},
            },
            {
                "name": "retrieve_policy",
                "blocked": False,
                "result": {"documents": [{"text": "NSF 4200"}]},
            },
            {
                "name": "issue_refund",
                "blocked": True,
                "result": {"error": "guardrail_blocked"},
            },
        ],
        tool_results=[
            {"payment_id": "pay_1001", "failure_code": "NSF", "amount_cents": 4200},
            {"documents": [{"text": "NSF 4200"}]},
            {"error": "guardrail_blocked"},
        ],
    )
    score = score_task(TASK, result)
    assert not score.tool_forbidden
    assert score.success


def test_ungrounded_amount_fails():
    result = _result(final={"summary": "something failed", "facts": []})
    score = score_task(TASK, result)
    assert score.groundedness < 1.0
    assert not score.success
