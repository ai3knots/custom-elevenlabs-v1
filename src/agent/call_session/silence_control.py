"""A quiet callee: one "Hello?" then hang up, the no-speech watchdog, and a caller turn that never ends."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

from src.agent.call_session.lifecycle import CallLifecycle
from src.agent.call_session.tasks import BackgroundTasks
from src.agent.call_session.timeline import CallTimeline
from src.agent.persona_agent import CallState
from src.agent.silence import nudge_and_rearm
from src.config import ConversationConfig

logger = logging.getLogger("posivex.session")


class SilenceControl:
    """Like the reference ElevenLabs agent: silence ends the call, except while the caller asked us to hold
    (skip_turn). Before the caller has said anything, the agent gets one casual "Hello?" first.

    The watchdog uses the time words were last heard, not VAD, so hold music or a looping recording (VAD
    "speaking", no words) still times out.
    """

    def __init__(
        self,
        *,
        session: Any,
        call_state: CallState,
        lifecycle: CallLifecycle,
        timeline: CallTimeline,
        tasks: BackgroundTasks,
        config: ConversationConfig,
        opener_spoken: Callable[[], bool],
        watch: Callable[..., Coroutine[Any, Any, None]],
        nudge: Callable[[Any], Coroutine[Any, Any, None]] = nudge_and_rearm,
    ) -> None:
        self._session = session
        self._call_state = call_state
        self._lifecycle = lifecycle
        self._timeline = timeline
        self._tasks = tasks
        self._config = config
        self._opener_spoken = opener_spoken
        self._watch = watch
        self._nudge = nudge
        self._hello_sent = False
        self._heard = time.monotonic()  # last time anything was heard from the callee
        self._agent_done = time.monotonic()  # last time the agent finished speaking
        self._long_turn_task: asyncio.Task[Any] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None

    # ---- what the callee and the agent are doing -----------------------------------------------
    def reset_activity(self) -> None:
        """The callee picked up: the quiet clock starts now."""
        self._heard = self._agent_done = time.monotonic()

    def note_heard(self) -> None:
        self._heard = time.monotonic()

    def on_agent_state(self, old_state: str, new_state: str) -> None:
        if old_state == "speaking" and new_state != "speaking":
            self._agent_done = time.monotonic()

    def on_user_state(self, new_state: str) -> None:
        previous = self._long_turn_task
        if previous is not None and not previous.done():
            previous.cancel()
        self._long_turn_task = None
        limit = self._config.max_user_speech_seconds
        if new_state == "speaking" and limit > 0:
            self._long_turn_task = self._tasks.spawn(self._force_long_turn_answer(limit))
        if new_state == "away":
            self._tasks.spawn(self._handle_away())
        elif new_state == "speaking":
            self._call_state.release_hold()
            self._lifecycle.cancel_closing_watch()

    # ---- silence -------------------------------------------------------------------------------
    async def _handle_away(self) -> None:
        if not self._opener_spoken() or self._call_state.is_holding:
            # Still ringing / detecting an answering machine, or the caller asked us to hold.
            self._session.reset_away_timer()
            return
        if self._call_state.user_turns == 0 and not self._hello_sent:
            self._hello_sent = True
            self._timeline.mark("silence_nudge")
            await self._nudge(self._session)
            return
        logger.info("Caller silent for %.0fs; ending the call.", self._config.silence_end_call_seconds)
        self._call_state.end_reason = self._call_state.end_reason or "silence"
        self._lifecycle.end("silence_timeout", drain=False)

    async def _force_long_turn_answer(self, limit: float) -> None:
        """Answer when VAD reports one uninterrupted caller turn for `limit` seconds.

        Background noise or off-script chatter can keep VAD in "speaking" for 10+ seconds while the
        transcript is complete, leaving the caller in silence. Committing the turn makes the agent reply
        to what STT has heard so far.
        """
        await asyncio.sleep(limit)
        if self._session.user_state != "speaking" or self._session.agent_state not in ("listening", "idle"):
            return
        logger.warning("Caller turn open for %.0fs; committing it so the agent can answer.", limit)
        try:
            await self._session.commit_user_turn()
        except Exception:
            logger.exception("Could not commit the long caller turn.")

    # ---- the no-speech watchdog ----------------------------------------------------------------
    def start_watchdog(self) -> None:
        if self._config.no_speech_watchdog_seconds > 0:
            self._watchdog_task = asyncio.create_task(self._run_watchdog())

    def stop_watchdog(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()

    def _run_watchdog(self) -> Coroutine[Any, Any, None]:
        return self._watch(
            limit=self._config.no_speech_watchdog_seconds,
            last_activity=lambda: max(self._heard, self._agent_done),
            is_paused=lambda: self._call_state.is_holding or self._session.agent_state in ("speaking", "thinking"),
            is_over=lambda: bool(self._lifecycle.reason),
            on_timeout=self._on_no_speech,
        )

    def _on_no_speech(self, quiet: float) -> None:
        logger.info("No words heard from the callee for %.0fs; ending the call.", quiet)
        self._timeline.mark("no_speech_watchdog", quiet_secs=round(quiet, 1))
        self._call_state.end_reason = self._call_state.end_reason or "silence"
        self._lifecycle.end("no_speech_timeout", drain=False)
