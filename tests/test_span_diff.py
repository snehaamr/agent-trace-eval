from agent_trace_eval.span_diff import diff_shapes, flatten_ops, shapes_from_spans, write_goldens


def _span(name, op, span_id, parent=None, **attrs):
    attributes = {"gen_ai.operation.name": op, **attrs}
    return {
        "name": name,
        "trace_id": "abc",
        "span_id": span_id,
        "parent_span_id": parent,
        "start_time_unix_nano": int(span_id, 16),
        "attributes": attributes,
    }


def test_tree_and_flatten():
    spans = [
        _span("eval.task nsf-retry", "invoke_workflow", "01"),
        _span("invoke_agent fastpay-exception-triage", "invoke_agent", "02", "01"),
        _span("apply_guardrail input_policy", "apply_guardrail", "03", "02", **{"guardrail.decision": "allow"}),
        _span("chat heuristic", "chat", "04", "02"),
        _span(
            "execute_tool lookup_payment",
            "execute_tool",
            "05",
            "02",
            **{"gen_ai.tool.name": "lookup_payment"},
        ),
    ]
    shapes = shapes_from_spans(spans)
    assert "nsf-retry" in shapes
    ops = flatten_ops(shapes["nsf-retry"]["tree"])
    assert ops[0] == "invoke_workflow"
    assert "execute_tool:lookup_payment" in ops
    assert "chat" in ops


def test_diff_detects_missing_tool(tmp_path):
    good = shapes_from_spans(
        [
            _span("eval.task demo", "invoke_workflow", "01"),
            _span("execute_tool lookup_payment", "execute_tool", "02", "01", **{"gen_ai.tool.name": "lookup_payment"}),
        ]
    )
    bad = shapes_from_spans(
        [
            _span("eval.task demo", "invoke_workflow", "01"),
            _span("execute_tool issue_refund", "execute_tool", "02", "01", **{"gen_ai.tool.name": "issue_refund"}),
        ]
    )
    write_goldens(good, tmp_path)
    problems = diff_shapes(bad, good)
    assert problems
    assert any("demo" in p for p in problems)


def test_identical_trees_pass():
    spans = [
        _span("eval.task demo", "invoke_workflow", "01"),
        _span("chat heuristic", "chat", "02", "01"),
    ]
    shapes = shapes_from_spans(spans)
    assert diff_shapes(shapes, shapes) == []
