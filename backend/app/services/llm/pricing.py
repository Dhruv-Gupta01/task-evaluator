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
    "gpt-6-sol": (2.0, 0.2, 10.0, 2.5),
    "gpt-6-luna": (0.1, 0.01, 0.5, 0.125),
    # Google paid tier; no cache-write premium, output includes thinking tokens.
    "gemini-3.5-flash-lite": (0.30, 0.03, 2.50, 0.30),
    # Anthropic list price; cache reads are 0.025x input on Fable 5.1, 5-minute cache writes 1.25x.
    "claude-fable-5-1": (10.0, 0.25, 50.0, 12.5),
    # Sonnet 5.5: $2 / $10 per M, cache read 0.1x, 5-minute cache write 1.25x.
    "claude-sonnet-5-5": (2.0, 0.20, 10.0, 2.5),
    # Sonnet 4.6 (Harbor's own default model for check/analyze): $3 / $15 per M, cache read 0.1x, 5-minute write 1.25x.
    "claude-sonnet-4-6": (3.0, 0.30, 15.0, 3.75),
    # Fireworks serverless gpt-oss-120b ($0.15 / $0.015 cached / $0.60); no cache-write premium.
    # Keyed without the "fireworks_ai/" LiteLLM prefix, which callers strip.
    "accounts/fireworks/models/gpt-oss-120b": (0.15, 0.015, 0.60, 0.15),
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
