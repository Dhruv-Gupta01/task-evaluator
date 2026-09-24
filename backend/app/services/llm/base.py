from abc import ABC, abstractmethod

from pydantic import BaseModel


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict  # JSON schema


class Message(BaseModel):
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict


class Usage(BaseModel):
    input_tokens: int = 0  # includes cached_tokens
    cached_tokens: int = 0
    output_tokens: int = 0  # includes reasoning tokens


class LLMResponse(BaseModel):
    text: str
    tool_call: ToolCall | None = None
    raw: dict = {}
    usage: Usage | None = None


class LLMClient(ABC):
    def __init__(self, model: str):
        self.model = model

    @abstractmethod
    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse: ...
