"""USD prices per million tokens as (input, cached input, output, cache write).
Only models listed here get their spend tracked against the budget; anything
else (e.g. Fireworks) is left untracked rather than recorded at a wrong price.

Cache writes cost more than plain input (25% more for gpt-6-astra) and the API
reports them separately, so leaving them out under-counts nearly every fresh
prompt. Not modelled: the higher rates OpenAI applies to requests above 272k
tokens (the judges send at most about 30k)."""

from app.services.llm.base import Usage

PRICES_PER_M: dict[str, tuple[float, float, float, float]] = {
    "gpt-6-astra": (10.0, 1.0, 50.0, 12.5),
}


def is_priced(model: str) -> bool:
    return model in PRICES_PER_M


def cost_usd(model: str, usage: Usage) -> float | None:
    prices = PRICES_PER_M.get(model)
    if prices is None:
        return None
    input_price, cached_price, output_price, write_price = prices
    uncached = max(usage.input_tokens - usage.cached_tokens - usage.cache_write_tokens, 0)
    return (
        uncached * input_price
        + usage.cached_tokens * cached_price
        + usage.cache_write_tokens * write_price
        + usage.output_tokens * output_price
    ) / 1_000_000
