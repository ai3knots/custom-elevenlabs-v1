"""The agent's tools driven by LiveKit's own machinery: a real AgentSession in text mode (the pattern LiveKit
recommends for unit tests) with a scripted model. This is what the model's tool calls hit in a live call, including
the framework injecting the RunContext into `end_call`.

Text mode skips the voice-only turn hook (`on_user_turn_completed`), so the reply gate is covered in
tests/test_run_agent_session.py instead.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from livekit.agents import AgentSession

from src.agent.persona_agent import CallState, PersonaAgent
from tests.support.mock_providers import ScriptedLLM


@pytest.fixture
def ended() -> list[tuple[str, bool]]:
    return []


def make_agent(ended: list[tuple[str, bool]]) -> tuple[PersonaAgent, CallState]:
    state = CallState()
    return PersonaAgent(instructions="You are Amelia.", call_state=state, on_end_call=lambda r, d: ended.append((r, d))), state


async def run_turn(agent: PersonaAgent, model: ScriptedLLM) -> SimpleNamespace:
    async with model, AgentSession(llm=model) as session:
        await session.start(agent)
        result = await session.run(user_input="ok")
        return SimpleNamespace(events=[type(e).__name__ for e in result.events], session=session)


async def test_end_call_runs_through_the_framework_and_hangs_up_after_the_closing_line(
    ended: list[tuple[str, bool]],
) -> None:
    agent, state = make_agent(ended)
    model = ScriptedLLM("Thanks, talk soon.", [("end_call", '{"reason": "conversation_complete"}')])
    turn = await run_turn(agent, model)

    assert sorted(turn.events) == ["ChatMessageEvent", "FunctionCallEvent", "FunctionCallOutputEvent"]
    assert ended == [("conversation_complete", True)]  # after the speech, with draining
    assert state.end_reason == "conversation_complete"
    assert agent._generation_text == "Thanks, talk soon."  # llm_node recorded what the model said


async def test_end_call_without_a_spoken_line_is_refused_once_then_goes_through(
    ended: list[tuple[str, bool]], caplog: pytest.LogCaptureFixture
) -> None:
    """The first silent end_call gets a ToolError so the model says its closing line; the repeat is allowed."""
    agent, _ = make_agent(ended)
    with caplog.at_level("WARNING", logger="posivex.agent"):
        await run_turn(agent, ScriptedLLM("", [("end_call", '{"reason": "conversation_complete"}')]))
    assert "asking the model to say its closing line" in caplog.text
    assert ended == [("conversation_complete", True)]  # only the second attempt ended the call


async def test_voicemail_detection_hangs_up_without_draining_and_records_the_kind(
    ended: list[tuple[str, bool]],
) -> None:
    agent, state = make_agent(ended)
    await run_turn(agent, ScriptedLLM("", [("voicemail_detection", '{"reason": "phone menu", "kind": "menu"}')]))
    assert ended == [("voicemail_detected", False)]
    assert (state.voicemail_detected, state.machine_kind) == (True, "menu")


async def test_skip_turn_starts_a_hold_and_stays_quiet(ended: list[tuple[str, bool]]) -> None:
    agent, state = make_agent(ended)
    await run_turn(agent, ScriptedLLM("", [("skip_turn", '{"reason": "checking with owner"}')]))
    assert state.is_holding and ended == []
