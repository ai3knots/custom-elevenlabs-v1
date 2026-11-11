"""Waiting on the phone: for the callee to pick up, and for them to be quiet before the opener."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

# How often the SIP call status is read while the phone rings.
ANSWER_POLL_SECS = 0.5

# How often the callee's speaking state is read while waiting for a quiet moment.
QUIET_POLL_SECS = 0.05


async def wait_for_answer(callee: Any, timeout: float, is_gone: Callable[[], bool] = lambda: False) -> str:
    """Wait for sip.callStatus to become 'active'.

    Returns 'active', 'hangup', 'disconnected' (the callee left the room while still ringing, for example
    by declining) or 'timeout'. A declined call never publishes a final status, so without `is_gone` this
    kept polling for the whole ring timeout and held the job open after the call was over.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = callee.attributes.get("sip.callStatus")
        if status == "active":
            return "active"
        if status == "hangup":
            return "hangup"
        if is_gone():
            return "disconnected"
        await asyncio.sleep(ANSWER_POLL_SECS)
    return "timeout"


async def wait_for_caller_quiet(session: Any, *, quiet_secs: float = 0.4, max_wait: float = 3.0) -> bool:
    """Wait until the caller has been silent for `quiet_secs` (True), or `max_wait` passes (False)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    quiet_since: float | None = None
    while loop.time() < deadline:
        if session.user_state == "speaking":
            quiet_since = None
        else:
            now = loop.time()
            quiet_since = quiet_since if quiet_since is not None else now
            if now - quiet_since >= quiet_secs:
                return True
        await asyncio.sleep(QUIET_POLL_SECS)
    return False
