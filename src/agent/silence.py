"""Ending calls that go quiet: the "Hello?" nudge that re-arms the timer, and the no-speech watchdog."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

NUDGE_INSTRUCTIONS = 'Say one casual "Hello?" once and nothing else. Do not repeat your introduction.'


async def nudge_and_rearm(session: Any, instructions: str = NUDGE_INSTRUCTIONS) -> None:
    """Say the "Hello?" nudge, then make LiveKit start counting the silence again.

    LiveKit starts its "user away" timer only while the user AND the agent are both "listening". After the
    nudge the user is still "away", so nothing re-armed the timer: a callee who stayed silent, or a carrier
    recording that went quiet, was never hung up on (real calls ran 68 s and 187 s). `reset_away_timer()`
    moves the user back to "listening", which starts a fresh countdown, so the next silence ends the call.
    """
    handle = session.generate_reply(instructions=instructions)
    try:
        await handle.wait_for_playout()
    finally:
        session.reset_away_timer()


async def watch_for_silence(
    *,
    limit: float,
    last_activity: Callable[[], float],
    is_paused: Callable[[], bool],
    is_over: Callable[[], bool],
    on_timeout: Callable[[float], None],
    interval: float = 2.0,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Call `on_timeout(quiet_secs)` once nothing has been heard for `limit` seconds, then return.

    Unlike the user-away timer this ignores VAD, so hold music or a looping recording (the line is "speaking"
    but no words come out) still times out. `is_paused` covers the agent talking or thinking and a hold the
    callee asked for; `is_over` ends the watch when the call has already finished.
    """
    while True:
        await sleep(interval)
        if is_over():
            return
        if is_paused():
            continue
        quiet = now() - last_activity()
        if quiet >= limit:
            on_timeout(quiet)
            return
