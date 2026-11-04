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
    assert len(agent.tools) == 3  # transition, set_call_outcome and skip_turn tools


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
    assert reply is None  # None completes the tool without a spoken follow-up
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


@pytest.mark.asyncio
async def test_set_call_outcome_records_and_validates(agent_fixture):
    """The LLM-facing outcome tool stores valid outcomes and rejects unknown ones."""
    from livekit.agents.llm import ToolError

    agent, fsm, _ = agent_fixture
    await agent.set_call_outcome("Consultant Call Scheduled", rationale="Demo Tue 3pm, tony@x.com")
    assert fsm.call_outcome == "consultant_call_scheduled"
    assert "tony@x.com" in (fsm.outcome_rationale or "")

    with pytest.raises(ToolError):
        await agent.set_call_outcome("made_up")


@pytest.mark.asyncio
async def test_illegal_transition_surfaces_tool_error(agent_fixture):
    """An illegal transition tells the LLM which states are allowed."""
    from livekit.agents.llm import ToolError

    agent, fsm, _ = agent_fixture
    with pytest.raises(ToolError, match="may only move to"):
        await agent.transition("lock_in", reason="skipping ahead")
    assert fsm.current_state == State.OPENER


def test_resolve_outcome_fallbacks():
    """Fallback outcomes follow the CRM's hangup_no_speak / unknown_dropped contract."""
    from src.agent.session_handler import _resolve_outcome

    fsm = ConversationFSM()
    assert _resolve_outcome(fsm, 0)[0] == "hangup_no_speak"
    assert _resolve_outcome(fsm, 2)[0] == "unknown_dropped"
    fsm.set_outcome("callback_set", "Thursday 10am")
    assert _resolve_outcome(fsm, 2) == ("callback_set", "Thursday 10am")


def test_persona_agent_syncs_thought_signatures():
    """Verify that PersonaAgent synchronizes thought_signatures across all LLMs and adapters."""
    from livekit.agents.llm import FallbackAdapter

    class DummyLLM:
        def __init__(self):
            self._thought_signatures = {}

    primary_child1 = DummyLLM()
    primary_child2 = DummyLLM()
    primary_adapter = MagicMock(spec=FallbackAdapter)
    primary_adapter._llm_instances = [primary_child1, primary_child2]

    conversation_child1 = DummyLLM()
    conversation_adapter = MagicMock(spec=FallbackAdapter)
    conversation_adapter._llm_instances = [conversation_child1]

    agent = PersonaAgent(
        fsm=ConversationFSM(),
        prompt_manager=PromptManager(persona="amelia", language="en"),
        variables=DynamicVariables(),
        llm_primary=primary_adapter,  # type: ignore[arg-type]
        llm_conversation=conversation_adapter,  # type: ignore[arg-type]
    )
    assert agent._shared_thought_signatures is not None

    # Add signature via primary
    primary_child1._thought_signatures["call_123"] = b"sig_bytes_123"

    # Verify conversation child sees it
    assert conversation_child1._thought_signatures.get("call_123") == b"sig_bytes_123"

    # Add signature via conversation
    conversation_child1._thought_signatures["call_456"] = b"sig_bytes_456"

    # Verify primary child sees it
    assert primary_child2._thought_signatures.get("call_456") == b"sig_bytes_456"


async def test_end_transition_waits_for_closing_line_before_shutdown(agent_fixture):
    """The Close step speaks and calls transition('end') in one step; shutdown must follow playout."""
    agent, fsm, _ = agent_fixture
    for state in ("conversation", "close"):
        await fsm.transition(state, reason="setup")

    order: list[str] = []
    session = MagicMock()
    session.shutdown = MagicMock(side_effect=lambda **_: order.append("shutdown"))
    agent.attach_test_session(session)

    context = MagicMock()

    async def playout() -> None:
        order.append("playout")

    context.wait_for_playout = playout

    await agent.transition("end", reason="closing line delivered", context=context)

    assert order == ["playout", "shutdown"]


# --------------------------------------------------------------------------------------------------
# Call-analysis fixes (knowledge base, atomic transitions, AMD as single source, no empty replies)
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_is_atomic_when_prompt_rendering_fails(agent_fixture):
    """A prompt that cannot be built must leave the FSM and the agent's instructions untouched."""
    from livekit.agents.llm import ToolError

    agent, fsm, prompt_manager = agent_fixture
    before = agent.instructions
    prompt_manager.render_prompt = MagicMock(side_effect=RuntimeError("template broke"))

    with pytest.raises(ToolError, match="temporarily unavailable"):
        await agent.transition("conversation", reason="Owner on the phone")

    assert fsm.current_state == State.OPENER
    assert agent.instructions == before
    # The same transition can be retried later instead of failing with 'conversation -> conversation'.
    prompt_manager.render_prompt = MagicMock(return_value="# retry prompt")
    await agent.transition("conversation", reason="Owner on the phone")
    assert fsm.current_state == State.CONVERSATION


@pytest.mark.asyncio
async def test_transition_rolls_back_when_applying_state_fails(agent_fixture):
    from livekit.agents.llm import ToolError

    agent, fsm, _ = agent_fixture
    agent.update_instructions = AsyncMock(side_effect=RuntimeError("update failed"))

    with pytest.raises(ToolError):
        await agent.transition("reach_owner", reason="Employee answered")

    assert fsm.current_state == State.OPENER
    assert fsm.previous_state is None
    assert fsm.history == []


@pytest.mark.asyncio
async def test_missing_knowledge_base_does_not_break_the_conversation_state(agent_fixture):
    from src.config import KBConfig

    agent, fsm, prompt_manager = agent_fixture
    prompt_manager.kb_provider.config = KBConfig(mode="inline", file_path="does/not/exist.md")
    prompt_manager.kb_provider._cached_content = None

    reply = await agent.transition("conversation", reason="Owner on the phone")
    assert "Successfully transitioned" in reply
    assert fsm.current_state == State.CONVERSATION
    assert "# Knowledge Base" not in agent.instructions


@pytest.mark.asyncio
async def test_ending_the_call_does_not_request_a_follow_up_reply(agent_fixture):
    """transition(end/dnc_close) and set_call_outcome must not trigger an (empty) LLM reply."""
    from livekit.agents.llm import ToolResult

    agent, fsm, _ = agent_fixture
    agent.attach_test_session(MagicMock())

    outcome = await agent.set_call_outcome("declined", rationale="Not interested")
    assert isinstance(outcome, ToolResult) and outcome.reply_required is False

    ended = await agent.transition("end", reason="done")
    assert isinstance(ended, ToolResult) and ended.reply_required is False
    assert fsm.is_terminal


@pytest.mark.asyncio
async def test_non_terminal_transition_still_requests_a_reply(agent_fixture):
    agent, _, _ = agent_fixture
    reply = await agent.transition("reach_owner", reason="Employee answered")
    assert isinstance(reply, str)


@pytest.mark.asyncio
async def test_amd_human_blocks_llm_from_ending_opener_on_recording_notice(agent_fixture):
    """AMD said 'human': a recording notice alone must not let the LLM end the call as a machine."""
    from livekit.agents.llm import ToolError

    agent, fsm, _ = agent_fixture
    agent.amd_category = "human"
    agent._recent_user_text = ["Hello? This call is being recorded."]

    with pytest.raises(ToolError, match="live person"):
        await agent.set_call_outcome("automated_menu", rationale="recording notice")
    with pytest.raises(ToolError, match="live person"):
        await agent.transition("end", reason="voicemail")
    assert fsm.call_outcome is None
    assert fsm.current_state == State.OPENER


@pytest.mark.asyncio
async def test_amd_guard_allows_real_voicemail_cues_and_uncertain_verdicts(agent_fixture):
    agent, fsm, _ = agent_fixture
    agent.attach_test_session(MagicMock())

    # AMD said human, but the callee then plays an unmistakable voicemail greeting.
    agent.amd_category = "human"
    agent._recent_user_text = ["Hi you've reached Tony. Please leave a message after the tone."]
    await agent.set_call_outcome("voicemail", rationale="voicemail: leave a message")
    assert fsm.call_outcome == "voicemail"

    # AMD uncertain: the LLM keeps its judgement.
    agent.amd_category = "uncertain"
    agent._recent_user_text = ["This call is being recorded."]
    await agent.set_call_outcome("automated_menu", rationale="screener")
    assert fsm.call_outcome == "automated_menu"


@pytest.mark.asyncio
async def test_replies_are_held_until_the_opening_line_then_released():
    from livekit.agents import StopResponse
    from livekit.agents.llm import ChatContext, ChatMessage

    held: list[str] = []
    agent = PersonaAgent(
        fsm=ConversationFSM(),
        prompt_manager=PromptManager(persona="amelia", language="en"),
        variables=DynamicVariables(),
        llm_primary=MockLLM(),
        llm_conversation=MockLLM(),
        hold_replies=True,
        on_gated_turn=held.append,
    )

    message = ChatMessage(role="user", content=["This call is being recorded."])
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(ChatContext(), message)
    assert held == ["This call is being recorded."]

    agent.open_reply_gate()
    await agent.on_user_turn_completed(ChatContext(), ChatMessage(role="user", content=["Hello?"]))
    assert held == ["This call is being recorded."]  # not held any more
    assert agent._recent_user_text[-1] == "Hello?"


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
