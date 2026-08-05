import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import httpx

T = TypeVar("T")

# Transient network failures (dropped connection, connect/read timeout) that
# should retry with backoff rather than crash the caller outright — observed
# live as an uncaught httpx.ConnectTimeout killing an agent trial.
CONNECTION_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
)


class QuotaExceededError(Exception):
    pass


class TokenBucket:
    """Async token bucket rate limiter: rpm (requests/min) and optionally
    tpm (tokens/min). acquire() blocks until capacity is available."""

    def __init__(self, rpm: int, tpm: int | None = None):
        self.rpm = rpm
        self.tpm = tpm
        self._capacity = float(rpm)
        self._tokens = float(rpm)
        self._tpm_capacity = float(tpm) if tpm else None
        self._tpm_tokens = float(tpm) if tpm else None
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now
        self._tokens = min(self._capacity, self._tokens + elapsed * (self.rpm / 60.0))
        if self._tpm_tokens is not None and self.tpm is not None:
            self._tpm_tokens = min(
                self._tpm_capacity, self._tpm_tokens + elapsed * (self.tpm / 60.0)
            )

    async def acquire(self, estimated_tokens: int = 0) -> None:
        async with self._lock:
            while True:
                self._refill()
                tpm_ok = self._tpm_tokens is None or self._tpm_tokens >= estimated_tokens
                if self._tokens >= 1 and tpm_ok:
                    self._tokens -= 1
                    if self._tpm_tokens is not None:
                        self._tpm_tokens -= estimated_tokens
                    return
                await asyncio.sleep(0.1)


async def with_backoff(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 5,
    base: float = 1.0,
    cap: float = 30.0,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """Call fn() with exponential backoff + jitter on retryable exceptions."""
    attempt = 0
    while True:
        try:
            return await fn()
        except retryable_exceptions:
            attempt += 1
            if attempt > max_retries:
                raise
            delay = min(cap, base * (2 ** (attempt - 1))) + random.uniform(0, base)
            await asyncio.sleep(delay)


class DailyQuota:
    """File-based daily call counter, reset at UTC midnight. Raises
    QuotaExceededError once max_calls_per_day is hit for the current day."""

    def __init__(self, provider: str, max_calls_per_day: int, storage_dir: Path):
        self.provider = provider
        self.max_calls_per_day = max_calls_per_day
        self.path = storage_dir / f"{provider}.json"

    def _today(self) -> str:
        return datetime.now(UTC).date().isoformat()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except Exception:
                pass
        return {"date": None, "count": 0}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data))

    def increment(self) -> None:
        data = self._load()
        today = self._today()
        if data.get("date") != today:
            data = {"date": today, "count": 0}
        if data["count"] >= self.max_calls_per_day:
            raise QuotaExceededError(
                f"{self.provider}: daily quota of {self.max_calls_per_day} calls exceeded"
            )
        data["count"] += 1
        self._save(data)

    def count_today(self) -> int:
        data = self._load()
        if data.get("date") != self._today():
            return 0
        return data["count"]
