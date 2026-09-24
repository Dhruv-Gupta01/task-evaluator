"""USD prices per million tokens as (input, cached input, output). Only models
listed here get their spend tracked against the budget; anything else
(e.g. Fireworks) is left untracked rather than recorded at a wrong price."""

from app.services.llm.base import Usage

PRICES_PER_M: dict[str, tuple[float, float, float]] = {
    "gpt-6-astra": (10.0, 1.0, 50.0),
}


def is_priced(model: str) -> bool:
    return model in PRICES_PER_M


def cost_usd(model: str, usage: Usage) -> float | None:
    prices = PRICES_PER_M.get(model)
    if prices is None:
        return None
    input_price, cached_price, output_price = prices
    uncached = max(usage.input_tokens - usage.cached_tokens, 0)
    return (
        uncached * input_price + usage.cached_tokens * cached_price + usage.output_tokens * output_price
    ) / 1_000_000
