"""Input, tool, and output guardrails.

These are policy, not the LLM: they emit apply_guardrail spans and can block
a turn before a tool mutates the fake ledger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from opentelemetry.trace import SpanKind, Status, StatusCode

from .telemetry import genai_span
from .world import World

# 13–19 digits with optional spaces/dashes (very small demo PAN detector).
PAN_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
EXFIL_RE = re.compile(
    r"\b(dump|export|exfiltrate)\b.*\b(card|pan|ssn|account numbers?)\b"
    r"|\b(full card numbers?|all card numbers?)\b",
    re.I,
)
WIRE_RE = re.compile(r"\b(bitcoin|wire to|off.?platform|crypto wallet)\b", re.I)

REFUND_MANAGER_LIMIT_CENTS = 50_000  # $500


@dataclass
class GuardrailResult:
    allowed: bool
    rule: str
    decision: str  # allow | redact | block
    reason: str
    redacted_text: str | None = None


def _span(name: str, result: GuardrailResult, extra: dict | None = None) -> None:
    attrs = {
        "gen_ai.provider.name": "agent_trace_eval",
        "guardrail.name": name,
        "guardrail.decision": result.decision,
        "guardrail.rule": result.rule,
        "guardrail.reason": result.reason,
    }
    if extra:
        attrs.update(extra)
    with genai_span(
        f"apply_guardrail {name}",
        operation="apply_guardrail",
        kind=SpanKind.INTERNAL,
        attributes=attrs,
    ) as span:
        if not result.allowed:
            span.set_status(Status(StatusCode.ERROR, result.reason))


def check_input(text: str) -> GuardrailResult:
    if EXFIL_RE.search(text) or WIRE_RE.search(text):
        result = GuardrailResult(
            allowed=False,
            rule="data_exfil_or_off_platform",
            decision="block",
            reason="Refusing off-platform movement or bulk card export.",
            redacted_text=text,
        )
        _span("input_policy", result)
        return result
    redacted = PAN_RE.sub("[REDACTED_PAN]", SSN_RE.sub("[REDACTED_SSN]", text))
    if redacted != text:
        result = GuardrailResult(
            allowed=True,
            rule="pii_redact",
            decision="redact",
            reason="Redacted PAN/SSN from the user turn.",
            redacted_text=redacted,
        )
        _span("input_policy", result)
        return result
    result = GuardrailResult(
        allowed=True, rule="input_ok", decision="allow", reason="ok", redacted_text=text
    )
    _span("input_policy", result)
    return result


def check_tool(name: str, args: dict, world: World) -> GuardrailResult:
    extra = {"gen_ai.tool.name": name}
    if name == "issue_refund":
        amount = int(args.get("amount_cents") or 0)
        pid = args.get("payment_id")
        payment = world.payments.get(pid) if isinstance(pid, str) else None
        if amount > REFUND_MANAGER_LIMIT_CENTS:
            result = GuardrailResult(
                allowed=False,
                rule="refund_manager_limit",
                decision="block",
                reason=f"Refund {amount} cents exceeds ${REFUND_MANAGER_LIMIT_CENTS // 100} manager limit.",
            )
            _span("tool_policy", result, extra)
            return result
        if payment is None:
            result = GuardrailResult(
                allowed=False,
                rule="refund_unknown_payment",
                decision="block",
                reason="Refund on unknown payment_id.",
            )
            _span("tool_policy", result, extra)
            return result
        remaining = payment["amount_cents"] - payment["refunded_cents"]
        if amount > remaining:
            result = GuardrailResult(
                allowed=False,
                rule="refund_over_captured",
                decision="block",
                reason=f"Refund {amount} cents exceeds remaining captured {remaining}.",
            )
            _span("tool_policy", result, extra)
            return result
        if payment["status"] not in {"settled"}:
            result = GuardrailResult(
                allowed=False,
                rule="refund_not_captured",
                decision="block",
                reason=f"Cannot refund a {payment['status']} payment.",
            )
            _span("tool_policy", result, extra)
            return result
    if name == "page_oncall":
        summary = str(args.get("summary") or "")
        if PAN_RE.search(summary) or SSN_RE.search(summary) or "[REDACTED_" in summary:
            result = GuardrailResult(
                allowed=False,
                rule="pager_pii",
                decision="block",
                reason="Pager text contains PAN/SSN (or a redaction placeholder from the user).",
            )
            _span("tool_policy", result, extra)
            return result
    result = GuardrailResult(allowed=True, rule="tool_ok", decision="allow", reason="ok")
    _span("tool_policy", result, extra)
    return result


def check_output(text: str) -> GuardrailResult:
    if PAN_RE.search(text) or SSN_RE.search(text):
        result = GuardrailResult(
            allowed=False,
            rule="output_pii",
            decision="block",
            reason="Model tried to emit PAN/SSN.",
            redacted_text=PAN_RE.sub("[REDACTED_PAN]", SSN_RE.sub("[REDACTED_SSN]", text)),
        )
        _span("output_policy", result)
        return result
    result = GuardrailResult(
        allowed=True, rule="output_ok", decision="allow", reason="ok", redacted_text=text
    )
    _span("output_policy", result)
    return result
