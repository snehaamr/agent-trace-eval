"""Token and USD accounting.

CI prices the heuristic planner as gpt-4o-mini so cost regressions are about
prompt bloat and extra turns, not about which SaaS billed us.
"""

from __future__ import annotations

from dataclasses import dataclass

# Published list prices, USD per 1M tokens (OpenAI gpt-4o-mini / gpt-4o).
PRICE_TABLE: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"input_usd_per_1m": 0.15, "output_usd_per_1m": 0.60},
    "gpt-4o": {"input_usd_per_1m": 2.50, "output_usd_per_1m": 10.00},
    "heuristic": {"input_usd_per_1m": 0.15, "output_usd_per_1m": 0.60},
}


def estimate_tokens(text: str) -> int:
    """Cheap, deterministic stand-in for tiktoken (~4 chars/token)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def price_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    table = PRICE_TABLE.get(model) or PRICE_TABLE["gpt-4o-mini"]
    return (
        input_tokens * table["input_usd_per_1m"] + output_tokens * table["output_usd_per_1m"]
    ) / 1_000_000


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model: str
