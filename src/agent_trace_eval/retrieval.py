"""Keyword retrieval over FastPay exception playbooks (no vector DB)."""

from __future__ import annotations

import re
from typing import Any

from opentelemetry.trace import SpanKind

from .telemetry import DATA_SOURCE_ID, genai_span, set_json_attr

_TOKEN = re.compile(r"[a-z0-9_]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


def retrieve_playbooks(query: str, playbooks: list[dict[str, Any]], top_k: int = 3) -> list[dict[str, Any]]:
    q = _tokens(query)
    ranked: list[tuple[float, dict[str, Any]]] = []
    for doc in playbooks:
        tags = {t.lower() for t in doc.get("tags") or []}
        hay = _tokens(doc["title"] + " " + doc["text"]) | tags
        overlap = len(q & hay)
        tag_hits = len({t for t in tags if t in query.lower() or t in q})
        score = overlap + 3.0 * tag_hits
        if score > 0:
            ranked.append((score, doc))
    ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
    out = []
    for score, doc in ranked[:top_k]:
        out.append(
            {
                "id": doc["id"],
                "title": doc["title"],
                "score": score,
                "text": doc["text"],
            }
        )
    return out


def traced_retrieve(query: str, playbooks: list[dict[str, Any]], top_k: int = 3) -> list[dict[str, Any]]:
    with genai_span(
        f"retrieval {DATA_SOURCE_ID}",
        operation="retrieval",
        kind=SpanKind.CLIENT,
        attributes={
            "gen_ai.provider.name": "agent_trace_eval",
            "gen_ai.data_source.id": DATA_SOURCE_ID,
            "gen_ai.retrieval.query.text": query,
            "gen_ai.retrieval.top_k": top_k,
        },
    ) as span:
        docs = retrieve_playbooks(query, playbooks, top_k=top_k)
        set_json_attr(
            span,
            "gen_ai.retrieval.documents",
            [{"id": d["id"], "score": d["score"]} for d in docs],
        )
        return docs
