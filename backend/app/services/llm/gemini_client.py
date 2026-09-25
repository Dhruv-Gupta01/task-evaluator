from google import genai
from google.genai import types
from google.genai.errors import ClientError, ServerError

from app.services.llm import usage as usage_collector
from app.services.llm.base import LLMClient, LLMResponse, Message, ToolCall, ToolSpec, Usage
from app.services.llm.rate_limiter import (
    CONNECTION_RETRYABLE_EXCEPTIONS,
    TokenBucket,
    with_backoff,
)


class _PermanentError(Exception):
    """Wraps a ClientError that retrying can't fix (bad request, auth, model
    gone), so with_backoff lets it through immediately instead of retrying."""

    def __init__(self, original: ClientError):
        super().__init__(str(original))
        self.original = original


class GeminiClient(LLMClient):
    def __init__(self, model: str, api_key: str, rate_limiter: TokenBucket):
        super().__init__(model)
        self._client = genai.Client(api_key=api_key)
        self._rate_limiter = rate_limiter

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        await self._rate_limiter.acquire()

        system = "\n".join(m.content for m in messages if m.role == "system")
        contents = [
            types.Content(
                role=("model" if m.role == "assistant" else "user"),
                parts=[types.Part(text=m.content)],
            )
            for m in messages
            if m.role != "system"
        ]
        function_decls = [
            types.FunctionDeclaration(
                name=t.name, description=t.description, parametersJsonSchema=t.parameters
            )
            for t in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            tools=[types.Tool(functionDeclarations=function_decls)] if function_decls else None,
        )

        async def _call():
            try:
                return await self._client.aio.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
            except ClientError as e:
                if getattr(e, "code", None) == 429:  # rate limit / quota: worth retrying
                    raise
                raise _PermanentError(e) from e

        try:
            resp = await with_backoff(
                _call,
                retryable_exceptions=(ClientError, ServerError, *CONNECTION_RETRYABLE_EXCEPTIONS),
            )
        except _PermanentError as e:
            raise e.original from None

        usage = None
        meta = getattr(resp, "usage_metadata", None)
        if meta is not None:
            usage = Usage(
                input_tokens=meta.prompt_token_count or 0,  # includes cached tokens
                cached_tokens=meta.cached_content_token_count or 0,
                # thinking tokens are billed as output
                output_tokens=(meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0),
            )
            usage_collector.report(self.model, usage)

        text = resp.text or ""
        tool_call: ToolCall | None = None
        if resp.candidates and resp.candidates[0].content and resp.candidates[0].content.parts:
            for part in resp.candidates[0].content.parts:
                if part.function_call:
                    tool_call = ToolCall(
                        id=part.function_call.name,
                        name=part.function_call.name,
                        arguments=dict(part.function_call.args or {}),
                    )
                    break

        return LLMResponse(text=text, tool_call=tool_call, raw={}, usage=usage)
