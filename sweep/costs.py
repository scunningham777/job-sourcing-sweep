"""Running cost tally, so every run tells you what it spent."""

from dataclasses import dataclass, field

# USD per million tokens (input, output, cache read). Check platform.claude.com pricing if these drift.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5": (2.00, 10.00, 0.20),  # possible refusal-fallback target
    "claude-opus-5": (5.00, 25.00, 0.50),    # possible refusal-fallback target
    "claude-opus-4-8": (5.00, 25.00, 0.50),  # possible refusal-fallback target
}
WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches


@dataclass
class CostTracker:
    lines: dict[str, float] = field(default_factory=dict)
    searches: int = 0

    def add(self, label: str, model: str, usage) -> None:
        price_in, price_out, price_cache = PRICES.get(model, PRICES["claude-opus-5"])
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cost = (
            usage.input_tokens * price_in
            + cache_write * price_in * 1.25
            + cache_read * price_cache
            + usage.output_tokens * price_out
        ) / 1_000_000
        server = getattr(usage, "server_tool_use", None)
        n_search = (getattr(server, "web_search_requests", 0) or 0) if server else 0
        self.searches += n_search
        cost += n_search * WEB_SEARCH_USD
        self.lines[label] = self.lines.get(label, 0.0) + cost

    @property
    def total(self) -> float:
        return sum(self.lines.values())

    def report(self) -> str:
        rows = [f"  {label:<28} ${cost:6.3f}" for label, cost in self.lines.items()]
        rows.append(f"  {'TOTAL':<28} ${self.total:6.3f}  ({self.searches} web searches)")
        return "\n".join(rows)
