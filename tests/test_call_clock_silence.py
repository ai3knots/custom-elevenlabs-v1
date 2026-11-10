"""Call time from pickup, the "Hello?" nudge that re-arms the silence timer, and the no-speech watchdog."""

from __future__ import annotations

from typing import Any

import pytest

from src.agent.call_clock import CallClock
from src.agent.silence import nudge_and_rearm, watch_for_silence


# ---- call clock -----------------------------------------------------------------------------------------
def test_call_time_is_zero_while_ringing_and_counts_from_pickup() -> None:
    now = {"t": 100.0}
    clock = CallClock(started_at=100.0, now=lambda: now["t"])
    now["t"] = 112.0
    assert clock.picked_up is False
    assert clock.since_pickup() == 0.0  # ringing is not call time
    assert clock.ring_secs == pytest.approx(12.0)  # still ringing: dial to now

    assert clock.pick_up() == pytest.approx(12.0)
    now["t"] = 130.0
    assert clock.picked_up is True
    assert clock.since_pickup() == pytest.approx(18.0)
    assert clock.ring_secs == pytest.approx(12.0)  # fixed once answered


def test_a_second_pickup_does_not_restart_the_clock() -> None:
    now = {"t": 0.0}
    clock = CallClock(started_at=0.0, now=lambda: now["t"])
    now["t"] = 5.0
    clock.pick_up()
    now["t"] = 9.0
    clock.pick_up()
    assert clock.ring_secs == pytest.approx(5.0)


# ---- the nudge ------------------------------------------------------------------------------------------
class FakeSession:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[str] = []
        self.fail = fail

    def generate_reply(self, **kw: Any) -> Any:
        self.events.append("generate")
        session = self

        class Handle:
            async def wait_for_playout(self) -> None:
                session.events.append("played")
                if session.fail:
                    raise RuntimeError("interrupted")

        return Handle()

    def reset_away_timer(self) -> None:
        self.events.append("reset")


async def test_the_nudge_resets_the_away_timer_after_it_has_played() -> None:
    session = FakeSession()
    await nudge_and_rearm(session)
    assert session.events == ["generate", "played", "reset"]


async def test_the_timer_is_re_armed_even_if_the_nudge_is_cut_off() -> None:
    session = FakeSession(fail=True)
    with pytest.raises(RuntimeError):
        await nudge_and_rearm(session)
    assert session.events[-1] == "reset"


# ---- the watchdog ---------------------------------------------------------------------------------------
class Time:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, secs: float) -> None:
        self.t += secs


async def run_watchdog(**overrides: Any) -> tuple[list[float], Time]:
    clock = Time()
    fired: list[float] = []
    kwargs: dict[str, Any] = {
        "limit": 40.0,
        "last_activity": lambda: 0.0,
        "is_paused": lambda: False,
        "is_over": lambda: False,
        "on_timeout": fired.append,
        "interval": 2.0,
        "now": clock.now,
        "sleep": clock.sleep,
    }
    kwargs.update(overrides)
    await watch_for_silence(**kwargs)
    return fired, clock


async def test_it_ends_the_call_once_nothing_has_been_heard_for_the_limit() -> None:
    fired, clock = await run_watchdog()
    assert len(fired) == 1 and fired[0] >= 40.0
    assert clock.t == pytest.approx(40.0)


async def test_hearing_something_postpones_it() -> None:
    heard = {"at": 0.0}
    clock = Time()
    fired: list[float] = []

    async def sleep(secs: float) -> None:
        await clock.sleep(secs)
        if clock.t == 30.0:
            heard["at"] = 30.0  # the callee spoke

    await watch_for_silence(
        limit=40.0, last_activity=lambda: heard["at"], is_paused=lambda: False, is_over=lambda: False,
        on_timeout=fired.append, now=clock.now, sleep=sleep,
    )
    assert clock.t == pytest.approx(70.0) and len(fired) == 1


async def test_a_hold_or_the_agent_talking_pauses_it() -> None:
    state = {"paused": True}
    clock = Time()
    fired: list[float] = []

    async def sleep(secs: float) -> None:
        await clock.sleep(secs)
        if clock.t >= 60.0:
            state["paused"] = False

    await watch_for_silence(
        limit=40.0, last_activity=lambda: 0.0, is_paused=lambda: state["paused"], is_over=lambda: False,
        on_timeout=fired.append, now=clock.now, sleep=sleep,
    )
    assert len(fired) == 1 and clock.t == pytest.approx(60.0)  # the first check after the pause ends


async def test_it_stops_quietly_when_the_call_is_already_over() -> None:
    fired, clock = await run_watchdog(is_over=lambda: True)
    assert fired == [] and clock.t == pytest.approx(2.0)
