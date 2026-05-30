"""
app/agents/claude_client.py

Single AsyncAnthropic client shared across all agents.
Retries overloaded/rate-limited responses and limits parallel calls
so Mode 1's fan-out agents do not stampede the API.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx
from anthropic import APIConnectionError, APIStatusError, AsyncAnthropic, APITimeoutError

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
_settings = get_settings()

_RETRYABLE_STATUS = {429, 500, 502, 503, 529}
_MAX_ATTEMPTS = 8
_MAX_CONCURRENT = 8

_client = AsyncAnthropic(
    api_key=_settings.anthropic_api_key,
    max_retries=0,
    timeout=180.0,
)
_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)


async def create_message(**kwargs: Any) -> Any:
    """Call Claude with bounded concurrency and exponential backoff."""
    last_error: Exception | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with _semaphore:
                return await _client.messages.create(**kwargs)
        except APIStatusError as exc:
            last_error = exc
            status = exc.status_code
            if status not in _RETRYABLE_STATUS or attempt >= _MAX_ATTEMPTS:
                raise

            retry_after = exc.response.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                delay = float(retry_after)
            else:
                delay = min(2 ** (attempt - 1) + random.uniform(0.5, 1.5), 90.0)

            logger.warning(
                "claude.retry",
                attempt=attempt,
                max_attempts=_MAX_ATTEMPTS,
                status=status,
                delay_s=round(delay, 2),
                model=kwargs.get("model"),
            )
            await asyncio.sleep(delay)
        except (APIConnectionError, APITimeoutError, httpx.ReadError, httpx.ConnectError, httpx.TimeoutException) as exc:
            last_error = exc
            if attempt >= _MAX_ATTEMPTS:
                raise

            delay = min(2 ** (attempt - 1) + random.uniform(0.5, 1.5), 90.0)
            logger.warning(
                "claude.retry",
                attempt=attempt,
                max_attempts=_MAX_ATTEMPTS,
                reason="connection",
                delay_s=round(delay, 2),
                model=kwargs.get("model"),
                error=str(exc)[:120],
            )
            await asyncio.sleep(delay)

    if last_error:
        raise last_error
    raise RuntimeError("Claude request failed without an error")


class _MessagesProxy:
    async def create(self, **kwargs: Any) -> Any:
        return await create_message(**kwargs)


class _ClaudeProxy:
    messages = _MessagesProxy()


# Backward-compatible export used by all agents.
claude = _ClaudeProxy()
