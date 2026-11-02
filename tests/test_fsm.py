"""Unit tests for the sales conversation Finite State Machine (FSM)."""

import pytest

from src.fsm import (
    DNC_APOLOGY_SPEECH,
    STATE_REGISTRY,
    ConversationFSM,
    IllegalTransitionError,
    InvalidStateError,
    State,
    TransitionEvent,
)


def test_initial_state():
    """Verify FSM begins in OPENER state with clean metrics."""
    fsm = ConversationFSM()
    assert fsm.current_state == State.OPENER
    assert fsm.previous_state is None
    assert fsm.history == []
    assert fsm.silence_count == 0
    assert not fsm.is_terminal
    assert not fsm.is_holding
    assert fsm.call_outcome is None


@pytest.mark.asyncio
async def test_standard_forward_flow():
    """Verify normal 5-beat sales call progression."""
    fsm = ConversationFSM()

    # Beat 1: Opener -> Conversation (owner confirmed)
    assert fsm.can_transition(State.CONVERSATION)
    await fsm.transition(State.CONVERSATION, reason="Owner confirmed identity")
    assert fsm.current_state == State.CONVERSATION
    assert fsm.previous_state == State.OPENER

    # Beat 2/3: Conversation -> Lock In (prospect interested in quote)
    assert fsm.can_transition(State.LOCK_IN)
    await fsm.transition(State.LOCK_IN, reason="Agreed to rate review")
    assert fsm.current_state == State.LOCK_IN
    assert fsm.previous_state == State.CONVERSATION

    # Beat 4: Lock In -> Close (meeting scheduled and details confirmed)
    assert fsm.can_transition(State.CLOSE)
    await fsm.transition(State.CLOSE, reason="Demo time and email confirmed")
    assert fsm.current_state == State.CLOSE

    # Beat 5: Close -> End (parting line spoken)
    assert fsm.can_transition(State.END)
    await fsm.transition(State.END, reason="Closing speech completed")
    assert fsm.current_state == State.END
    assert fsm.is_terminal

    # Verify history audit trail
    assert len(fsm.history) == 4
    assert [e.to_state for e in fsm.history] == [
        State.CONVERSATION,
        State.LOCK_IN,
        State.CLOSE,
        State.END,
    ]


@pytest.mark.asyncio
async def test_gatekeeper_reach_owner_flow():
    """Verify routing through reach_owner state when receptionist answers."""
    fsm = ConversationFSM()

    # Opener -> ReachOwner
    await fsm.transition(State.REACH_OWNER, reason="Staff member answered")
    assert fsm.current_state == State.REACH_OWNER

    # Scenario A: Owner brought to phone
    await fsm.transition(State.CONVERSATION, reason="Owner picked up transferred call")
    assert fsm.current_state == State.CONVERSATION

    # Or Scenario B: Staff refuses, transitions directly to Close
    fsm2 = ConversationFSM()
    await fsm2.transition(State.REACH_OWNER, reason="Staff member answered")
    assert fsm2.can_transition(State.CLOSE)
    await fsm2.transition(State.CLOSE, reason="Staff refused transfer")
    assert fsm2.current_state == State.CLOSE


@pytest.mark.asyncio
async def test_backward_loop_lock_in_to_conversation():
    """Verify backward loop from lock_in to conversation when owner hesitates or asks product questions."""
    fsm = ConversationFSM()

    await fsm.transition(State.CONVERSATION, reason="Owner confirmed")
    await fsm.transition(State.LOCK_IN, reason="Pitch accepted")

    # Owner hesitates during date selection: "Wait, do I need new hardware?"
    assert fsm.can_transition(State.CONVERSATION)
    await fsm.transition(State.CONVERSATION, reason="Owner raised terminal compatibility question")
    assert fsm.current_state == State.CONVERSATION

    # Agent addresses question, transitions back to Lock In
    assert fsm.can_transition(State.LOCK_IN)
    await fsm.transition(State.LOCK_IN, reason="Objection handled, returning to booking")
    assert fsm.current_state == State.LOCK_IN

    # Finish call
    await fsm.transition(State.CLOSE, reason="All set")
    await fsm.transition(State.END, reason="Finished")
    assert fsm.is_terminal


@pytest.mark.asyncio
async def test_dnc_parity_edge_from_all_active_states():
    """Verify prompt-driven DNC parity edge works from opener, reach_owner, conversation, and lock_in."""
    active_states = [State.OPENER, State.REACH_OWNER, State.CONVERSATION, State.LOCK_IN]

    for start_state in active_states:
        fsm = ConversationFSM(initial_state=start_state)
        assert fsm.can_transition(State.DNC_CLOSE), f"DNC should be allowed from {start_state}"

        await fsm.transition(State.DNC_CLOSE, reason="Prospect said 'take me off your list'")
        assert fsm.current_state == State.DNC_CLOSE
        assert fsm.call_outcome == "do_not_call"
        assert fsm.canned_speech == DNC_APOLOGY_SPEECH
        assert fsm.metadata.interruptible is False

        # Can only transition to END
        assert fsm.can_transition(State.END)
        assert not fsm.can_transition(State.CONVERSATION)
        await fsm.transition(State.END, reason="DNC apology delivered")
        assert fsm.is_terminal


@pytest.mark.asyncio
async def test_illegal_transitions_rejected():
    """Verify invalid or forbidden state transitions raise IllegalTransitionError."""
    fsm = ConversationFSM()

    # Opener cannot skip directly to Lock In or Close
    assert not fsm.can_transition(State.LOCK_IN)
    with pytest.raises(IllegalTransitionError) as exc:
        await fsm.transition(State.LOCK_IN)
    assert exc.value.from_state == State.OPENER
    assert exc.value.to_state == State.LOCK_IN

    assert not fsm.can_transition(State.CLOSE)
    with pytest.raises(IllegalTransitionError):
        await fsm.transition(State.CLOSE)

    # Move to Close
    await fsm.transition(State.CONVERSATION)
    await fsm.transition(State.CLOSE)

    # Close cannot loop back to Conversation or Lock In
    assert not fsm.can_transition(State.CONVERSATION)
    with pytest.raises(IllegalTransitionError):
        await fsm.transition(State.CONVERSATION)

    # Terminal END state cannot transition anywhere
    await fsm.transition(State.END)
    assert not fsm.can_transition(State.OPENER)
    with pytest.raises(IllegalTransitionError):
        await fsm.transition(State.OPENER)


@pytest.mark.asyncio
async def test_invalid_state_string_rejected():
    """Verify unrecognized state names raise InvalidStateError."""
    fsm = ConversationFSM()
    assert not fsm.can_transition("non_existent_state")

    with pytest.raises(InvalidStateError):
        await fsm.transition("non_existent_state")

    with pytest.raises(InvalidStateError):
        _ = ConversationFSM(initial_state="bad_state")


@pytest.mark.asyncio
async def test_silence_counter_and_auto_timeout():
    """Verify silence increments count, auto-terminates on max threshold, and resets properly."""
    fsm = ConversationFSM(max_silence_count=3)

    count1 = await fsm.record_silence()
    assert count1 == 1
    assert not fsm.is_terminal

    count2 = await fsm.record_silence()
    assert count2 == 2
    assert not fsm.is_terminal

    # 3rd consecutive silence triggers transition to END
    count3 = await fsm.record_silence()
    assert count3 == 3
    assert fsm.is_terminal
    assert fsm.current_state == State.END
    assert fsm.call_outcome == "no_answer"
    assert "max_silence_timeout" in fsm.history[-1].reason


@pytest.mark.asyncio
async def test_silence_counter_resets_on_active_transition():
    """Verify that user activity / successful transition clears silence count."""
    fsm = ConversationFSM(max_silence_count=3)
    await fsm.record_silence()
    await fsm.record_silence()
    assert fsm.silence_count == 2

    # User speaks, transitioning to Conversation
    await fsm.transition(State.CONVERSATION, reason="User responded")
    assert fsm.silence_count == 0


def test_hold_timer():
    """Verify hold duration tracking and explicit release."""
    fsm = ConversationFSM(hold_duration_seconds=15.0)
    assert not fsm.is_holding

    fsm.request_hold(10.0)
    assert fsm.is_holding

    fsm.release_hold()
    assert not fsm.is_holding


@pytest.mark.asyncio
async def test_transition_listeners():
    """Verify synchronous and asynchronous transition event listeners are called."""
    events_captured: list[TransitionEvent] = []

    def sync_listener(event: TransitionEvent) -> None:
        events_captured.append(event)

    async def async_listener(event: TransitionEvent) -> None:
        events_captured.append(event)

    fsm = ConversationFSM()
    fsm.add_transition_listener(sync_listener)
    fsm.add_transition_listener(async_listener)

    await fsm.transition(State.CONVERSATION, reason="Test reason")

    assert len(events_captured) == 2
    assert events_captured[0].from_state == State.OPENER
    assert events_captured[0].to_state == State.CONVERSATION
    assert events_captured[0].reason == "Test reason"


def test_state_registry_completeness():
    """Verify all states are registered with valid operational metadata and transitions."""
    for state in State:
        assert state in STATE_REGISTRY
        meta = STATE_REGISTRY[state]
        assert meta.state == state
        # Every allowed transition must be a valid State member
        for target in meta.allowed_transitions:
            assert isinstance(target, State)
            assert target in STATE_REGISTRY
