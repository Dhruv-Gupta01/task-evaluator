from anthropic import AsyncAnthropic, RateLimitError

from app.services.llm import usage as usage_collector
from app.services.llm.base import LLMClient, LLMResponse, Message, ToolCall, ToolSpec, Usage
from app.services.llm.rate_limiter import (
    CONNECTION_RETRYABLE_EXCEPTIONS,
    TokenBucket,
    with_backoff,
)


class AnthropicClient(LLMClient):
    def __init__(
        self,
        model: str,
        api_key: str,
        rate_limiter: TokenBucket,
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
    ):
        super().__init__(model)
        self._client = AsyncAnthropic(api_key=api_key)
        self._rate_limiter = rate_limiter
        self._reasoning_effort = reasoning_effort
        self._max_output_tokens = max_output_tokens

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
        if self._reasoning_effort:
            # output_config.effort is GA (no beta header); sent as a raw body field so it
            # works whatever the installed SDK version declares.
            extra_kwargs["extra_body"] = {"output_config": {"effort": self._reasoning_effort}}

        async def _call():
            return await self._client.messages.create(
                model=self.model,
                max_tokens=self._max_output_tokens or 16000,  # the API requires a cap; 0 in settings means this
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

        usage = None
        if resp.usage is not None:
            # Anthropic reports uncached input separately from cache reads and writes;
            # Usage.input_tokens is the total of all three.
            cache_read = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
            cache_write = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
            usage = Usage(
                input_tokens=(resp.usage.input_tokens or 0) + cache_read + cache_write,
                cached_tokens=cache_read,
                cache_write_tokens=cache_write,
                output_tokens=resp.usage.output_tokens or 0,
            )
            usage_collector.report(self.model, usage)

        return LLMResponse(
            text="".join(text_parts), tool_call=tool_call, raw=resp.model_dump(), usage=usage
        )
