"""Deterministic FastPay tools: fake ledger + fake pager + policy retrieval."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from jsonschema import Draft202012Validator
from opentelemetry.trace import SpanKind, Status, StatusCode

from .retrieval import traced_retrieve
from .telemetry import genai_span, set_json_attr
from .world import World

Json = dict[str, Any]


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


LOOKUP_PAYMENT = {
    "name": "lookup_payment",
    "description": "Fetch a single FastPay payment by payment_id.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["payment_id"],
        "properties": {"payment_id": {"type": "string"}},
    },
}
SEARCH_PAYMENTS = {
    "name": "search_payments",
    "description": "Search payments by merchant, customer, status, or failure_code.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "merchant_id": {"type": "string"},
            "customer_id": {"type": "string"},
            "status": {"type": "string"},
            "failure_code": {"type": "string"},
        },
    },
}
GET_LEDGER = {
    "name": "get_ledger_entries",
    "description": "Return ledger lines for a payment (auth, capture, mismatch, refund).",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["payment_id"],
        "properties": {"payment_id": {"type": "string"}},
    },
}
LIST_DUPES = {
    "name": "list_duplicates",
    "description": "List captured payments that share a charge fingerprint.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["fingerprint"],
        "properties": {"fingerprint": {"type": "string"}},
    },
}
ISSUE_REFUND = {
    "name": "issue_refund",
    "description": "Refund a settled payment. amount_cents must be within captured remaining.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["payment_id", "amount_cents", "reason"],
        "properties": {
            "payment_id": {"type": "string"},
            "amount_cents": {"type": "integer", "minimum": 1},
            "reason": {"type": "string"},
        },
    },
}
HOLD_PAYMENT = {
    "name": "hold_payment",
    "description": "Place or confirm a compliance hold on a payment.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["payment_id", "reason"],
        "properties": {
            "payment_id": {"type": "string"},
            "reason": {"type": "string"},
        },
    },
}
PAGE_ONCALL = {
    "name": "page_oncall",
    "description": "Open a pager incident. severity is sev-1 (critical) or sev-2 (high).",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["severity", "summary"],
        "properties": {
            "severity": {"type": "string", "enum": ["sev-1", "sev-2"]},
            "summary": {"type": "string"},
            "payment_id": {"type": "string"},
        },
    },
}
ACK_INCIDENT = {
    "name": "ack_incident",
    "description": "Acknowledge an open pager incident.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["incident_id"],
        "properties": {"incident_id": {"type": "string"}},
    },
}
RETRIEVE_POLICY = {
    "name": "retrieve_policy",
    "description": "Retrieve FastPay exception playbooks for a query (NSF, duplicate, KYC, ...).",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["query"],
        "properties": {"query": {"type": "string"}},
    },
}
GET_MERCHANT = {
    "name": "get_merchant",
    "description": "Fetch merchant risk profile.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["merchant_id"],
        "properties": {"merchant_id": {"type": "string"}},
    },
}

TOOL_SPECS: list[dict[str, Any]] = [
    LOOKUP_PAYMENT,
    SEARCH_PAYMENTS,
    GET_LEDGER,
    LIST_DUPES,
    ISSUE_REFUND,
    HOLD_PAYMENT,
    PAGE_ONCALL,
    ACK_INCIDENT,
    RETRIEVE_POLICY,
    GET_MERCHANT,
]
TOOL_BY_NAME = {t["name"]: t for t in TOOL_SPECS}
VALIDATORS = {
    t["name"]: Draft202012Validator(t["parameters"]) for t in TOOL_SPECS
}


def openai_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        }
        for spec in TOOL_SPECS
    ]


def validate_args(name: str, args: dict[str, Any]) -> list[str]:
    spec = TOOL_BY_NAME.get(name)
    if spec is None:
        return [f"unknown tool: {name}"]
    return [e.message for e in VALIDATORS[name].iter_errors(args)]


def _lookup_payment(world: World, payment_id: str) -> Json:
    p = world.payments.get(payment_id)
    if not p:
        return {"error": "not_found", "payment_id": payment_id}
    return dict(p)


def _search_payments(world: World, **filters: Any) -> Json:
    hits = []
    for p in world.payments.values():
        ok = True
        for key, value in filters.items():
            if value in (None, "") or key not in p:
                continue
            if p.get(key) != value:
                ok = False
                break
        if ok and any(filters.values()):
            hits.append(dict(p))
    hits.sort(key=lambda row: row["created_at"])
    return {"count": len(hits), "payments": hits}


def _get_ledger(world: World, payment_id: str) -> Json:
    if payment_id not in world.payments:
        return {"error": "not_found", "payment_id": payment_id}
    return {"payment_id": payment_id, "entries": list(world.ledger.get(payment_id, []))}


def _list_duplicates(world: World, fingerprint: str) -> Json:
    hits = [dict(p) for p in world.payments.values() if p.get("fingerprint") == fingerprint]
    hits.sort(key=lambda row: row["created_at"])
    return {"fingerprint": fingerprint, "count": len(hits), "payments": hits}


def _issue_refund(world: World, payment_id: str, amount_cents: int, reason: str) -> Json:
    p = world.payments.get(payment_id)
    if not p:
        return {"error": "not_found", "payment_id": payment_id}
    p["refunded_cents"] += int(amount_cents)
    world.ledger.setdefault(payment_id, []).append(
        {
            "entry_id": f"le_{payment_id}_refund_{p['refunded_cents']}",
            "payment_id": payment_id,
            "type": "refund",
            "amount_cents": int(amount_cents),
            "reason": reason,
            "at": _iso_now(),
        }
    )
    world.audit.append({"op": "refund", "payment_id": payment_id, "amount_cents": amount_cents})
    return {
        "ok": True,
        "payment_id": payment_id,
        "refunded_cents": p["refunded_cents"],
        "this_refund_cents": int(amount_cents),
        "reason": reason,
    }


def _hold_payment(world: World, payment_id: str, reason: str) -> Json:
    p = world.payments.get(payment_id)
    if not p:
        return {"error": "not_found", "payment_id": payment_id}
    p["status"] = "held"
    p["failure_code"] = p.get("failure_code") or "KYC_HOLD"
    world.audit.append({"op": "hold", "payment_id": payment_id, "reason": reason})
    return {"ok": True, "payment_id": payment_id, "status": "held", "reason": reason}


def _page_oncall(world: World, severity: str, summary: str, payment_id: str | None = None) -> Json:
    iid = world.next_incident_id()
    incident = {
        "incident_id": iid,
        "payment_id": payment_id,
        "severity": severity,
        "summary": summary,
        "status": "open",
    }
    world.incidents[iid] = incident
    world.audit.append({"op": "page", **incident})
    return {"ok": True, **incident}


def _ack_incident(world: World, incident_id: str) -> Json:
    inc = world.incidents.get(incident_id)
    if not inc:
        return {"error": "not_found", "incident_id": incident_id}
    inc["status"] = "acked"
    world.audit.append({"op": "ack", "incident_id": incident_id})
    return {"ok": True, **inc}


def _retrieve_policy(world: World, query: str) -> Json:
    docs = traced_retrieve(query, world.playbooks, top_k=3)
    return {"query": query, "documents": docs}


def _get_merchant(world: World, merchant_id: str) -> Json:
    m = world.merchants.get(merchant_id)
    if not m:
        return {"error": "not_found", "merchant_id": merchant_id}
    return dict(m)


HANDLERS: dict[str, Callable[..., Json]] = {
    "lookup_payment": _lookup_payment,
    "search_payments": _search_payments,
    "get_ledger_entries": _get_ledger,
    "list_duplicates": _list_duplicates,
    "issue_refund": _issue_refund,
    "hold_payment": _hold_payment,
    "page_oncall": _page_oncall,
    "ack_incident": _ack_incident,
    "retrieve_policy": _retrieve_policy,
    "get_merchant": _get_merchant,
}


def execute_tool(name: str, args: dict[str, Any], world: World, call_id: str) -> Json:
    with genai_span(
        f"execute_tool {name}",
        operation="execute_tool",
        kind=SpanKind.INTERNAL,
        attributes={
            "gen_ai.provider.name": "agent_trace_eval",
            "gen_ai.tool.name": name,
            "gen_ai.tool.type": "function",
            "gen_ai.tool.call.id": call_id,
        },
    ) as span:
        set_json_attr(span, "gen_ai.tool.call.arguments", args)
        errors = validate_args(name, args)
        if errors:
            payload = {"error": "schema_invalid", "messages": errors}
            set_json_attr(span, "gen_ai.tool.call.result", payload)
            span.set_status(Status(StatusCode.ERROR, "schema_invalid"))
            return payload
        handler = HANDLERS[name]
        payload = handler(world, **args)
        set_json_attr(span, "gen_ai.tool.call.result", payload)
        if isinstance(payload, dict) and payload.get("error"):
            span.set_status(Status(StatusCode.ERROR, str(payload["error"])))
        return payload


def dumps(payload: Json) -> str:
    return json.dumps(payload, default=str)
