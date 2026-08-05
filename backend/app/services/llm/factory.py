from app.config import get_settings
from app.services.llm.base import LLMClient
from app.services.llm.rate_limiter import DailyQuota, TokenBucket

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"
DEFAULT_MAX_CALLS_PER_DAY = 1000


def get_llm_client() -> LLMClient:
    settings = get_settings()
    provider = settings.llm_provider
    rate_limiter = TokenBucket(rpm=settings.llm_rpm, tpm=settings.llm_tpm)

    if provider == "anthropic":
        from app.services.llm.anthropic_client import AnthropicClient

        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")
        return AnthropicClient(
            model=settings.llm_model, api_key=settings.anthropic_api_key, rate_limiter=rate_limiter
        )

    if provider == "openai":
        from app.services.llm.openai_client import OpenAIClient

        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY not set")
        return OpenAIClient(
            model=settings.llm_model, api_key=settings.openai_api_key, rate_limiter=rate_limiter
        )

    if provider == "groq":
        from app.services.llm.openai_client import OpenAIClient

        if not settings.groq_api_key:
            raise ValueError("GROQ_API_KEY not set")
        return OpenAIClient(
            model=settings.llm_model,
            api_key=settings.groq_api_key,
            rate_limiter=rate_limiter,
            base_url=GROQ_BASE_URL,
        )

    if provider == "fireworks":
        from app.services.llm.openai_client import OpenAIClient

        if not settings.fireworks_api_key:
            raise ValueError("FIREWORKS_API_KEY not set")
        return OpenAIClient(
            model=settings.llm_model,
            api_key=settings.fireworks_api_key,
            rate_limiter=rate_limiter,
            base_url=FIREWORKS_BASE_URL,
        )

    if provider == "gemini":
        from app.services.llm.gemini_client import GeminiClient

        if not settings.gemini_api_key:
            raise ValueError("GEMINI_API_KEY not set")
        return GeminiClient(
            model=settings.llm_model, api_key=settings.gemini_api_key, rate_limiter=rate_limiter
        )

    raise ValueError(f"unknown LLM provider: {provider}")


def get_daily_quota() -> DailyQuota:
    settings = get_settings()
    return DailyQuota(
        provider=settings.llm_provider,
        max_calls_per_day=DEFAULT_MAX_CALLS_PER_DAY,
        storage_dir=settings.storage_dir / "quota",
    )
