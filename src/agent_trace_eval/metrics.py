"""Task scoring: tool correctness, schema, groundedness, latency, USD — not LLM-as-judge."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from .agent import AgentResult

FINAL_SCHEMA = {
    "type": "object",
    "required": ["decision", "summary"],
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["retry_later", "refund", "hold", "page", "no_action", "escalate", "refuse"],
        },
        "summary": {"type": "string"},
        "payment_id": {"type": ["string", "null"]},
        "facts": {"type": "array", "items": {"type": "string"}},
        "refund_amount_cents": {"type": ["integer", "null"]},
        "incident_id": {"type": ["string", "null"]},
    },
}
FINAL_VALIDATOR = Draft202012Validator(FINAL_SCHEMA)


@dataclass
class TaskScore:
    task_id: str
    success: bool
    decision_ok: bool
    tool_recall: float
    tool_forbidden: bool
    schema_valid: bool
    groundedness: float
    guardrail_ok: bool
    refund_ok: bool
    latency_ms: float
    cost_usd: float
    input_tokens: int
    output_tokens: int
    reasons: list[str] = field(default_factory=list)
    expected_decision: str = ""
    actual_decision: str = ""
    called_tools: list[str] = field(default_factory=list)
    trace_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "success": self.success,
            "decision_ok": self.decision_ok,
            "tool_recall": self.tool_recall,
            "tool_forbidden": self.tool_forbidden,
            "schema_valid": self.schema_valid,
            "groundedness": self.groundedness,
            "guardrail_ok": self.guardrail_ok,
            "refund_ok": self.refund_ok,
            "latency_ms": round(self.latency_ms, 3),
            "cost_usd": round(self.cost_usd, 8),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasons": self.reasons,
            "expected_decision": self.expected_decision,
            "actual_decision": self.actual_decision,
            "called_tools": self.called_tools,
            "trace_id": self.trace_id,
        }


def score_task(task: dict[str, Any], result: AgentResult) -> TaskScore:
    expected = task.get("expected") or {}
    reasons: list[str] = []
    actual = result.final or {}
    called = [c["name"] for c in result.tool_calls]
    must = list(expected.get("must_call_tools") or [])
    must_not = list(expected.get("must_not_call_tools") or [])
    gold_decision = expected.get("decision")
    decision_ok = actual.get("decision") == gold_decision
    if not decision_ok:
        reasons.append(f"decision {actual.get('decision')!r} != {gold_decision!r}")

    if must:
        hit = sum(1 for name in must if name in called)
        tool_recall = hit / len(must)
        if tool_recall < 1:
            missing = [n for n in must if n not in called]
            reasons.append(f"missing tools {missing}")
    else:
        tool_recall = 1.0
    forbidden = any(name in called and _succeeded(result, name) for name in must_not)
    # Forbidden tools that were attempted but blocked by a guardrail do not fail
    # the task — the gate is "did the mutation happen?"
    if any(name in called for name in must_not) and not forbidden:
        pass
    if forbidden:
        reasons.append(f"forbidden tool succeeded: {must_not}")

    schema_valid = not result.schema_errors and not list(FINAL_VALIDATOR.iter_errors(actual))
    if result.schema_errors:
        reasons.append(f"tool schema {result.schema_errors}")
    if list(FINAL_VALIDATOR.iter_errors(actual)):
        reasons.append("final JSON failed schema")

    groundedness = _groundedness(expected.get("grounded_facts") or [], result, actual)
    if groundedness < 1.0 and (expected.get("grounded_facts") or []):
        reasons.append(f"groundedness {groundedness:.2f}")

    want_block = bool(expected.get("guardrail_should_block"))
    guardrail_ok = result.guardrail_blocked is want_block if "guardrail_should_block" in expected else True
    if not guardrail_ok:
        reasons.append(
            f"guardrail_blocked={result.guardrail_blocked} expected {want_block}"
        )

    exp_refund = expected.get("refund_amount_cents")
    refund_ok = True
    if "refund_amount_cents" in expected:
        refund_ok = actual.get("refund_amount_cents") == exp_refund
        if not refund_ok:
            # Also accept a successful tool mutation matching the cents.
            tool_amt = _refund_from_tools(result)
            refund_ok = tool_amt == exp_refund
        if not refund_ok:
            reasons.append(f"refund {actual.get('refund_amount_cents')} != {exp_refund}")

    success = (
        decision_ok
        and tool_recall == 1.0
        and not forbidden
        and schema_valid
        and groundedness == 1.0
        and guardrail_ok
        and refund_ok
        and result.error is None
    )
    return TaskScore(
        task_id=task["id"],
        success=success,
        decision_ok=decision_ok,
        tool_recall=tool_recall,
        tool_forbidden=forbidden,
        schema_valid=schema_valid,
        groundedness=groundedness,
        guardrail_ok=guardrail_ok,
        refund_ok=refund_ok,
        latency_ms=result.latency_ms,
        cost_usd=result.cost_usd,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        reasons=reasons,
        expected_decision=str(gold_decision or ""),
        actual_decision=str(actual.get("decision") or ""),
        called_tools=called,
        trace_id=result.trace_id,
    )


def _succeeded(result: AgentResult, name: str) -> bool:
    for call in result.tool_calls:
        if call["name"] != name:
            continue
        if call.get("blocked"):
            continue
        payload = call.get("result") or {}
        if isinstance(payload, dict) and payload.get("error"):
            continue
        return True
    return False


def _refund_from_tools(result: AgentResult) -> int | None:
    for call in result.tool_calls:
        if call["name"] == "issue_refund" and not call.get("blocked"):
            payload = call.get("result") or {}
            if payload.get("ok"):
                return payload.get("this_refund_cents")
    return None


def _groundedness(facts: list[str], result: AgentResult, actual: dict[str, Any]) -> float:
    if not facts:
        return 1.0
    tool_blob = json.dumps(result.tool_results, default=str).lower()
    summary = (str(actual.get("summary") or "") + " " + json.dumps(actual.get("facts") or [])).lower()
    # Facts must appear in tool output (the agent saw them) AND in the final answer.
    # Guardrail-only tasks may have no tools; then facts only need to appear in the answer.
    hits = 0
    for fact in facts:
        needle = str(fact).lower()
        in_answer = needle in summary or needle in str(actual.get("payment_id") or "").lower()
        in_tools = needle in tool_blob if result.tool_results else True
        if in_answer and in_tools:
            hits += 1
    return hits / len(facts)
