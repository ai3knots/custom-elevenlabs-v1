"""How a call ends: the first reason wins, a goodbye without `end_call` is rescued, and a call has a hard time cap."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.agent.call_session.tasks import BackgroundTasks
from src.agent.persona_agent import CallState
from src.analysis import CallSignals

logger = logging.getLogger("posivex.session")


class CallLifecycle:
    def __init__(
        self,
        *,
        session: Any,
        call_state: CallState,
        signals: CallSignals,
        tasks: BackgroundTasks,
        closing_grace_secs: float,
        max_duration_secs: float,
    ) -> None:
        self._session = session
        self._call_state = call_state
        self._signals = signals
        self._tasks = tasks
        self._closing_grace_secs = closing_grace_secs
        self._max_duration_secs = max_duration_secs
        self.reason = ""  # why the call ended; empty while it is running
        self._closing_task: asyncio.Task[Any] | None = None
        self._guard_task: asyncio.Task[None] | None = None

    # ---- ending --------------------------------------------------------------------------------
    def end(self, reason: str, *, drain: bool = True) -> None:
        """Shut the session down. The first reason given is the one that is reported."""
        if not self.reason:
            self.reason = reason
        self._session.shutdown(drain=drain)

    def session_closed(self, ev: Any) -> None:
        """The session closed by itself (the callee hung up, the room went away): keep its reason if none is set."""
        if not self.reason:
            reason = getattr(ev, "reason", None)
            self.reason = str(getattr(reason, "value", reason) or "session_closed")

    # ---- safety net for a goodbye without end_call ---------------------------------------------
    def watch_for_closing(self) -> None:
        self.cancel_closing_watch()
        self._closing_task = self._tasks.spawn(self._hang_up_after_goodbye())

    def cancel_closing_watch(self) -> None:
        task = self._closing_task
        if task is not None and not task.done():
            task.cancel()
        self._closing_task = None

    async def _hang_up_after_goodbye(self) -> None:
        await asyncio.sleep(self._closing_grace_secs)
        if self.reason:
            return  # end_call (or another path) is already closing the call; nothing to rescue
        if self._session.user_state == "speaking" or self._session.agent_state == "speaking":
            return
        logger.info("Goodbye was spoken without end_call; ending the call.")
        self._call_state.end_reason = self._call_state.end_reason or "closing_line_detected"
        self.end("closing_line_detected")

    # ---- hard cap on call length (the SIP max_call_duration is only a backstop) ----------------
    def start_duration_guard(self) -> None:
        self._guard_task = asyncio.create_task(self._duration_guard())

    async def _duration_guard(self) -> None:
        await asyncio.sleep(self._max_duration_secs)
        logger.warning("Max call duration (%ss) reached; ending call.", self._max_duration_secs)
        self._signals.notes.append("The call was cut off at the maximum call duration.")
        self.end("max_duration", drain=False)

    def stop(self) -> None:
        """Cancel the duration guard and the goodbye watch (the call is being finalized)."""
        if self._guard_task is not None:
            self._guard_task.cancel()
        self.cancel_closing_watch()
