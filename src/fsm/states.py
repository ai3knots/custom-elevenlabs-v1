"""Conversation state definitions, entry behaviors, and state metadata."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class State(enum.StrEnum):
    """The 7 explicit conversation states in the sales workflow."""

    OPENER = "opener"
    REACH_OWNER = "reach_owner"
    CONVERSATION = "conversation"
    LOCK_IN = "lock_in"
    CLOSE = "close"
    DNC_CLOSE = "dnc_close"
    END = "end"


class EntryBehavior(enum.StrEnum):
    """Behavior of the agent upon entering a state."""

    WAIT_FOR_USER = "wait_for_user"
    GENERATE_IMMEDIATELY = "generate_immediately"
    SPEAK_CANNED = "speak_canned"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class StateMetadata:
    """Metadata and operational configuration for an individual state."""

    state: State
    entry_behavior: EntryBehavior
    prompt_fragment_file: str | None
    allowed_transitions: frozenset[State]
    default_model: str
    min_endpointing_delay: float
    interruptible: bool = True
    canned_speech: str | None = None
    default_outcome: str | None = None


# Literal DNC apology speech matching ElevenLabs production parity
DNC_APOLOGY_SPEECH = (
    "Understood, my apologies. I'll make sure we don't call this number again. Take care."
)

# Operational metadata registry for all 7 states
STATE_REGISTRY: dict[State, StateMetadata] = {
    State.OPENER: StateMetadata(
        state=State.OPENER,
        entry_behavior=EntryBehavior.WAIT_FOR_USER,
        prompt_fragment_file="nodes/node_1_opener.md",
        allowed_transitions=frozenset(
            {State.REACH_OWNER, State.CONVERSATION, State.DNC_CLOSE, State.END}
        ),
        default_model="gemini-3.5-flash-lite",
        min_endpointing_delay=0.3,
        interruptible=True,
    ),
    State.REACH_OWNER: StateMetadata(
        state=State.REACH_OWNER,
        entry_behavior=EntryBehavior.GENERATE_IMMEDIATELY,
        prompt_fragment_file="nodes/node_2_reach_owner.md",
        allowed_transitions=frozenset(
            {State.CONVERSATION, State.CLOSE, State.DNC_CLOSE, State.END}
        ),
        default_model="gemini-3.5-flash-lite",
        min_endpointing_delay=0.5,
        interruptible=True,
    ),
    State.CONVERSATION: StateMetadata(
        state=State.CONVERSATION,
        entry_behavior=EntryBehavior.GENERATE_IMMEDIATELY,
        prompt_fragment_file="nodes/node_3_conversation.md",
        allowed_transitions=frozenset(
            {State.LOCK_IN, State.CLOSE, State.DNC_CLOSE, State.END}
        ),
        default_model="gemini-3.5-flash",
        min_endpointing_delay=0.5,
        interruptible=True,
    ),
    State.LOCK_IN: StateMetadata(
        state=State.LOCK_IN,
        entry_behavior=EntryBehavior.GENERATE_IMMEDIATELY,
        prompt_fragment_file="nodes/node_4_lock_in.md",
        # Backward loop to CONVERSATION allowed when owner hesitates or asks questions
        allowed_transitions=frozenset(
            {State.CONVERSATION, State.CLOSE, State.DNC_CLOSE, State.END}
        ),
        default_model="gemini-3.5-flash-lite",
        min_endpointing_delay=1.0,
        interruptible=True,
    ),
    State.CLOSE: StateMetadata(
        state=State.CLOSE,
        entry_behavior=EntryBehavior.GENERATE_IMMEDIATELY,
        prompt_fragment_file="nodes/node_5_close.md",
        allowed_transitions=frozenset({State.END}),
        default_model="gemini-3.5-flash-lite",
        min_endpointing_delay=0.4,
        interruptible=True,
    ),
    State.DNC_CLOSE: StateMetadata(
        state=State.DNC_CLOSE,
        entry_behavior=EntryBehavior.SPEAK_CANNED,
        prompt_fragment_file=None,
        allowed_transitions=frozenset({State.END}),
        default_model="gemini-3.5-flash-lite",
        min_endpointing_delay=0.2,
        interruptible=False,  # Non-interruptible canned closing
        canned_speech=DNC_APOLOGY_SPEECH,
        default_outcome="do_not_call",
    ),
    State.END: StateMetadata(
        state=State.END,
        entry_behavior=EntryBehavior.TERMINAL,
        prompt_fragment_file=None,
        allowed_transitions=frozenset(),
        default_model="gemini-3.5-flash-lite",
        min_endpointing_delay=0.0,
        interruptible=False,
    ),
}
