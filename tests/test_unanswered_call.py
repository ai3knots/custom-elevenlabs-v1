"""A phone call that is never answered must end promptly and be reported as no_answer."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from src.agent.session_handler import _wait_for_answer


def callee(status: str | None = None) -> Any:
    return SimpleNamespace(attributes={"sip.callStatus": status} if status else {})


async def test_answered_call_is_reported_active() -> None:
    assert await _wait_for_answer(callee("active"), 5.0) == "active"


async def test_hangup_status_is_reported() -> None:
    assert await _wait_for_answer(callee("hangup"), 5.0) == "hangup"


async def test_a_declined_call_stops_the_wait_instead_of_polling_for_the_whole_ring_timeout() -> None:
    """USER_REJECTED publishes no final status; the callee simply leaves the room while still ringing."""
    ringing = callee("ringing")
    in_room = {"yes": True}

    async def decline_after_a_moment() -> None:
        await asyncio.sleep(0.2)
        in_room["yes"] = False

    task = asyncio.create_task(decline_after_a_moment())
    started = asyncio.get_running_loop().time()
    answer = await _wait_for_answer(ringing, 75.0, is_gone=lambda: not in_room["yes"])
    await task
    assert answer == "disconnected"
    assert asyncio.get_running_loop().time() - started < 3.0  # not the 75 s ring timeout


async def test_unanswered_ringing_times_out() -> None:
    assert await _wait_for_answer(callee("ringing"), 0.6) == "timeout"
