"""Finite State Machine engine for sales conversation orchestration."""

from src.fsm.events import FSMError, IllegalTransitionError, InvalidStateError, TransitionEvent
from src.fsm.machine import ConversationFSM
from src.fsm.states import (
    DNC_APOLOGY_SPEECH,
    STATE_REGISTRY,
    EntryBehavior,
    State,
    StateMetadata,
)

__all__ = [
    "DNC_APOLOGY_SPEECH",
    "STATE_REGISTRY",
    "ConversationFSM",
    "EntryBehavior",
    "FSMError",
    "IllegalTransitionError",
    "InvalidStateError",
    "State",
    "StateMetadata",
    "TransitionEvent",
]

