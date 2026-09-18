"""Deterministic FastPay planner used as the CI stand-in for an LLM.

It does not read gold labels. It looks at the user prompt plus tool JSON
the agent already collected, then either emits a tool call or a final JSON
answer — the same contract a chat model uses.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

PAY_RE = re.compile(r"\bpay_\d+\w*\b")
MCH_RE = re.compile(r"\bmch_[a-z0-9]+\b")
CUS_RE = re.compile(r"\bcus_[a-z0-9]+\b")
INC_RE = re.compile(r"\binc_\d+\b")
MONEY_RE = re.compile(r"\$([0-9]+(?:\.[0-9]{1,2})?)")
CENTS_RE = re.compile(r"\b([0-9]+) cents\b", re.I)

DECISIONS = (
    "retry_later",
    "refund",
    "hold",
    "page",
    "no_action",
    "escalate",
    "refuse",
)


def plan_turn(messages: list[dict[str, Any]]) -> dict[str, Any]:
    user = _user_text(messages)
    called = _called_tools(messages)
    results = _tool_payloads(messages)
    payments = _payments_from_results(results)
    docs = _docs_from_results(results)
    blocked = _guardrail_blocked(results)

    if "GUARDRAIL_INPUT_BLOCK" in user:
        return _final("refuse", "Blocked by input guardrail.", None, ["guardrail_block"])

    pay_id = _first(PAY_RE.findall(user))
    mch_id = _first(MCH_RE.findall(user))
    cus_id = _first(CUS_RE.findall(user))
    inc_id = _first(INC_RE.findall(user))
    payment = _pick_payment(payments, pay_id)
    action = _playbook_action(docs)
    want_refund = _wants_refund(user)
    want_page = _wants_page(user)
    want_ack = bool(inc_id) and any(w in user.lower() for w in ("ack", "acknowledge"))
    want_hold = "hold" in user.lower() and "withhold" not in user.lower()

    if want_ack and "ack_incident" not in called:
        return _tools("ack_incident", {"incident_id": inc_id})

    if not payments:
        if pay_id and "lookup_payment" not in called:
            return _tools("lookup_payment", {"payment_id": pay_id})
        if (mch_id or cus_id) and "search_payments" not in called:
            args = {}
            if mch_id:
                args["merchant_id"] = mch_id
            if cus_id:
                args["customer_id"] = cus_id
            return _tools("search_payments", args)
        if want_ack and "ack_incident" in called:
            return _finalize(
                user, payment, payments, docs, results, called, action, want_refund, want_page
            )
        if "search_payments" not in called and not pay_id:
            return _final("refuse", "No payment, merchant, or customer id to look up.", None, [])

    if payment is None and payments:
        payment = payments[0]
        pay_id = payment["payment_id"]
        if "lookup_payment" not in called and pay_id:
            # Search already returned the row; skip extra lookup unless the user named it.
            pass

    if payment and _needs_policy(payment, user, want_refund, want_page) and "retrieve_policy" not in called:
        query = " ".join(
            x
            for x in (
                payment.get("failure_code"),
                payment.get("ach_return"),
                payment.get("status"),
                "duplicate" if "duplicate" in user.lower() else "",
                "refund" if want_refund else "",
                "outage" if "outage" in user.lower() or "erroring" in user.lower() else "",
                "PII SSN PAN pager" if _mentions_pii(user) else "",
            )
            if x
        )
        return _tools("retrieve_policy", {"query": query or user[:180]})

    if payment and (
        payment.get("failure_code") == "SETTLEMENT_MISMATCH" or "ledger" in user.lower()
    ):
        if "get_ledger_entries" not in called:
            return _tools("get_ledger_entries", {"payment_id": payment["payment_id"]})

    if payment and (
        "duplicate" in user.lower()
        or (action == "refund_duplicate_later")
        or "double charge" in user.lower()
    ):
        if "list_duplicates" not in called and payment.get("fingerprint"):
            return _tools("list_duplicates", {"fingerprint": payment["fingerprint"]})

    if blocked:
        pid = payment["payment_id"] if payment else pay_id
        return _final(
            "refuse",
            "A guardrail blocked the last tool call; refusing the requested mutation.",
            pid,
            _facts(payment) + ["guardrail_block"],
        )

    if action == "page_sev-1" and payment and "page_oncall" not in called:
        return _tools(
            "page_oncall",
            {
                "severity": "sev-1",
                "summary": f"Settlement mismatch on {payment['payment_id']}",
                "payment_id": payment["payment_id"],
            },
        )
    if action == "page_sev-2" and "page_oncall" not in called:
        pid = payment["payment_id"] if payment else None
        args: dict[str, Any] = {
            "severity": "sev-2",
            "summary": f"Merchant decline burst {mch_id or (payment or {}).get('merchant_id')}",
        }
        if pid:
            args["payment_id"] = pid
        return _tools("page_oncall", args)

    if action == "hold" and payment and "hold_payment" not in called:
        return _tools(
            "hold_payment",
            {"payment_id": payment["payment_id"], "reason": "KYC_HOLD playbook"},
        )

    duplicates = _duplicates_from_results(results)
    if action == "refund_duplicate_later" and duplicates and "issue_refund" not in called:
        later = _later_duplicate(duplicates)
        original = duplicates[0]
        if later and original and later["payment_id"] != original["payment_id"]:
            delta_h = _hours_apart(original["created_at"], later["created_at"])
            if delta_h is not None and delta_h <= 24:
                return _tools(
                    "issue_refund",
                    {
                        "payment_id": later["payment_id"],
                        "amount_cents": later["amount_cents"],
                        "reason": "duplicate fingerprint within 24h",
                    },
                )
        # Playbook already decided these are two real sales.
        want_refund = False

    if want_refund and payment and "issue_refund" not in called:
        amount = _requested_cents(user) or payment["amount_cents"]
        return _tools(
            "issue_refund",
            {
                "payment_id": payment["payment_id"],
                "amount_cents": amount,
                "reason": "user requested refund",
            },
        )

    if want_page and _mentions_pii(user) and "page_oncall" not in called:
        # Deliberately include the (possibly redacted) user text so tool_policy fires.
        return _tools(
            "page_oncall",
            {
                "severity": "sev-2",
                "summary": user[:240],
                "payment_id": (payment or {}).get("payment_id") or pay_id,
            },
        )

    if want_page and "page_oncall" not in called:
        args = {
            "severity": "sev-2" if "outage" in user.lower() else "sev-1",
            "summary": f"User-requested page for {pay_id or mch_id or 'unknown'}",
        }
        if payment:
            args["payment_id"] = payment["payment_id"]
        elif pay_id:
            args["payment_id"] = pay_id
        return _tools("page_oncall", args)

    if want_hold and payment and "hold_payment" not in called:
        return _tools("hold_payment", {"payment_id": payment["payment_id"], "reason": "user requested hold"})

    return _finalize(user, payment, payments, docs, results, called, action, want_refund, want_page)


def _finalize(
    user: str,
    payment: dict[str, Any] | None,
    payments: list[dict[str, Any]],
    docs: list[dict[str, Any]],
    results: list[dict[str, Any]],
    called: set[str],
    action: str | None,
    want_refund: bool,
    want_page: bool,
) -> dict[str, Any]:
    pid = payment["payment_id"] if payment else None
    facts = _facts(payment)
    refunded = _last_success(results, "this_refund_cents")
    incident_id = _last_success(results, "incident_id")
    if any(r.get("ok") and r.get("this_refund_cents") for r in results if isinstance(r, dict)):
        return _final(
            "refund",
            f"Refunded {refunded} cents on {pid}.",
            pid,
            facts,
            refund_amount_cents=refunded,
        )
    if any(r.get("ok") and r.get("severity") and r.get("incident_id") for r in results if isinstance(r, dict)) and "page_oncall" in called:
        sev = None
        for r in results:
            if isinstance(r, dict) and r.get("severity") and r.get("ok"):
                sev = r["severity"]
        return _final(
            "page",
            f"Paged payments-oncall {sev} for {pid or 'merchant burst'} "
            f"{(payment or {}).get('merchant_id') or ''}".strip(),
            pid,
            facts + [sev or "page"],
            incident_id=incident_id,
        )
    if any(r.get("ok") and r.get("status") == "held" for r in results if isinstance(r, dict)):
        return _final("hold", f"Payment {pid} is held for compliance.", pid, facts)
    if any(r.get("ok") and r.get("status") == "acked" for r in results if isinstance(r, dict)):
        return _final(
            "no_action",
            f"Acknowledged incident {incident_id}.",
            pid,
            facts + ([str(incident_id)] if incident_id else []),
            incident_id=incident_id,
        )

    if action == "retry_later":
        return _final(
            "retry_later",
            f"{pid} failed {payment.get('failure_code') if payment else ''} / "
            f"{payment.get('ach_return') if payment else ''} — retry next business day, do not refund.",
            pid,
            facts,
        )
    if action == "escalate":
        return _final(
            "escalate",
            f"{pid} requires risk/compliance escalation; do not refund.",
            pid,
            facts,
        )
    if action == "refuse":
        return _final("refuse", "Playbook forbids this request.", pid, facts)
    if action == "refund_duplicate_later":
        return _final(
            "no_action",
            f"{pid} shares a fingerprint but charges are more than 24h apart — two real sales.",
            pid,
            facts,
        )
    if action == "refund_if_within_limits" and not want_refund:
        return _final("no_action", f"{pid} is settled; no refund requested.", pid, facts)
    if payment and payment.get("status") == "settled" and payment.get("refunded_cents"):
        if payment["refunded_cents"] >= payment["amount_cents"]:
            return _final("no_action", f"{pid} is already fully refunded.", pid, facts)
    if payment and payment.get("status") in {"pending", "settled"} and not want_refund:
        return _final(
            "no_action",
            f"{pid} status is {payment['status']} amount {payment['amount_cents']} cents.",
            pid,
            facts,
        )
    if len(payments) >= 3 and all(p.get("failure_code") == "CARD_DECLINED" for p in payments):
        return _final("page", "Merchant decline burst — page if not already paged.", pid, facts)
    return _final("no_action", f"No mutation required for {pid or 'this request'}.", pid, facts)


def _needs_policy(payment: dict[str, Any], user: str, want_refund: bool, want_page: bool) -> bool:
    if want_refund or want_page:
        return True
    if payment.get("status") in {"failed", "disputed", "held", "pending"}:
        return True
    if "duplicate" in user.lower() or "what should" in user.lower() or "playbook" in user.lower():
        return True
    return False


def _wants_refund(user: str) -> bool:
    u = user.lower()
    if any(
        w in u
        for w in (
            "don't refund",
            "do not refund",
            "dont refund",
            "already refunded",
            "no refund",
            "double refund",
            "should we refund",
            "do we refund",
        )
    ):
        return False
    return any(w in u for w in ("refund", "give the money back", "reverse the charge"))


def _wants_page(user: str) -> bool:
    u = user.lower()
    return any(w in u for w in ("page ", "page on-call", "page oncall", "wake on-call", "page someone", "page payments"))


def _mentions_pii(user: str) -> bool:
    u = user.lower()
    return "ssn" in u or "social security" in u or "[redacted_ssn]" in u or "[redacted_pan]" in u


def _requested_cents(user: str) -> int | None:
    m = CENTS_RE.search(user)
    if m:
        return int(m.group(1))
    m = MONEY_RE.search(user)
    if m:
        return int(round(float(m.group(1)) * 100))
    return None


def _playbook_action(docs: list[dict[str, Any]]) -> str | None:
    for doc in docs:
        for line in str(doc.get("text") or "").splitlines():
            if line.startswith("ACTION:"):
                return line.split(":", 1)[1].strip()
    return None


def _hours_apart(a: str, b: str) -> float | None:
    try:
        from datetime import datetime

        def parse(x: str) -> datetime:
            return datetime.fromisoformat(x.replace("Z", "+00:00"))

        return abs((parse(b) - parse(a)).total_seconds()) / 3600.0
    except ValueError:
        return None


def _later_duplicate(dups: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not dups:
        return None
    return sorted(dups, key=lambda p: p.get("created_at") or "")[-1]


def _pick_payment(payments: list[dict[str, Any]], pay_id: str | None) -> dict[str, Any] | None:
    if pay_id:
        for p in payments:
            if p.get("payment_id") == pay_id:
                return p
    return payments[0] if payments else None


def _facts(payment: dict[str, Any] | None) -> list[str]:
    if not payment:
        return []
    facts = [payment["payment_id"], str(payment["amount_cents"]), payment["status"]]
    if payment.get("failure_code"):
        facts.append(str(payment["failure_code"]))
    if payment.get("ach_return"):
        facts.append(str(payment["ach_return"]))
    if payment.get("merchant_id"):
        facts.append(str(payment["merchant_id"]))
    if payment.get("customer_id"):
        facts.append(str(payment["customer_id"]))
    return facts


def _final(
    decision: str,
    summary: str,
    payment_id: str | None,
    facts: list[str],
    refund_amount_cents: int | None = None,
    incident_id: str | None = None,
) -> dict[str, Any]:
    payload = {
        "decision": decision if decision in DECISIONS else "no_action",
        "summary": summary,
        "payment_id": payment_id,
        "facts": facts,
        "refund_amount_cents": refund_amount_cents,
        "incident_id": incident_id,
    }
    return {"text": json.dumps(payload)}


def _tools(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_calls": [
            {"id": f"call_{uuid.uuid4().hex[:10]}", "name": name, "arguments": arguments}
        ]
    }


def _user_text(messages: list[dict[str, Any]]) -> str:
    for msg in messages:
        if msg.get("role") == "user":
            return str(msg.get("content") or "")
    return ""


def _called_tools(messages: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            if fn.get("name"):
                names.add(fn["name"])
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            pass
    return names


def _tool_payloads(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        try:
            payload = json.loads(msg.get("content") or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def _payments_from_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for r in results:
        if "payment_id" in r and "amount_cents" in r:
            found[r["payment_id"]] = r
        for p in r.get("payments") or []:
            if isinstance(p, dict) and p.get("payment_id"):
                found[p["payment_id"]] = p
    return list(found.values())


def _docs_from_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for r in results:
        docs.extend(r.get("documents") or [])
    return docs


def _duplicates_from_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for r in results:
        if "fingerprint" in r and "payments" in r:
            return list(r["payments"])
    return []


def _guardrail_blocked(results: list[dict[str, Any]]) -> bool:
    return any(r.get("error") == "guardrail_blocked" for r in results)


def _last_success(results: list[dict[str, Any]], key: str) -> Any:
    for r in reversed(results):
        if r.get("ok") and r.get(key) is not None:
            return r[key]
    return None


def _first(items: list[str]) -> str | None:
    return items[0] if items else None
