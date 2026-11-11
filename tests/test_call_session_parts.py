"""The pieces of a call that are easiest to pin on their own."""

from __future__ import annotations

from types import SimpleNamespace

from src.agent.call_session.lifecycle import CallLifecycle
from src.agent.call_session.tasks import BackgroundTasks
from src.agent.persona_agent import CallState
from src.analysis import CallSignals


def make_lifecycle() -> tuple[CallLifecycle, SimpleNamespace]:
    session = SimpleNamespace(shutdowns=[], user_state="listening", agent_state="listening")
    session.shutdown = lambda drain=True: session.shutdowns.append(drain)
    lifecycle = CallLifecycle(
        session=session, call_state=CallState(), signals=CallSignals(), tasks=BackgroundTasks(),
        closing_grace_secs=0.01, max_duration_secs=60,
    )
    return lifecycle, session


def test_the_first_reason_a_call_ends_for_is_the_one_reported() -> None:
    lifecycle, session = make_lifecycle()
    lifecycle.end("voicemail_detected", drain=False)
    lifecycle.end("silence_timeout")
    assert lifecycle.reason == "voicemail_detected"
    assert session.shutdowns == [False, True]  # the session is still told to shut down each time


def test_a_session_that_closes_by_itself_keeps_an_earlier_reason() -> None:
    lifecycle, _ = make_lifecycle()
    lifecycle.end("no_answer", drain=False)
    lifecycle.session_closed(SimpleNamespace(reason=SimpleNamespace(value="participant_disconnected")))
    assert lifecycle.reason == "no_answer"


def test_a_session_that_closes_by_itself_reports_why() -> None:
    lifecycle, _ = make_lifecycle()
    lifecycle.session_closed(SimpleNamespace(reason=SimpleNamespace(value="participant_disconnected")))
    assert lifecycle.reason == "participant_disconnected"
    other, _ = make_lifecycle()
    other.session_closed(SimpleNamespace(reason=None))
    assert other.reason == "session_closed"
