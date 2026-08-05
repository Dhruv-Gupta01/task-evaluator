from anthropic import AsyncAnthropic, RateLimitError

from app.services.llm.base import LLMClient, LLMResponse, Message, ToolCall, ToolSpec
from app.services.llm.rate_limiter import (
    CONNECTION_RETRYABLE_EXCEPTIONS,
    TokenBucket,
    with_backoff,
)


class AnthropicClient(LLMClient):
    def __init__(self, model: str, api_key: str, rate_limiter: TokenBucket):
        super().__init__(model)
        self._client = AsyncAnthropic(api_key=api_key)
        self._rate_limiter = rate_limiter

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        await self._rate_limiter.acquire()

        system = "\n".join(m.content for m in messages if m.role == "system")
        anth_messages = [
            {"role": m.role, "content": m.content} for m in messages if m.role != "system"
        ]
        anth_tools = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]
        extra_kwargs = {"tools": anth_tools} if anth_tools else {}

        async def _call():
            return await self._client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system,
                messages=anth_messages,
                **extra_kwargs,
            )

        resp = await with_backoff(
            _call, retryable_exceptions=(RateLimitError, *CONNECTION_RETRYABLE_EXCEPTIONS)
        )

        text_parts: list[str] = []
        tool_call: ToolCall | None = None
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_call = ToolCall(id=block.id, name=block.name, arguments=block.input)

        return LLMResponse(text="".join(text_parts), tool_call=tool_call, raw=resp.model_dump())
