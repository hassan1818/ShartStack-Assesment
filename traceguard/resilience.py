"""Bounded retry. Never unlimited: max_attempts is a hard ceiling."""
from __future__ import annotations

import time
from typing import Callable, TypeVar

from .tools import ToolError

T = TypeVar("T")


class ToolUnavailable(Exception):
    """All bounded attempts failed; the caller must use its fallback."""

    def __init__(self, tool: str, attempts: int, last: Exception):
        super().__init__(f"{tool} failed after {attempts} attempt(s): {last}")
        self.tool, self.attempts, self.last = tool, attempts, last


def call_with_retry(tool: str, fn: Callable[[], T], *, max_attempts: int = 3,
                    base_delay: float = 0.0, trace: list | None = None,
                    sleep: Callable[[float], None] = time.sleep) -> T:
    last: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = fn()
            if trace is not None:
                trace.append(f"{tool}: attempt {attempt}/{max_attempts} ok")
            return result
        except ToolError as exc:  # only known, retryable failures
            last = exc
            if trace is not None:
                trace.append(f"{tool}: attempt {attempt}/{max_attempts} failed ({exc})")
            if attempt < max_attempts and base_delay:
                sleep(base_delay * 2 ** (attempt - 1))  # exponential backoff
    raise ToolUnavailable(tool, max_attempts, last)  # type: ignore[arg-type]
