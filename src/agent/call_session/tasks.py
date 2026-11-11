"""Background tasks of one call: kept referenced so they are not garbage-collected, dropped when they finish."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


class BackgroundTasks:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task
