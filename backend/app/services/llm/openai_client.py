import json

from openai import AsyncOpenAI, BadRequestError, InternalServerError, RateLimitError

from app.services.llm.base import LLMClient, LLMResponse, Message, ToolCall, ToolSpec
from app.services.llm.rate_limiter import (
    CONNECTION_RETRYABLE_EXCEPTIONS,
    TokenBucket,
    with_backoff,
)


class OpenAIClient(LLMClient):
    """Also used for Groq, which exposes an OpenAI-compatible endpoint —
    pass base_url to point at it."""

    def __init__(
        self,
        model: str,
        api_key: str,
        rate_limiter: TokenBucket,
        base_url: str | None = None,
    ):
        super().__init__(model)
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._rate_limiter = rate_limiter

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        await self._rate_limiter.acquire()

        oai_messages = [{"role": m.role, "content": m.content} for m in messages]
        oai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

        extra_kwargs = {"tools": oai_tools} if oai_tools else {}

        async def _call():
            return await self._client.chat.completions.create(
                model=self.model,
                messages=oai_messages,
                **extra_kwargs,
            )

        try:
            resp = await with_backoff(
                _call,
                retryable_exceptions=(
                    RateLimitError,
                    InternalServerError,
                    *CONNECTION_RETRYABLE_EXCEPTIONS,
                ),
            )
        except BadRequestError as e:
            # Some providers (observed on Groq) hard-reject a malformed tool-
            # call generation with a 400 "output_parse_failed" instead of
            # returning a normal response with no tool_calls. Treat this like
            # "the model didn't produce a valid tool call" rather than
            # letting it crash the whole agent loop — surface whatever
            # partial text the model was attempting so it can retry.
            body = getattr(e, "body", None) or {}
            error_info = body.get("error", {}) if isinstance(body, dict) else {}
            if error_info.get("code") == "output_parse_failed":
                fallback_text = error_info.get("failed_generation", "")
                return LLMResponse(text=fallback_text, tool_call=None, raw={"error": error_info})
            raise

        choice = resp.choices[0]
        text = choice.message.content or ""
        tool_call: ToolCall | None = None
        if choice.message.tool_calls:
            tc = choice.message.tool_calls[0]
            tool_call = ToolCall(
                id=tc.id, name=tc.function.name, arguments=json.loads(tc.function.arguments)
            )

        return LLMResponse(text=text, tool_call=tool_call, raw=resp.model_dump())
