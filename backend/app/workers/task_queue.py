import asyncio
from collections.abc import Coroutine
from typing import Any

_tasks: dict[str, asyncio.Task] = {}


async def submit(key: str, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Schedule coro as a tracked background task under `key`. If a task is
    already running under that key, cancel it first (cancel-and-restart),
    matching "each stage independently re-runnable at any time"."""
    existing = _tasks.get(key)
    if existing is not None and not existing.done():
        existing.cancel()
        try:
            await existing
        except (asyncio.CancelledError, Exception):
            pass

    task = asyncio.create_task(coro)
    _tasks[key] = task

    def _cleanup(t: asyncio.Task, key: str = key) -> None:
        if _tasks.get(key) is t:
            _tasks.pop(key, None)

    task.add_done_callback(_cleanup)
    return task


def is_running(key: str) -> bool:
    task = _tasks.get(key)
    return task is not None and not task.done()
