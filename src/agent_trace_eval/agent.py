"""FastPay exception-triage agent. One ReAct loop, no framework."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.trace import SpanKind, get_current_span

from .cost import price_usd
from .guardrail import check_input, check_output, check_tool
from .llm import LLM, LLMResponse
from .telemetry import AGENT_NAME, AGENT_VERSION, genai_span, set_json_attr
from .tools import dumps, execute_tool, openai_tool_schemas, validate_args
from .world import World

MAX_TURNS = 6
SYSTEM_PROMPT = """You are FastPay's payments exception triage agent.
Use tools to inspect the ledger and playbooks before you mutate anything.
Never invent payment amounts, statuses, or failure codes.
When finished, return ONLY JSON:
{"decision":"retry_later|refund|hold|page|no_action|escalate|refuse",
 "summary":"...","payment_id":null,"facts":[],"refund_amount_cents":null,"incident_id":null}
"""


@dataclass
class AgentResult:
    final: dict[str, Any]
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    guardrail_blocked: bool = False
    schema_errors: list[str] = field(default_factory=list)
    conversation_id: str = ""
    trace_id: str = ""
    error: str | None = None
    model: str = ""


def run_agent(
    prompt: str,
    *,
    world: World,
    llm: LLM,
    conversation_id: str | None = None,
) -> AgentResult:
    started = time.perf_counter()
    conv = conversation_id or f"conv_{uuid.uuid4().hex[:12]}"
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    schema_errors: list[str] = []
    blocked = False
    inp = out = 0
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    tools = openai_tool_schemas()

    with genai_span(
        f"invoke_agent {AGENT_NAME}",
        operation="invoke_agent",
        kind=SpanKind.INTERNAL,
        attributes={
            "gen_ai.provider.name": llm.provider,
            "gen_ai.agent.name": AGENT_NAME,
            "gen_ai.agent.version": AGENT_VERSION,
            "gen_ai.conversation.id": conv,
            "gen_ai.request.model": llm.model,
            "gen_ai.agent.description": "FastPay payments exception triage",
        },
    ) as root:
        set_json_attr(root, "gen_ai.system_instructions", [{"type": "text", "content": SYSTEM_PROMPT}])
        inbound = check_input(prompt)
        if not inbound.allowed:
            blocked = True
            messages[-1]["content"] = "GUARDRAIL_INPUT_BLOCK: " + inbound.reason
        elif inbound.redacted_text:
            messages[-1]["content"] = inbound.redacted_text

        final_text = ""
        error = None
        for _ in range(MAX_TURNS):
            response: LLMResponse = llm.complete(messages, tools, conversation_id=conv)
            inp += response.input_tokens
            out += response.output_tokens
            if not response.tool_calls:
                final_text = response.text or ""
                break
            assistant_calls = []
            for call in response.tool_calls:
                assistant_calls.append(
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                    }
                )
                record = {"id": call.id, "name": call.name, "arguments": call.arguments, "blocked": False}
                errs = validate_args(call.name, call.arguments)
                if errs:
                    schema_errors.extend(errs)
                gate = check_tool(call.name, call.arguments, world)
                if not gate.allowed:
                    blocked = True
                    record["blocked"] = True
                    payload = {"error": "guardrail_blocked", "rule": gate.rule, "reason": gate.reason}
                else:
                    payload = execute_tool(call.name, call.arguments, world, call.id)
                record["result"] = payload
                tool_calls.append(record)
                tool_results.append(payload)
                messages.append({"role": "assistant", "content": None, "tool_calls": assistant_calls[-1:]})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": dumps(payload)})
        else:
            error = "max_turns"
            final_text = json.dumps(
                {
                    "decision": "refuse",
                    "summary": "Exceeded max tool turns.",
                    "payment_id": None,
                    "facts": [],
                    "refund_amount_cents": None,
                    "incident_id": None,
                }
            )

        outbound = check_output(final_text)
        if not outbound.allowed:
            blocked = True
            final_text = outbound.redacted_text or final_text
        parsed = _parse_final(final_text)
        root.set_attribute("gen_ai.usage.input_tokens", inp)
        root.set_attribute("gen_ai.usage.output_tokens", out)
        set_json_attr(root, "gen_ai.output.messages", [{"role": "assistant", "parts": [{"type": "text", "content": final_text[:4000]}]}])
        ctx = get_current_span().get_span_context()
        trace_id = format(ctx.trace_id, "032x") if ctx else ""

    cost = price_usd(llm.model, inp, out)
    return AgentResult(
        final=parsed,
        text=final_text,
        tool_calls=tool_calls,
        tool_results=tool_results,
        input_tokens=inp,
        output_tokens=out,
        cost_usd=cost,
        latency_ms=(time.perf_counter() - started) * 1000,
        guardrail_blocked=blocked,
        schema_errors=schema_errors,
        conversation_id=conv,
        trace_id=trace_id,
        error=error,
        model=llm.model,
    )


def _parse_final(text: str) -> dict[str, Any]:
    blob = text.strip()
    if blob.startswith("```"):
        blob = blob.strip("`")
        blob = blob.split("\n", 1)[-1]
    try:
        data = json.loads(blob)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        start, end = blob.find("{"), blob.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(blob[start : end + 1])
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
    return {
        "decision": "refuse",
        "summary": blob[:500],
        "payment_id": None,
        "facts": [],
        "refund_amount_cents": None,
        "incident_id": None,
    }
