from google import genai
from google.genai import types
from google.genai.errors import ClientError

from app.services.llm.base import LLMClient, LLMResponse, Message, ToolCall, ToolSpec
from app.services.llm.rate_limiter import (
    CONNECTION_RETRYABLE_EXCEPTIONS,
    TokenBucket,
    with_backoff,
)


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
            return await self._client.aio.models.generate_content(
                model=self.model, contents=contents, config=config
            )

        resp = await with_backoff(
            _call, retryable_exceptions=(ClientError, *CONNECTION_RETRYABLE_EXCEPTIONS)
        )

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

        return LLMResponse(text=text, tool_call=tool_call, raw={})
