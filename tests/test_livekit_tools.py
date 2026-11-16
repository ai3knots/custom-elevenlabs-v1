"""The agent's tools driven by LiveKit's own machinery: a real AgentSession in text mode (the pattern LiveKit
recommends for unit tests) with a scripted model. This is what the model's tool calls hit in a live call, including
the framework injecting the RunContext into `end_call`.

Text mode skips the voice-only turn hook (`on_user_turn_completed`), so the reply gate is covered in
tests/test_run_agent_session.py instead.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from livekit.agents import AgentSession
from livekit.agents.llm import ToolContext

from src.agent.persona_agent import CallState, PersonaAgent
from src.agent.transfer import CallTransferrer, build_transfer_tool
from src.config import CallRequest, TransferConfig
from tests.support.fake_call import FakeLiveKitApi
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


# ---- the transfer tool (it exists only on calls that may be transferred) ------------------------------------------
def make_transfer_agent(
    ended: list[tuple[str, bool]], api: FakeLiveKitApi, *, phone: bool = True
) -> tuple[PersonaAgent, CallState]:
    state = CallState()

    async def emit(event_type: str, payload: dict[str, Any]) -> None:
        return None

    transferrer = CallTransferrer(
        ctx=SimpleNamespace(room=SimpleNamespace(name="call_x"), api=api),
        call_request=CallRequest(call_id="lk_x", phone="+14045550000"),
        config=TransferConfig(enabled=True),
        call_state=state,
        is_phone_call=lambda: phone,
        callee_identity=lambda: "phone_+14045550000",
        call_ending=lambda: False,
        end_call=lambda r, d: ended.append((r, d)),
        emit=emit,
    )
    agent = PersonaAgent(
        instructions="You are Amelia.",
        call_state=state,
        on_end_call=lambda r, d: ended.append((r, d)),
        extra_tools=[build_transfer_tool(transferrer)],
    )
    return agent, state


def test_the_model_sees_transfer_call_with_four_optional_arguments_and_no_context(ended: list[tuple[str, bool]]) -> None:
    agent, _ = make_transfer_agent(ended, FakeLiveKitApi())
    schemas = {s["function"]["name"]: s["function"] for s in ToolContext(agent.tools).parse_function_tools("openai")}
    assert sorted(schemas) == ["end_call", "skip_turn", "transfer_call", "voicemail_detection"]
    properties = schemas["transfer_call"]["parameters"]["properties"]
    assert sorted(properties) == ["callback_number", "prospect_email", "prospect_name", "summary"]  # the RunContext is injected
    assert "only when the owner or the person who makes the decision asks to talk to a person" in schemas["transfer_call"]["description"]


def test_without_the_transfer_tool_the_models_tools_are_the_same_three(ended: list[tuple[str, bool]]) -> None:
    agent, _ = make_agent(ended)
    assert sorted(ToolContext(agent.tools).function_tools) == ["end_call", "skip_turn", "voicemail_detection"]


async def test_a_refused_transfer_comes_back_to_the_model_as_a_tool_error_and_the_call_goes_on(
    ended: list[tuple[str, bool]],
) -> None:
    """Through LiveKit's own machinery: the framework binds the RunContext, runs the tool, and reports its ToolError."""
    api = FakeLiveKitApi()
    agent, state = make_transfer_agent(ended, api, phone=False)
    turn = await run_turn(agent, ScriptedLLM("", [("transfer_call", '{"summary": "Wants a person."}')]))
    assert "FunctionCallOutputEvent" in turn.events
    assert api.sip.transfers == [] and ended == [] and state.transferred is False
