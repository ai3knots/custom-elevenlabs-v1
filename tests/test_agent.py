"""Unit tests for PersonaAgent: the three tools, the reply gate and the session helpers."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from livekit.agents.llm import ToolError

from src.agent.persona_agent import DNC_APOLOGY_SPEECH, CallState, PersonaAgent
from src.agent.session_handler import parse_call_request_from_room


def make_agent(**kwargs):
    state = kwargs.pop("call_state", None) or CallState()
    ended: list[tuple[str, bool]] = []
    agent = PersonaAgent(
        instructions="You are Amelia.",
        call_state=state,
        on_end_call=lambda reason, drain: ended.append((reason, drain)),
        call_id="call_test_123",
        **kwargs,
    )
    return agent, state, ended


def make_context():
    """A RunContext stand-in whose speech handle lets the test fire the done callback."""
    handle = MagicMock()
    callbacks = []
    handle.add_done_callback.side_effect = callbacks.append
    session = MagicMock()
    spoken = MagicMock()
    spoken.wait_for_playout = AsyncMock()
    session.say.return_value = spoken
    context = MagicMock()
    context.speech_handle = handle
    context.session = session
    return context, callbacks, session


def test_agent_exposes_exactly_the_three_tools():
    agent, _, _ = make_agent()
    assert len(agent.tools) == 3
    assert agent.instructions == "You are Amelia."


@pytest.mark.asyncio
async def test_end_call_hangs_up_after_the_closing_line_finishes():
    agent, state, ended = make_agent()
    agent._generation_text = "Perfect, you're all set. Thanks, talk soon."
    context, callbacks, _ = make_context()

    assert await agent.end_call("conversation_complete", context) is None
    assert ended == []  # not yet: the closing line is still playing
    assert state.end_reason == "conversation_complete"
    callbacks[0](context.speech_handle)
    assert ended == [("conversation_complete", True)]


@pytest.mark.asyncio
async def test_end_call_without_a_spoken_line_is_refused_once_then_allowed():
    agent, _, ended = make_agent()
    agent._generation_text = ""
    context, callbacks, _ = make_context()

    with pytest.raises(ToolError, match="closing line"):
        await agent.end_call("conversation_complete", context)
    assert callbacks == []

    await agent.end_call("conversation_complete", context)  # second attempt goes through
    callbacks[0](context.speech_handle)
    assert ended == [("conversation_complete", True)]


@pytest.mark.asyncio
async def test_end_call_may_be_silent_for_silence_reason():
    agent, _, ended = make_agent()
    agent._generation_text = ""
    context, callbacks, _ = make_context()
    await agent.end_call("silence", context)
    callbacks[0](context.speech_handle)
    assert ended == [("silence", True)]


@pytest.mark.asyncio
async def test_do_not_call_speaks_the_apology_when_the_model_did_not():
    agent, state, ended = make_agent()
    agent._generation_text = ""
    context, callbacks, session = make_context()

    await agent.end_call("do_not_call", context)
    session.say.assert_called_once_with(DNC_APOLOGY_SPEECH, allow_interruptions=False)
    assert state.do_not_call is True
    callbacks[0](context.speech_handle)
    assert ended == [("do_not_call", True)]


@pytest.mark.asyncio
async def test_do_not_call_does_not_repeat_an_apology_the_model_already_said():
    agent, _, _ = make_agent()
    agent._generation_text = DNC_APOLOGY_SPEECH
    context, _, session = make_context()
    await agent.end_call("do_not_call", context)
    session.say.assert_not_called()


@pytest.mark.asyncio
async def test_voicemail_detection_ends_the_call_without_draining():
    agent, state, ended = make_agent()
    assert await agent.voicemail_detection("voicemail: leave a message") is None
    assert state.voicemail_detected is True
    assert ended == [("voicemail_detected", False)]


@pytest.mark.asyncio
async def test_amd_human_blocks_voicemail_tool_on_a_recording_notice():
    state = CallState(amd_category="human", user_turns=1, recent_user_text=["This call is being recorded."])
    agent, _, ended = make_agent(call_state=state)
    with pytest.raises(ToolError, match="live person"):
        await agent.voicemail_detection("recorded message")
    assert ended == [] and state.voicemail_detected is False


@pytest.mark.asyncio
async def test_voicemail_guard_allows_real_cues_uncertain_verdicts_and_later_recordings():
    cue = CallState(amd_category="human", user_turns=1, recent_user_text=["Please leave a message after the tone."])
    agent, _, ended = make_agent(call_state=cue)
    await agent.voicemail_detection("voicemail greeting")
    assert ended == [("voicemail_detected", False)]

    uncertain = CallState(amd_category="uncertain", user_turns=1, recent_user_text=["This call is being recorded."])
    agent, _, ended = make_agent(call_state=uncertain)
    await agent.voicemail_detection("voicemail greeting")
    assert ended

    # Well into the call (a recording after "let me get the owner") the guard no longer applies.
    later = CallState(amd_category="human", user_turns=8, recent_user_text=["please hold"])
    agent, _, ended = make_agent(call_state=later)
    await agent.voicemail_detection("recorded hold message")
    assert ended


@pytest.mark.asyncio
async def test_skip_turn_registers_a_hold_without_a_reply():
    agent, state, _ = make_agent(hold_duration_seconds=12.0)
    assert state.is_holding is False
    assert await agent.skip_turn("checking with owner") is None
    assert state.is_holding is True
    state.release_hold()
    assert state.is_holding is False


@pytest.mark.asyncio
async def test_replies_are_held_until_the_opening_line_then_released():
    from livekit.agents import StopResponse
    from livekit.agents.llm import ChatContext, ChatMessage

    held: list[str] = []
    agent, state, _ = make_agent(hold_replies=True, on_gated_turn=held.append)

    message = ChatMessage(role="user", content=["This call is being recorded."])
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(ChatContext(), message)
    assert held == ["This call is being recorded."]

    agent.open_reply_gate()
    await agent.on_user_turn_completed(ChatContext(), ChatMessage(role="user", content=["Hello?"]))
    assert held == ["This call is being recorded."]  # not held any more
    assert state.recent_user_text[-1] == "Hello?"
    assert state.user_turns == 2


def test_parse_call_request_from_room():
    """Verify parsing CallRequest from room metadata JSON."""
    raw_json = (
        '{"call_id": "call_abc", "phone": "+14045551234", "persona": "michael", '
        '"language": "en", "dynamic_variables": {"business_name": "Tacos El Rey"}}'
    )
    req = parse_call_request_from_room(raw_json)
    assert req.call_id == "call_abc"
    assert req.phone == "+14045551234"
    assert req.persona == "michael"
    assert req.dynamic_variables.business_name == "Tacos El Rey"

    # Missing or invalid metadata is refused instead of dialing a placeholder number
    with pytest.raises(ValueError):
        parse_call_request_from_room("")
    with pytest.raises(ValueError):
        parse_call_request_from_room("not-valid-json")


def test_parse_call_request_dev_fallback(monkeypatch):
    """ALLOW_DEFAULT_CALL_REQUEST restores the placeholder request for local development."""
    monkeypatch.setenv("ALLOW_DEFAULT_CALL_REQUEST", "true")
    assert parse_call_request_from_room("").phone == "+14045550000"
    assert parse_call_request_from_room("not-valid-json").phone == "+14045550000"


def test_prewarmed_vad_is_reused_only_when_settings_match():
    from types import SimpleNamespace

    from src.agent.session_handler import _prewarmed_vad
    from src.config import Settings

    settings = Settings()
    sentinel = object()
    ctx = SimpleNamespace(proc=SimpleNamespace(userdata={"vad": (settings.vad.model_dump(), sentinel)}))
    assert _prewarmed_vad(ctx, settings) is sentinel

    changed = Settings()
    changed.vad.min_silence_duration = 0.9
    assert _prewarmed_vad(ctx, changed) is None
    assert _prewarmed_vad(SimpleNamespace(proc=SimpleNamespace(userdata={})), settings) is None


@pytest.mark.asyncio
async def test_wait_for_caller_quiet_waits_for_speech_to_end():
    import asyncio
    from types import SimpleNamespace

    from src.agent.session_handler import _wait_for_caller_quiet

    session = SimpleNamespace(user_state="speaking")

    async def stop_speaking() -> None:
        await asyncio.sleep(0.2)
        session.user_state = "listening"

    task = asyncio.create_task(stop_speaking())
    started = asyncio.get_running_loop().time()
    assert await _wait_for_caller_quiet(session, quiet_secs=0.1, max_wait=2.0) is True
    assert asyncio.get_running_loop().time() - started >= 0.25
    await task


@pytest.mark.asyncio
async def test_wait_for_caller_quiet_gives_up_after_max_wait():
    from types import SimpleNamespace

    from src.agent.session_handler import _wait_for_caller_quiet

    session = SimpleNamespace(user_state="speaking")
    assert await _wait_for_caller_quiet(session, quiet_secs=0.1, max_wait=0.2) is False


def test_closing_lines_are_recognised_only_at_the_end_of_a_message():
    from src.agent.persona_agent import is_closing_line

    assert is_closing_line("Perfect, you're all set. Thanks, talk soon.")
    assert is_closing_line("Got it. Perfect, I've got everything noted for the owner. Thanks for your help!")
    assert is_closing_line("Understood, my apologies. I'll make sure we don't call this number again. Take care.")
    assert is_closing_line("Totally understand. Thanks for your time, and have a great day.")
    assert not is_closing_line("Happy to take care of that for you. What day works best?")
    assert not is_closing_line("How are you currently handling your card payments?")
    assert not is_closing_line("")
