"""Characterization tests for `run_agent_session`: they pin what the live call flow does today.

They run the real flow with a fake LiveKit job, session and recorder (tests/support/fake_call.py), so a refactor
of `src/agent/session_handler.py` can be checked against them. Nothing here asserts what *should* happen; each
test records what happens now.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.agent import session_handler
from src.agent.greeting_detector import GreetingVerdict
from src.agent.opener import OVERLAP_REPLY_INSTRUCTIONS
from src.agent.silence import NUDGE_INSTRUCTIONS
from src.analysis import OutcomeResult
from src.config import Settings
from tests.support.fake_call import (
    AGENT_ID,
    CALL_ID,
    PHONE,
    FakeClassifier,
    FakeJobContext,
    FakeRecorder,
    FakeSession,
    browser_callee,
    call_metadata,
    settle,
    sip_callee,
    wait_until,
)
from tests.support.mock_providers import MockLLM, MockSTT, MockTTS, MockVAD

FIRST_MESSAGE = "Hi, this is Amelia from Clover Posivex. Am I speaking with the owner of Apex Pizza?"
DESTINATION = "http://localhost:3000/api/webhooks/elevenlabs"


def make_settings(*, detector: bool = False, **conversation: Any) -> Settings:
    return Settings(
        gemini_api_key="test-key" if detector else "",
        recording={"enabled": False},  # type: ignore[arg-type]
        conversation={  # type: ignore[arg-type]
            "opener_delay_seconds": 0.0,
            "opener_quiet_wait_seconds": 0.0,
            "no_speech_watchdog_seconds": 0.0,
            "machine_detection_enabled": detector,
            **conversation,
        },
    )


@dataclass
class Harness:
    ctx: FakeJobContext
    recorder: FakeRecorder = field(default_factory=FakeRecorder)
    sessions: list[FakeSession] = field(default_factory=list)
    settings: Settings = field(default_factory=make_settings)
    gate: asyncio.Event | None = None  # when set, the opener keeps "playing" until the test opens it

    @property
    def session(self) -> FakeSession:
        return self.sessions[0]

    async def run(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(
            session_handler.run_agent_session(self.ctx, settings=self.settings, recorder=self.recorder),  # type: ignore[arg-type]
            timeout,
        )
        await settle()  # timing events are stored by small background tasks

    def start(self) -> asyncio.Task[None]:
        return asyncio.get_running_loop().create_task(self.run())

    async def finish(self) -> dict[str, Any]:
        """Close the session (as the framework would) and return what was reported at the end of the call."""
        if not self.ctx.shutdown_finished.is_set():
            if not self.ctx.shutdown_reasons:
                self.session.shutdown(drain=True)
            await asyncio.wait_for(self.ctx.shutdown_finished.wait(), 5.0)
        assert self.recorder.finalized is not None
        return self.recorder.finalized


@pytest.fixture
def make_harness(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Harness]:
    monkeypatch.delenv("ALLOW_DEFAULT_CALL_REQUEST", raising=False)

    async def stub_classifier(transcript: Any, signals: Any, **_: Any) -> OutcomeResult:
        return OutcomeResult("callback_set", "Stub rationale.", "Stub summary.")

    monkeypatch.setattr(session_handler, "classify_transcript", stub_classifier)
    monkeypatch.setattr(session_handler, "get_stt", lambda *a, **k: MockSTT())
    monkeypatch.setattr(session_handler, "get_tts", lambda *a, **k: MockTTS())
    monkeypatch.setattr(session_handler, "get_vad", lambda *a, **k: MockVAD())
    monkeypatch.setattr(session_handler, "get_llm", lambda *a, **k: MockLLM())
    monkeypatch.setattr(session_handler, "GeminiGreetingClassifier", FakeClassifier)
    FakeClassifier.verdict = GreetingVerdict("human", 0.95, 0.1)
    FakeClassifier.asked = []

    def build(
        *,
        metadata: str | None = None,
        callee: Any = None,
        settings: Settings | None = None,
        no_metadata: bool = False,
        hold_opener: bool = False,
        never_joins: bool = False,
    ) -> Harness:
        harness = Harness(
            ctx=FakeJobContext(None if no_metadata else (metadata or call_metadata()), callee, never_joins=never_joins)
        )
        if settings is not None:
            harness.settings = settings
        if hold_opener:
            harness.gate = asyncio.Event()

        def new_session(**kwargs: Any) -> FakeSession:
            session = FakeSession(**kwargs)
            session.playout_gate = harness.gate
            harness.sessions.append(session)
            return session

        monkeypatch.setattr(session_handler, "AgentSession", new_session)
        return harness

    return build


# ---- start-up and metadata ---------------------------------------------------------------------------
async def test_missing_dispatch_metadata_refuses_to_start(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(no_metadata=True)
    await harness.run()
    assert harness.ctx.shutdown_reasons == ["invalid call metadata"]
    assert harness.sessions == []
    assert harness.recorder.events == []
    assert harness.recorder.finalized is None


async def test_malformed_dispatch_metadata_refuses_to_start(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(metadata="not json")
    await harness.run()
    assert harness.ctx.shutdown_reasons == ["invalid call metadata"]
    assert harness.sessions == []


def test_metadata_sources_in_priority_order() -> None:
    from types import SimpleNamespace

    resolve = session_handler._resolve_raw_metadata
    job_first = SimpleNamespace(
        job=SimpleNamespace(metadata="job", room=SimpleNamespace(metadata="job-room")),
        room=SimpleNamespace(metadata="room", remote_participants={}),
    )
    assert resolve(job_first) == "job"  # type: ignore[arg-type]
    room_next = SimpleNamespace(
        job=SimpleNamespace(metadata=None, room=SimpleNamespace(metadata="job-room")),
        room=SimpleNamespace(metadata="room", remote_participants={}),
    )
    assert resolve(room_next) == "room"  # type: ignore[arg-type]
    participant = SimpleNamespace(metadata="participant")
    last = SimpleNamespace(
        job=SimpleNamespace(metadata=None, room=SimpleNamespace(metadata=None)),
        room=SimpleNamespace(metadata=None, remote_participants={"p": participant}),
    )
    assert resolve(last) == "participant"  # type: ignore[arg-type]


def test_default_call_request_is_only_allowed_by_the_dev_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_DEFAULT_CALL_REQUEST", raising=False)
    with pytest.raises(ValueError):
        session_handler.parse_call_request_from_room(None)
    monkeypatch.setenv("ALLOW_DEFAULT_CALL_REQUEST", "true")
    assert session_handler.parse_call_request_from_room(None).phone == "+14045550000"
    assert session_handler.parse_call_request_from_room("not json").phone == "+14045550000"


# ---- answered call: opener, events, report -----------------------------------------------------------
async def test_answered_call_speaks_the_scripted_opener_once_after_pickup(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(callee=browser_callee(), settings=make_settings())
    await harness.run()
    session = harness.session

    assert session.said == [(FIRST_MESSAGE, {"allow_interruptions": False})]
    assert session.kwargs["user_away_timeout"] == 15.0
    assert session.room_options.delete_room_on_close is True
    assert harness.ctx.log_context_fields == {"call_id": CALL_ID, "room": f"call_{CALL_ID}"}  # on every log line
    assert session.agent._hold_replies is False  # the model may answer from now on
    assert session.away_resets == 1  # the silence timer restarts when the opener ends
    assert harness.recorder.call["phone"] == PHONE
    assert harness.recorder.call["persona"] == "amelia"
    assert harness.recorder.call["lead_id"] == "lead-1"
    assert harness.recorder.call["metadata_json"]["dynamic_variables"]["business_name"] == "Apex Pizza"

    names = harness.recorder.event_names()
    assert [n for n in names if n.startswith("timing_")] == ["timing_pickup", "timing_opener_started", "timing_opener_finished"]
    assert harness.recorder.payload("timing_pickup")["sip"] is False
    assert harness.recorder.payload("timing_opener_started")["waited_for_quiet"] is True

    start = harness.ctx.local.published[0]
    assert start["type"] == "session_init" and start["call_id"] == CALL_ID and start["persona"] == "amelia"
    assert start["topic"] == "posivex_telemetry"


async def test_call_report_when_nobody_spoke(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(callee=browser_callee())
    await harness.run()
    report = await harness.finish()

    assert report["call_id"] == CALL_ID
    assert report["call_outcome"] == "hangup_no_speak"
    assert report["outcome_rationale"] == "Call ended without the callee speaking."
    assert report["termination_reason"] == "session_closed"
    assert report["agent_id"] == AGENT_ID
    assert report["agent_name"] == "Posivex LiveKit - Amelia"
    assert report["destination_url"] == DESTINATION
    assert report["duration_secs"] >= 0
    assert report["charging_details"].keys() == {
        "call_charge", "llm_charge", "tts_charge", "stt_charge", "transport_charge"
    }
    assert [(u["provider"], u["model"], u["metric_type"]) for u in harness.recorder.usage] == [
        ("livekit", "sip_webrtc", "audio_seconds")
    ]
    call_end = harness.recorder.payload("timing_call_end")
    assert call_end["reason"] == "session_closed" and call_end["outcome"] == "hangup_no_speak"
    assert call_end.keys() >= {"ring_secs", "talk_secs", "since_pickup_secs"}


async def test_conversation_is_stored_and_classified(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(callee=browser_callee())
    await harness.run()
    session = harness.session
    session.item("assistant", FIRST_MESSAGE)
    session.item("user", "Yes, speaking.")
    session.item("assistant", "Great, thanks for picking up.", interrupted=True)
    session.item("assistant", "")  # empty items are not stored
    report = await harness.finish()

    assert [(t["role"], t["message"], t["interrupted"]) for t in harness.recorder.transcript] == [
        ("agent", FIRST_MESSAGE, False),
        ("user", "Yes, speaking.", False),
        ("agent", "Great, thanks for picking up.", True),
    ]
    assert all(t["time_in_call_secs"] >= 0 for t in harness.recorder.transcript)
    assert report["call_outcome"] == "callback_set"
    assert report["outcome_rationale"] == "Stub rationale."
    assert report["transcript_summary"] == "Stub summary."


async def test_a_goodbye_without_end_call_hangs_up_after_the_grace_period(
    make_harness: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_handler, "CLOSING_GRACE_SECS", 0.05)
    harness = make_harness(callee=browser_callee())
    await harness.run()
    harness.session.item("assistant", "Thanks, talk soon.")
    await wait_until(lambda: harness.session.shutdowns == [True])
    report = await harness.finish()
    assert report["termination_reason"] == "closing_line_detected"


async def test_caller_speech_cancels_the_goodbye_hangup(
    make_harness: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_handler, "CLOSING_GRACE_SECS", 0.2)
    harness = make_harness(callee=browser_callee())
    await harness.run()
    harness.session.item("assistant", "Thanks, talk soon.")
    harness.session.user_state_changed("speaking")
    await asyncio.sleep(0.4)
    assert harness.session.shutdowns == []


# ---- phone calls that are not answered ----------------------------------------------------------------
async def test_sip_call_answered_is_timed_from_pickup(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(callee=sip_callee("active"))
    await harness.run()
    assert harness.session.participants == [f"phone_{PHONE}"]  # audio is focused on the callee
    assert harness.session.said and harness.recorder.payload("timing_pickup")["sip"] is True


async def test_declined_phone_call_is_reported_as_no_answer_without_an_opener(
    make_harness: Callable[..., Harness],
) -> None:
    harness = make_harness(callee=sip_callee("hangup"))
    await harness.run()
    assert harness.session.said == []
    assert harness.session.shutdowns == [False]
    report = await harness.finish()
    assert report["call_outcome"] == "no_answer"
    assert report["outcome_rationale"] == "Call not answered (hangup)."
    assert report["termination_reason"] == "no_answer"
    assert report["duration_secs"] == 0.0  # ringing is not call time
    assert "timing_pickup" not in harness.recorder.event_names()


async def test_no_participant_is_reported_as_no_answer(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(callee=None)
    await harness.run()
    report = await harness.finish()
    assert report["call_outcome"] == "no_answer"
    assert report["outcome_rationale"] == "No participant joined the room."
    assert report["termination_reason"] == "no_participant"
    assert harness.session.shutdowns == [False]


async def test_a_callee_who_never_joins_is_reported_as_no_answer_instead_of_waiting_for_ever(
    make_harness: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_handler, "PARTICIPANT_JOIN_TIMEOUT_SECS", 0.1)
    harness = make_harness(never_joins=True)
    await harness.run()
    report = await harness.finish()
    assert report["call_outcome"] == "no_answer"
    assert report["termination_reason"] == "no_participant"


async def test_a_phone_call_that_ends_while_still_ringing_is_labelled_no_answer(
    make_harness: Callable[..., Harness],
) -> None:
    """The session closes before the callee ever picked up (declined, busy, network refusal)."""
    harness = make_harness(callee=sip_callee("ringing"))
    task = harness.start()
    await wait_until(lambda: bool(harness.sessions) and bool(harness.session.participants))  # waiting for pickup
    harness.session.shutdown(drain=False)
    report = await harness.finish()
    task.cancel()
    assert report["call_outcome"] == "no_answer"
    assert report["outcome_rationale"] == "The callee did not answer (the call was rejected or dropped while ringing)."
    assert report["duration_secs"] == 0.0
    assert "timing_pickup" not in harness.recorder.event_names()


async def test_ring_time_is_measured_to_the_pickup_and_talk_time_starts_there(
    make_harness: Callable[..., Harness],
) -> None:
    callee = sip_callee("ringing")
    harness = make_harness(callee=callee)
    task = harness.start()
    await wait_until(lambda: bool(harness.sessions) and bool(harness.session.participants))
    await asyncio.sleep(0.3)
    callee.attributes["sip.callStatus"] = "active"
    await asyncio.wait_for(task, 5.0)
    await settle()
    report = await harness.finish()
    pickup = harness.recorder.payload("timing_pickup")
    assert pickup["ring_secs"] >= 0.3 and pickup["sip"] is True
    assert pickup["since_pickup_secs"] == 0.0  # the call clock starts at the pickup, not at the dial
    end = harness.recorder.payload("timing_call_end")
    assert end["ring_secs"] == pickup["ring_secs"] and end["talk_secs"] < end["ring_secs"]
    assert report["duration_secs"] < pickup["ring_secs"]


async def test_the_call_is_finalized_once_even_if_the_shutdown_callback_runs_again(
    make_harness: Callable[..., Harness],
) -> None:
    harness = make_harness(callee=browser_callee())
    await harness.run()
    await harness.finish()
    await harness.ctx._callbacks[0]()  # a second shutdown notification
    assert harness.recorder.finalize_count == 1


# ---- voicemail and phone-menu detection ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("category", "outcome", "reason"),
    [("voicemail", "voicemail", "voicemail_detected"), ("menu", "automated_menu", "automated_menu_detected")],
)
async def test_confident_machine_verdict_hangs_up_and_labels_the_call(
    make_harness: Callable[..., Harness], category: str, outcome: str, reason: str
) -> None:
    FakeClassifier.verdict = GreetingVerdict(category, 0.95, 0.2)
    harness = make_harness(callee=browser_callee(), settings=make_settings(detector=True))
    await harness.run()
    harness.session.hear("You have reached the mailbox of Apex Pizza please leave a message after the tone")
    await wait_until(lambda: harness.session.shutdowns == [False])
    report = await harness.finish()

    assert harness.session.interrupts == [True]  # the uninterruptible opener is cut off by force
    assert report["call_outcome"] == outcome
    assert report["termination_reason"] == reason
    assert report["outcome_rationale"].startswith(f"Greeting check: {category} (0.95).")
    assert "timing_machine_detected" in harness.recorder.event_names()
    assert harness.recorder.payload("timing_machine_detected")["category"] == category


async def test_low_confidence_or_human_verdict_keeps_the_call_going(make_harness: Callable[..., Harness]) -> None:
    FakeClassifier.verdict = GreetingVerdict("voicemail", 0.5, 0.2)
    harness = make_harness(callee=browser_callee(), settings=make_settings(detector=True))
    await harness.run()
    harness.session.hear("Hello this is the front desk how can I help")
    await wait_until(lambda: "timing_greeting_check" in harness.recorder.event_names())
    await settle()
    assert harness.session.shutdowns == [] and harness.session.interrupts == []


async def test_machine_heard_during_the_opener_cuts_it_off_before_it_finishes(
    make_harness: Callable[..., Harness],
) -> None:
    FakeClassifier.verdict = GreetingVerdict("voicemail", 0.97, 0.2)
    harness = make_harness(callee=browser_callee(), settings=make_settings(detector=True), hold_opener=True)
    task = harness.start()
    await wait_until(lambda: bool(harness.sessions) and bool(harness.session.said))  # the opener is playing
    harness.session.hear("the person you are calling is not available please leave a message")
    await wait_until(lambda: harness.session.shutdowns == [False])
    assert harness.gate is not None
    harness.gate.set()  # the real session stops playing when interrupted
    await task
    await settle()
    report = await harness.finish()
    assert harness.session.interrupts == [True]
    assert report["call_outcome"] == "voicemail"
    assert "timing_opener_finished" not in harness.recorder.event_names()


async def test_greeting_check_stops_once_the_conversation_is_under_way(make_harness: Callable[..., Harness]) -> None:
    """After three caller turns the call counts as a real conversation and the check stops asking."""
    FakeClassifier.verdict = GreetingVerdict("voicemail", 0.99, 0.2)
    harness = make_harness(callee=browser_callee(), settings=make_settings(detector=True))
    await harness.run()
    harness.session.agent.call_state.user_turns = 3
    harness.session.hear("You have reached the mailbox of Apex Pizza please leave a message after the tone")
    await settle()
    await asyncio.sleep(0.05)
    assert FakeClassifier.asked == [] and harness.session.shutdowns == []


async def test_greeting_check_is_off_without_a_gemini_key(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(callee=browser_callee(), settings=make_settings(detector=False))
    await harness.run()
    harness.session.hear("You have reached the voicemail of Apex Pizza leave a message")
    await settle()
    assert FakeClassifier.asked == [] and harness.session.shutdowns == []


# ---- answering over the opener ------------------------------------------------------------------------
async def _run_with_speech_over_opener(harness: Harness, said: list[str]) -> None:
    task = harness.start()
    await wait_until(lambda: bool(harness.sessions) and bool(harness.session.said))  # the opener is playing
    for text in said:
        harness.session.hear(text)
    await settle()
    assert harness.gate is not None
    harness.gate.set()
    await task
    await settle()


async def test_a_real_answer_given_over_the_opener_gets_a_reply(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(callee=browser_callee(), hold_opener=True)
    await _run_with_speech_over_opener(harness, ["Yes, this is the owner."])
    assert harness.session.replies == [
        {"user_input": "Yes, this is the owner.", "instructions": OVERLAP_REPLY_INSTRUCTIONS}
    ]
    assert "timing_replying_to_overlap" in harness.recorder.event_names()


@pytest.mark.parametrize("speech", ["Hello?", "Hi, hello", FIRST_MESSAGE])
async def test_a_bare_greeting_or_an_echo_over_the_opener_gets_no_reply(
    make_harness: Callable[..., Harness], speech: str
) -> None:
    harness = make_harness(callee=browser_callee(), hold_opener=True)
    await _run_with_speech_over_opener(harness, [speech])
    assert harness.session.replies == []


async def test_overlap_reply_waits_for_the_greeting_check_to_say_human(make_harness: Callable[..., Harness]) -> None:
    """With the check on, an answer given over the opener is replied to only once the check said 'human'."""
    FakeClassifier.verdict = GreetingVerdict("human", 0.95, 0.2)
    harness = make_harness(callee=browser_callee(), settings=make_settings(detector=True), hold_opener=True)
    await _run_with_speech_over_opener(harness, ["Yes this is the owner of the shop"])
    assert harness.session.replies == [
        {"user_input": "Yes this is the owner of the shop", "instructions": OVERLAP_REPLY_INSTRUCTIONS}
    ]


async def test_no_reply_is_made_to_words_over_the_opener_while_the_greeting_check_is_unsure(
    make_harness: Callable[..., Harness],
) -> None:
    """A voicemail greeting must never get a reply: without a confident 'human', words over the opener stay unanswered."""
    FakeClassifier.verdict = GreetingVerdict("unclear", 0.3, 0.2)
    harness = make_harness(callee=browser_callee(), settings=make_settings(detector=True), hold_opener=True)
    await _run_with_speech_over_opener(harness, ["Yes this is the owner of the shop"])
    assert harness.session.replies == []


# ---- silence ------------------------------------------------------------------------------------------
async def test_silence_gets_one_hello_then_ends_the_call(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(callee=browser_callee())
    await harness.run()
    session = harness.session
    resets = session.away_resets

    session.user_state_changed("away")
    await wait_until(lambda: len(session.replies) == 1)
    await settle()
    assert session.replies == [{"instructions": NUDGE_INSTRUCTIONS}]
    assert session.away_resets == resets + 1  # the nudge restarts the silence timer
    assert session.shutdowns == []

    session.user_state_changed("away")
    await wait_until(lambda: session.shutdowns == [False])
    report = await harness.finish()
    assert report["termination_reason"] == "silence_timeout"
    assert report["call_outcome"] == "hangup_no_speak"
    assert "timing_silence_nudge" in harness.recorder.event_names()


async def test_silence_during_a_requested_hold_does_not_end_the_call(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(callee=browser_callee())
    await harness.run()
    session = harness.session
    session.agent.call_state.start_hold(30.0)
    resets = session.away_resets
    session.user_state_changed("away")
    await settle()
    assert session.away_resets == resets + 1
    assert session.replies == [] and session.shutdowns == []


async def test_no_speech_watchdog_ends_a_call_that_stays_wordless(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(callee=browser_callee(), settings=make_settings(no_speech_watchdog_seconds=0.1))
    original = session_handler.watch_for_silence

    async def fast_watch(**kwargs: Any) -> None:
        await original(**{**kwargs, "interval": 0.02})

    session_handler.watch_for_silence = fast_watch  # type: ignore[assignment]
    try:
        await harness.run()
        await wait_until(lambda: harness.session.shutdowns == [False])
    finally:
        session_handler.watch_for_silence = original
    report = await harness.finish()
    assert report["termination_reason"] == "no_speech_timeout"
    assert "timing_no_speech_watchdog" in harness.recorder.event_names()


def test_the_events_the_flow_stores_are_json_serialisable() -> None:
    """The remote recorder posts events as JSON; every payload the flow builds must survive that."""
    json.dumps({"since_pickup_secs": 1.5, "ring_secs": 4.2, "sip": True, "category": "human"})
