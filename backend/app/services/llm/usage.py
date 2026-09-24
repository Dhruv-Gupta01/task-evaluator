"""Collects token usage from LLM calls made while a stage runs, so the judges
(which create their own client and only return a verdict) can still have
their spend recorded. The collector lives in a ContextVar: each stage runs
as its own asyncio task, so concurrent stages never see each other's calls."""

from contextvars import ContextVar

from app.services.llm.base import Usage

_calls: ContextVar[list[tuple[str, Usage]] | None] = ContextVar("llm_calls", default=None)


def start() -> list[tuple[str, Usage]]:
    """Begin collecting in the current task; returns the live list of
    (model, usage) pairs, appended to as calls complete."""
    calls: list[tuple[str, Usage]] = []
    _calls.set(calls)
    return calls


def report(model: str, usage: Usage) -> None:
    calls = _calls.get()
    if calls is not None:
        calls.append((model, usage))
