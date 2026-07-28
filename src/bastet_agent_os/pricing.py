"""Model pricing (SPEC §5.2.3).

Price data follows the LiteLLM community `model_prices_and_context_window.json`
format (per-token USD costs). We consume that public JSON as data — refresh it
into ~/.bastet/model_prices.json with `bastet pricing update` — with a small
bundled fallback so accounting works out of the box.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Minimal fallback (per-token USD). The local prices file, when present,
# always wins; these are just a floor so cost is never silently zero for
# common models. Update via `bastet pricing update`.
FALLBACK_PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-4-20250514": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_read_input_token_cost": 3e-07,
        "cache_creation_input_token_cost": 3.75e-06,
    },
    "claude-opus-4-20250514": {
        "input_cost_per_token": 1.5e-05,
        "output_cost_per_token": 7.5e-05,
        "cache_read_input_token_cost": 1.5e-06,
        "cache_creation_input_token_cost": 1.875e-05,
    },
    "claude-haiku-4-5-20251001": {
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 5e-06,
        "cache_read_input_token_cost": 1e-07,
        "cache_creation_input_token_cost": 1.25e-06,
    },
    "gpt-4o": {
        "input_cost_per_token": 2.5e-06,
        "output_cost_per_token": 1e-05,
        "cache_read_input_token_cost": 1.25e-06,
    },
}

PRICES_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def add(self, other: Usage) -> None:
        self.tokens_in += other.tokens_in
        self.tokens_out += other.tokens_out
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write


class PriceBook:
    def __init__(self, prices_file: Path | None = None):
        self.prices: dict[str, dict] = dict(FALLBACK_PRICES)
        if prices_file and prices_file.exists():
            try:
                self.prices.update(json.loads(prices_file.read_text()))
            except (json.JSONDecodeError, OSError):
                pass  # fallback prices still apply

    def _lookup(self, model: str) -> dict | None:
        if model in self.prices:
            return self.prices[model]
        # LiteLLM keys are sometimes provider-prefixed ("anthropic/claude-...")
        for key, entry in self.prices.items():
            if key.endswith("/" + model) or model.endswith("/" + key):
                return entry
        return None

    def cost_usd(self, model: str, usage: Usage) -> float:
        """Cost with cache tokens priced separately (cache_read != input)."""
        entry = self._lookup(model)
        if not entry:
            return 0.0
        input_cost = float(entry.get("input_cost_per_token", 0) or 0)
        output_cost = float(entry.get("output_cost_per_token", 0) or 0)
        cache_read_cost = float(entry.get("cache_read_input_token_cost", 0) or 0)
        cache_write_cost = float(entry.get("cache_creation_input_token_cost", 0) or 0)
        return (
            usage.tokens_in * input_cost
            + usage.tokens_out * output_cost
            + usage.cache_read * cache_read_cost
            + usage.cache_write * cache_write_cost
        )
