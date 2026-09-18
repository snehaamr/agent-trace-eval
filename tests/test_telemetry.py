from agent_trace_eval.agent import run_agent
from agent_trace_eval.llm import HeuristicLLM
from agent_trace_eval.telemetry import setup_tracer
from agent_trace_eval.world import World


def test_settlement_task_emits_genai_span_types(tmp_path):
    sink: list[dict] = []
    setup_tracer(jsonl_path=str(tmp_path / "spans.jsonl"), sink=sink)
    before = len(sink)
    run_agent(
        "pay_1007 failed settlement. Inspect the ledger and page if the playbook says so.",
        world=World.seed(),
        llm=HeuristicLLM(),
        conversation_id="otel-settlement",
    )
    spans = sink[before:]
    names = {s["name"] for s in spans}
    ops = {s["attributes"].get("gen_ai.operation.name") for s in spans}

    assert any(n.startswith("invoke_agent ") for n in names)
    assert any(n.startswith("chat ") for n in names)
    assert "execute_tool lookup_payment" in names
    assert "execute_tool retrieve_policy" in names
    assert "execute_tool get_ledger_entries" in names
    assert "execute_tool page_oncall" in names
    assert "retrieval fastpay-playbooks" in names
    assert "apply_guardrail input_policy" in names
    assert "apply_guardrail tool_policy" in names
    assert "apply_guardrail output_policy" in names
    assert {"invoke_agent", "chat", "execute_tool", "retrieval", "apply_guardrail"} <= ops

    lookup = next(s for s in spans if s["name"] == "execute_tool lookup_payment")
    assert lookup["attributes"]["gen_ai.tool.name"] == "lookup_payment"
    chat = next(s for s in spans if s["name"].startswith("chat "))
    assert int(chat["attributes"]["gen_ai.usage.input_tokens"]) > 0
