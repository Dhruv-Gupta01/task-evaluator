"""Self-contained LLMClient construction from plain environment variables —
deliberately does NOT depend on app.config/pydantic-settings, since this
module is copied into the agent wrapper image and runs in a container that
has no access to the backend's .env file or full app package."""

import os

from app.services.llm.base import LLMClient
from app.services.llm.rate_limiter import TokenBucket

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"


def get_llm_client_from_env() -> LLMClient:
    provider = os.environ.get("LLM_PROVIDER", "gemini")
    model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
    rpm = int(os.environ.get("LLM_RPM", "10"))
    tpm_raw = os.environ.get("LLM_TPM", "")
    tpm = int(tpm_raw) if tpm_raw else None

    rate_limiter = TokenBucket(rpm=rpm, tpm=tpm)

    if provider == "anthropic":
        from app.services.llm.anthropic_client import AnthropicClient

        return AnthropicClient(
            model=model, api_key=os.environ["ANTHROPIC_API_KEY"], rate_limiter=rate_limiter
        )
    if provider == "openai":
        from app.services.llm.openai_client import OpenAIClient

        return OpenAIClient(
            model=model, api_key=os.environ["OPENAI_API_KEY"], rate_limiter=rate_limiter
        )
    if provider == "groq":
        from app.services.llm.openai_client import OpenAIClient

        return OpenAIClient(
            model=model,
            api_key=os.environ["GROQ_API_KEY"],
            rate_limiter=rate_limiter,
            base_url=GROQ_BASE_URL,
        )
    if provider == "fireworks":
        from app.services.llm.openai_client import OpenAIClient

        return OpenAIClient(
            model=model,
            api_key=os.environ["FIREWORKS_API_KEY"],
            rate_limiter=rate_limiter,
            base_url=FIREWORKS_BASE_URL,
        )
    if provider == "gemini":
        from app.services.llm.gemini_client import GeminiClient

        return GeminiClient(
            model=model, api_key=os.environ["GEMINI_API_KEY"], rate_limiter=rate_limiter
        )

    raise ValueError(f"unknown LLM provider: {provider}")
