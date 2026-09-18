"""LLM providers: OpenAI-compatible HTTP and a deterministic CI planner."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from opentelemetry.trace import SpanKind

from .cost import estimate_tokens
from .planner import plan_turn
from .telemetry import genai_span, set_json_attr


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = "heuristic"
    response_id: str = ""
    provider: str = "heuristic"


class LLM(Protocol):
    model: str
    provider: str

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        conversation_id: str,
    ) -> LLMResponse: ...


def _usage_from_messages(messages: list[dict[str, Any]], output: str) -> tuple[int, int]:
    blob = json.dumps(messages, default=str)
    return estimate_tokens(blob), estimate_tokens(output)


class HeuristicLLM:
    """In-process planner that emits the same tool-call schema as a chat model."""

    def __init__(self, model: str = "heuristic") -> None:
        self.model = model
        self.provider = "heuristic"

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        conversation_id: str,
    ) -> LLMResponse:
        with genai_span(
            f"chat {self.model}",
            operation="chat",
            kind=SpanKind.INTERNAL,
            attributes={
                "gen_ai.provider.name": self.provider,
                "gen_ai.request.model": self.model,
                "gen_ai.response.model": self.model,
                "gen_ai.conversation.id": conversation_id,
                "gen_ai.request.temperature": 0.0,
            },
        ) as span:
            set_json_attr(span, "gen_ai.input.messages", _otel_input_messages(messages))
            planned = plan_turn(messages)
            output_blob = json.dumps(planned, default=str)
            inp, out = _usage_from_messages(messages, output_blob)
            span.set_attribute("gen_ai.usage.input_tokens", inp)
            span.set_attribute("gen_ai.usage.output_tokens", out)
            rid = f"heur_{uuid.uuid4().hex[:12]}"
            span.set_attribute("gen_ai.response.id", rid)
            if planned.get("tool_calls"):
                calls = [
                    ToolCall(
                        id=c.get("id") or f"call_{uuid.uuid4().hex[:10]}",
                        name=c["name"],
                        arguments=c.get("arguments") or {},
                    )
                    for c in planned["tool_calls"]
                ]
                set_json_attr(
                    span,
                    "gen_ai.output.messages",
                    [
                        {
                            "role": "assistant",
                            "parts": [
                                {
                                    "type": "tool_call",
                                    "id": c.id,
                                    "name": c.name,
                                    "arguments": c.arguments,
                                }
                                for c in calls
                            ],
                            "finish_reason": "tool_calls",
                        }
                    ],
                )
                span.set_attribute("gen_ai.response.finish_reasons", ("tool_calls",))
                return LLMResponse(
                    text=None,
                    tool_calls=calls,
                    input_tokens=inp,
                    output_tokens=out,
                    model=self.model,
                    response_id=rid,
                    provider=self.provider,
                )
            text = planned.get("text") or "{}"
            set_json_attr(
                span,
                "gen_ai.output.messages",
                [
                    {
                        "role": "assistant",
                        "parts": [{"type": "text", "content": text}],
                        "finish_reason": "stop",
                    }
                ],
            )
            span.set_attribute("gen_ai.response.finish_reasons", ("stop",))
            return LLMResponse(
                text=text,
                input_tokens=inp,
                output_tokens=out,
                model=self.model,
                response_id=rid,
                provider=self.provider,
            )


class OpenAICompatLLM:
    """Minimal Chat Completions client. No SDK, no LangChain."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = model or os.environ.get("AGENT_MODEL", "gpt-4o-mini")
        self.provider = "openai"
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip(
            "/"
        )
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is required for AGENT_LLM=openai")

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        conversation_id: str,
    ) -> LLMResponse:
        body = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with genai_span(
            f"chat {self.model}",
            operation="chat",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.provider.name": self.provider,
                "gen_ai.request.model": self.model,
                "gen_ai.conversation.id": conversation_id,
                "gen_ai.request.temperature": 0.0,
                "server.address": self.base_url,
            },
        ) as span:
            set_json_attr(span, "gen_ai.input.messages", _otel_input_messages(messages))
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                span.set_attribute("error.type", "http_error")
                raise RuntimeError(f"openai http {exc.code}: {detail}") from exc
            choice = payload["choices"][0]
            message = choice["message"]
            usage = payload.get("usage") or {}
            inp = int(usage.get("prompt_tokens") or 0)
            out = int(usage.get("completion_tokens") or 0)
            span.set_attribute("gen_ai.usage.input_tokens", inp)
            span.set_attribute("gen_ai.usage.output_tokens", out)
            span.set_attribute("gen_ai.response.id", payload.get("id") or "")
            span.set_attribute("gen_ai.response.model", payload.get("model") or self.model)
            raw_calls = message.get("tool_calls") or []
            if raw_calls:
                calls = []
                for raw in raw_calls:
                    fn = raw.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    calls.append(ToolCall(id=raw.get("id") or "", name=fn.get("name") or "", arguments=args))
                span.set_attribute("gen_ai.response.finish_reasons", ("tool_calls",))
                return LLMResponse(
                    text=None,
                    tool_calls=calls,
                    input_tokens=inp,
                    output_tokens=out,
                    model=self.model,
                    response_id=payload.get("id") or "",
                    provider=self.provider,
                )
            text = message.get("content") or ""
            span.set_attribute("gen_ai.response.finish_reasons", (choice.get("finish_reason") or "stop",))
            return LLMResponse(
                text=text,
                input_tokens=inp,
                output_tokens=out,
                model=self.model,
                response_id=payload.get("id") or "",
                provider=self.provider,
            )


def build_llm() -> LLM:
    kind = os.environ.get("AGENT_LLM", "heuristic").strip().lower()
    if kind in {"heuristic", "mock", "ci"}:
        return HeuristicLLM()
    if kind in {"openai", "openai_compat"}:
        return OpenAICompatLLM()
    raise RuntimeError(f"unknown AGENT_LLM={kind}")


def _otel_input_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim to the OTel input-messages shape without dumping full tool payloads twice."""
    out = []
    for msg in messages:
        role = msg.get("role") or "user"
        if role == "system":
            continue
        parts: list[dict[str, Any]] = []
        if msg.get("content"):
            parts.append({"type": "text", "content": str(msg["content"])[:4000]})
        if msg.get("tool_calls"):
            for call in msg["tool_calls"]:
                fn = call.get("function") or {}
                parts.append(
                    {
                        "type": "tool_call",
                        "id": call.get("id"),
                        "name": fn.get("name"),
                    }
                )
        if role == "tool":
            parts.append(
                {
                    "type": "tool_call_response",
                    "id": msg.get("tool_call_id"),
                    "result": str(msg.get("content") or "")[:2000],
                }
            )
        out.append({"role": role, "parts": parts})
    return out
