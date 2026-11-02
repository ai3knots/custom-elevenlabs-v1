"""Unit tests for PersonaAgent state orchestration, instruction updates, and DNC delivery."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.persona_agent import PersonaAgent
from src.agent.session_handler import parse_call_request_from_room
from src.config import DynamicVariables
from src.fsm.machine import ConversationFSM
from src.fsm.states import DNC_APOLOGY_SPEECH, State
from src.prompts.manager import PromptManager
from src.providers.mock import MockLLM


@pytest.fixture
def agent_fixture():
    """Build a test PersonaAgent instance with mocks."""
    variables = DynamicVariables(
        business_name="Tony's Pizza & Grill",
        business_context="Pizza restaurant in Atlanta",
        current_datetime="Tuesday, October 20, 2026",
    )
    fsm = ConversationFSM()
    prompt_manager = PromptManager(persona="amelia", language="en")
    llm_primary = MockLLM(default_response="primary response")
    llm_conversation = MockLLM(default_response="conversation response")

    agent = PersonaAgent(
        fsm=fsm,
        prompt_manager=prompt_manager,
        variables=variables,
        llm_primary=llm_primary,
        llm_conversation=llm_conversation,
        call_id="call_test_123",
    )
    return agent, fsm, prompt_manager


def test_persona_agent_initialization(agent_fixture):
    """Verify PersonaAgent is configured with Opener instructions and tools."""
    agent, fsm, _ = agent_fixture

    assert fsm.current_state == State.OPENER
    assert "You are Amelia" in agent.instructions
    assert "Node 1: Opener & Route" in agent.instructions
    assert len(agent.tools) == 2  # transition and skip_turn tools


@pytest.mark.asyncio
async def test_agent_transition_updates_instructions(agent_fixture):
    """Verify calling transition tool updates instructions and active state."""
    agent, fsm, _ = agent_fixture

    # Transition: Opener -> Conversation
    reply = await agent.transition("conversation", reason="Owner on the phone")
    assert "Successfully transitioned" in reply
    assert fsm.current_state == State.CONVERSATION
    assert "Node 3: Conversation" in agent.instructions
    assert "# Knowledge Base" in agent.instructions

    # Transition: Conversation -> Lock In
    reply2 = await agent.transition("lock_in", reason="Agreed to quote")
    assert "Successfully transitioned" in reply2
    assert fsm.current_state == State.LOCK_IN
    assert "Node 4: Lock In Next Step" in agent.instructions


@pytest.mark.asyncio
async def test_agent_skip_turn_registers_hold(agent_fixture):
    """Verify calling skip_turn sets hold timer on FSM."""
    agent, fsm, _ = agent_fixture
    assert not fsm.is_holding

    reply = await agent.skip_turn(reason="One second, checking")
    assert "Hold requested" in reply
    assert fsm.is_holding


@pytest.mark.asyncio
async def test_agent_dnc_transition_flow(agent_fixture):
    """Verify transition to dnc_close plays non-interruptible canned speech and shuts down."""
    agent, fsm, _ = agent_fixture

    # Mock the AgentSession attached to the agent
    mock_session = MagicMock()
    mock_speech_handle = MagicMock()
    mock_speech_handle.wait_for_playout = AsyncMock()
    mock_session.say.return_value = mock_speech_handle
    mock_session.shutdown = MagicMock()

    agent.attach_test_session(mock_session)

    # Execute DNC transition from Opener
    await agent.transition("dnc_close", reason="Prospect said 'don't call again'")

    assert fsm.call_outcome == "do_not_call"
    assert fsm.is_terminal
    assert fsm.current_state == State.END

    # Verify canned speech playout
    mock_session.say.assert_called_once_with(
        DNC_APOLOGY_SPEECH,
        allow_interruptions=False,
    )
    mock_speech_handle.wait_for_playout.assert_awaited_once()
    mock_session.shutdown.assert_called_once_with()


def test_parse_call_request_from_room():
    """Verify parsing CallRequest from room metadata JSON."""
    raw_json = (
        '{"call_id": "call_abc", "phone": "+14045551234", "persona": "michael", '
        '"language": "es", "dynamic_variables": {"business_name": "Tacos El Rey"}}'
    )
    req = parse_call_request_from_room(raw_json)
    assert req.call_id == "call_abc"
    assert req.phone == "+14045551234"
    assert req.persona == "michael"
    assert req.language == "es"
    assert req.dynamic_variables.business_name == "Tacos El Rey"

    # Fallback on empty or invalid metadata
    req_empty = parse_call_request_from_room("")
    assert req_empty.phone == "+14045550000"

    req_invalid = parse_call_request_from_room("not-valid-json")
    assert req_invalid.phone == "+14045550000"
