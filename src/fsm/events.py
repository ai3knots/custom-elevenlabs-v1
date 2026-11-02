"""FSM transition event models and custom exceptions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.fsm.states import State


class FSMError(Exception):
    """Base exception for all finite state machine errors."""

    pass


class InvalidStateError(FSMError):
    """Raised when an unknown or invalid state string is supplied."""

    pass


class IllegalTransitionError(FSMError):
    """Raised when attempting an unauthorized transition between states."""

    def __init__(self, from_state: State, to_state: State, reason: str = "") -> None:
        message = f"Illegal transition from '{from_state.value}' to '{to_state.value}'"
        if reason:
            message += f" (reason: {reason})"
        super().__init__(message)
        self.from_state = from_state
        self.to_state = to_state
        self.reason = reason


@dataclass(frozen=True)
class TransitionEvent:
    """Historical record of an individual state transition."""

    from_state: State
    to_state: State
    reason: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
