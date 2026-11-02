"""Deterministic Finite State Machine governing the sales conversation workflow."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

from src.fsm.events import IllegalTransitionError, InvalidStateError, TransitionEvent
from src.fsm.states import STATE_REGISTRY, State, StateMetadata

logger = logging.getLogger("posivex.fsm")

TransitionListener = Callable[[TransitionEvent], None] | Callable[[TransitionEvent], Coroutine[Any, Any, None]]


class ConversationFSM:
    """Manages conversational state progression, transition constraints, and timeouts."""

    def __init__(
        self,
        initial_state: State | str = State.OPENER,
        max_silence_count: int = 3,
        hold_duration_seconds: float = 12.0,
    ) -> None:
        self._current_state: State = self._coerce_state(initial_state)
        self._previous_state: State | None = None
        self._history: list[TransitionEvent] = []
        self._listeners: list[TransitionListener] = []

        self._max_silence_count = max_silence_count
        self._default_hold_duration_seconds = hold_duration_seconds
        self._silence_count = 0
        self._hold_until: datetime | None = None

        self._call_outcome: str | None = None
        self._outcome_rationale: str | None = None

    @property
    def current_state(self) -> State:
        """Current active state."""
        return self._current_state

    @property
    def previous_state(self) -> State | None:
        """Prior state immediately preceding the current state."""
        return self._previous_state

    @property
    def history(self) -> list[TransitionEvent]:
        """Chronological list of all state transitions performed."""
        return list(self._history)

    @property
    def silence_count(self) -> int:
        """Consecutive unhandled silence occurrences."""
        return self._silence_count

    @property
    def call_outcome(self) -> str | None:
        """Classified call outcome matching Posivex-lead-gen enum."""
        return self._call_outcome

    @property
    def outcome_rationale(self) -> str | None:
        """Explanation or notes accompanying the classified outcome."""
        return self._outcome_rationale

    @property
    def is_terminal(self) -> bool:
        """True if the FSM has entered the terminal END state."""
        return self._current_state == State.END

    @property
    def metadata(self) -> StateMetadata:
        """Operational metadata for the current state."""
        return STATE_REGISTRY[self._current_state]

    @property
    def canned_speech(self) -> str | None:
        """Literal text to speak if current state defines canned speech."""
        return self.metadata.canned_speech

    def get_metadata(self, state: State) -> StateMetadata:
        """Obtain metadata for any specified state."""
        return STATE_REGISTRY[state]

    def _coerce_state(self, target: State | str) -> State:
        """Coerce string or State into validated State enum."""
        if isinstance(target, State):
            return target
        try:
            return State(target.lower().strip())
        except ValueError as err:
            raise InvalidStateError(f"Unknown state: '{target}'") from err

    def can_transition(self, to_state: State | str) -> bool:
        """Check if transition from current state to target state is permissible."""
        try:
            target = self._coerce_state(to_state)
        except InvalidStateError:
            return False

        if self.is_terminal:
            return False

        allowed = self.metadata.allowed_transitions
        return target in allowed

    async def transition(self, to_state: State | str, reason: str = "") -> State:
        """Execute a validated transition to the target state.

        Raises:
            IllegalTransitionError: If the transition violates the transition matrix.
            InvalidStateError: If the target state name is unrecognized.
        """
        target = self._coerce_state(to_state)

        if not self.can_transition(target):
            raise IllegalTransitionError(self._current_state, target, reason=reason)

        from_state = self._current_state
        self._previous_state = from_state
        self._current_state = target
        self._silence_count = 0  # Reset silence counter on active transition

        # If entering DNC_CLOSE, automatically set do_not_call outcome
        if target == State.DNC_CLOSE and not self._call_outcome:
            self._call_outcome = "do_not_call"
            self._outcome_rationale = reason or "Prospect requested not to be called."

        event = TransitionEvent(from_state=from_state, to_state=target, reason=reason)
        self._history.append(event)

        logger.info(
            "FSM state transition: %s -> %s (reason: %s)",
            from_state.value,
            target.value,
            reason,
            extra={"from_state": from_state.value, "to_state": target.value, "reason": reason},
        )

        # Notify transition listeners
        for listener in self._listeners:
            try:
                res = listener(event)
                if inspect.isawaitable(res):
                    await res
            except Exception as e:
                logger.error("Error in transition listener: %s", e, exc_info=True)

        return self._current_state

    def add_transition_listener(self, listener: TransitionListener) -> None:
        """Register a callback invoked whenever a transition occurs."""
        self._listeners.append(listener)

    async def record_silence(self) -> int:
        """Increment silence counter and trigger transition to END if max threshold exceeded."""
        self._silence_count += 1
        recorded_count = self._silence_count
        logger.warning(
            "Silence recorded: %d/%d (state: %s)",
            recorded_count,
            self._max_silence_count,
            self._current_state.value,
        )

        if recorded_count >= self._max_silence_count:
            if not self._call_outcome:
                self._call_outcome = "no_answer" if self._current_state == State.OPENER else "unknown_dropped"
                self._outcome_rationale = f"Max silence count ({self._max_silence_count}) exceeded."
            await self.transition(State.END, reason="max_silence_timeout")

        return recorded_count

    def reset_silence(self) -> None:
        """Reset silence counter back to 0."""
        self._silence_count = 0

    def request_hold(self, duration_seconds: float | None = None) -> datetime:
        """Place the FSM in hold state until the specified timestamp."""
        duration = duration_seconds if duration_seconds is not None else self._default_hold_duration_seconds
        self._hold_until = datetime.now(UTC) + timedelta(seconds=duration)
        logger.info("FSM hold requested for %.1f seconds (until %s)", duration, self._hold_until.isoformat())
        return self._hold_until

    @property
    def is_holding(self) -> bool:
        """True if the hold period is currently active."""
        if self._hold_until is None:
            return False
        if datetime.now(UTC) >= self._hold_until:
            self._hold_until = None
            return False
        return True

    def release_hold(self) -> None:
        """Explicitly cancel any active hold period."""
        self._hold_until = None

    def set_outcome(self, outcome: str, rationale: str = "") -> None:
        """Assign or override the classified call outcome and rationale."""
        self._call_outcome = outcome
        if rationale:
            self._outcome_rationale = rationale
