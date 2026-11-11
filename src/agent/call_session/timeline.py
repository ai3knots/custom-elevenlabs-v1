"""What happened in the call and when: best-effort persistence and the per-call timing events."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any

from src.agent.call_clock import CallClock
from src.persistence.recorder import CallRecorder
from src.persistence.remote import RemoteRecorder

logger = logging.getLogger("posivex.session")

Recorder = CallRecorder | RemoteRecorder


class CallTimeline:
    """Writes to the recorder without ever letting a storage problem reach the call, and stamps timing events."""

    def __init__(self, call_id: str, recorder: Recorder, clock: CallClock) -> None:
        self.call_id = call_id
        self.recorder = recorder
        self.clock = clock
        self._event_tasks: set[asyncio.Task[Any]] = set()

    async def persist(self, label: str, coro: Awaitable[Any]) -> Any:
        try:
            return await coro
        except Exception as e:
            logger.error("Persistence step '%s' failed: %s", label, e, exc_info=True)
            return None

    def mark(self, name: str, **extra: Any) -> None:
        """Log and store a timing event with its time since pickup, for call analysis after the fact.

        The agent's logs disappear when the worker scales to zero; these events travel with the call.
        """
        secs = round(self.clock.since_pickup(), 2)
        logger.info("timing %s at +%.2fs since pickup %s", name, secs, extra or "")
        task = asyncio.create_task(
            self.persist(
                f"event:timing_{name}",
                self.recorder.record_event(
                    call_id=self.call_id,
                    event_type=f"timing_{name}",
                    state=None,
                    payload={"since_pickup_secs": secs, **extra},
                ),
            )
        )
        self._event_tasks.add(task)
        task.add_done_callback(self._event_tasks.discard)

    async def flush(self) -> None:
        """Wait until the timing events started so far are stored (before the call is finalized)."""
        if self._event_tasks:
            await asyncio.gather(*self._event_tasks, return_exceptions=True)
