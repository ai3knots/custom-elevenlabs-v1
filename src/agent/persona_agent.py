"""PersonaAgent driving the sales workflow with dynamic prompt updates and tool execution."""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from livekit.agents import Agent
from livekit.agents.llm import LLM, ToolError, function_tool

from src.config import DynamicVariables
from src.db.models import CallOutcome
from src.fsm.events import FSMError
from src.fsm.machine import ConversationFSM
from src.fsm.states import DNC_APOLOGY_SPEECH, State, StateMetadata
from src.prompts.manager import PromptManager

logger = logging.getLogger("posivex.agent")

EventHandler = Callable[[str, dict[str, Any]], None] | Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]


class PersonaAgent(Agent):
    """Voice agent representing Amelia or Michael, orchestrated by ConversationFSM."""

    def __init__(
        self,
        fsm: ConversationFSM,
        prompt_manager: PromptManager,
        variables: DynamicVariables,
        llm_primary: LLM,
        llm_conversation: LLM,
        call_id: str | None = None,
        event_handler: EventHandler | None = None,
    ) -> None:
        self.fsm = fsm
        self.prompt_manager = prompt_manager
        self.variables = variables
        self.llm_primary = llm_primary
        self.llm_conversation = llm_conversation
        self.call_id = call_id
        self.event_handler = event_handler
        self._test_session: Any | None = None

        # Synchronize thought_signatures across all underlying LLM instances (including FallbackAdapters)
        # to ensure multi-turn function call signatures are preserved across dynamic model switches and fallbacks.
        self._shared_thought_signatures: dict[str, bytes] = {}
        self._sync_thought_signatures(self.llm_primary)
        if self.llm_conversation is not None:
            self._sync_thought_signatures(self.llm_conversation)

        initial_prompt = self.prompt_manager.render_prompt(self.fsm.current_state, self.variables)
        initial_meta = self.fsm.metadata

        super().__init__(
            instructions=initial_prompt,
            min_endpointing_delay=initial_meta.min_endpointing_delay,
            allow_interruptions=initial_meta.interruptible,
        )

    def _sync_thought_signatures(self, llm_inst: Any) -> None:
        """Point all underlying LLM instances to the shared thought_signatures dict."""
        if hasattr(llm_inst, "_thought_signatures") and isinstance(llm_inst._thought_signatures, dict):
            self._shared_thought_signatures.update(llm_inst._thought_signatures)
            object.__setattr__(llm_inst, "_thought_signatures", self._shared_thought_signatures)

        # If it's a FallbackAdapter or container, recurse over children
        if hasattr(llm_inst, "_llm_instances"):
            for child in llm_inst._llm_instances:
                self._sync_thought_signatures(child)

    @property
    def active_session(self) -> Any | None:
        """Return the active AgentSession or test session if attached."""
        if self._test_session is not None:
            return self._test_session
        try:
            return self.session
        except RuntimeError:
            return None

    def attach_test_session(self, session: Any) -> None:
        """Attach a mock or test session for isolated unit testing."""
        self._test_session = session

    async def _emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Helper to invoke optional external event listeners."""
        if not self.event_handler:
            return
        try:
            res = self.event_handler(event_type, payload)
            if res is not None:
                await res
        except Exception as e:
            logger.error("Error in event handler: %s", e, exc_info=True)

    @function_tool
    async def transition(self, to: str, reason: str = "") -> str:
        """Request a transition to another state in the sales conversation.

        Args:
            to: Target state name ('reach_owner', 'conversation', 'lock_in', 'close', 'dnc_close', 'end')
            reason: Justification based on what the prospect said
        """
        logger.info("Agent requesting transition to '%s' (reason: %s)", to, reason)
        try:
            new_state = await self.fsm.transition(to, reason=reason)
        except FSMError as e:
            allowed = ", ".join(sorted(s.value for s in self.fsm.metadata.allowed_transitions))
            raise ToolError(
                f"{e}. From '{self.fsm.current_state.value}' you may only move to: {allowed or 'nowhere'}."
            ) from e
        await self._apply_state(new_state)
        await self._emit_event(
            "state_transition",
            {
                "from_state": self.fsm.previous_state.value if self.fsm.previous_state else None,
                "to_state": new_state.value,
                "reason": reason,
                "call_id": self.call_id,
            },
        )
        return f"Successfully transitioned to state: {new_state.value}"

    @function_tool
    async def set_call_outcome(self, outcome: str, rationale: str = "") -> str:
        """Record how this call ended. Call it once the result is known, in the same turn as the final transition.

        Args:
            outcome: One of 'voicemail', 'no_answer', 'automated_menu', 'employee_no_owner_details',
                'owner_details_captured', 'callback_set', 'info_email', 'email_comparison',
                'consultant_call_scheduled', 'declined', 'do_not_call', 'other'
            rationale: One short sentence with the facts behind the outcome (names, emails, times agreed)
        """
        value = outcome.strip().lower().replace(" ", "_").replace("-", "_")
        valid = [o.value for o in CallOutcome]
        if value not in valid:
            raise ToolError(f"Unknown outcome '{outcome}'. Use one of: {', '.join(valid)}.")
        self.fsm.set_outcome(value, rationale=rationale)
        logger.info("Agent recorded call outcome '%s' (%s)", value, rationale)
        await self._emit_event(
            "outcome_recorded",
            {"outcome": value, "reason": rationale, "call_id": self.call_id},
        )
        return f"Outcome recorded: {value}"

    @function_tool
    async def skip_turn(self, reason: str = "Merchant requested hold") -> None:
        """Stay silent while the merchant is on hold or fetching the owner.

        Args:
            reason: Why the hold was requested (e.g. 'checking with owner', 'one second')
        """
        logger.info("Agent requested skip_turn/hold (reason: %s)", reason)
        self.fsm.request_hold()
        await self._emit_event(
            "hold_requested",
            {
                "reason": reason,
                "call_id": self.call_id,
            },
        )
        # Returning None completes the tool without a follow-up reply, so the agent stays quiet.
        return None

    async def _apply_state(self, new_state: State) -> None:
        """Update instructions, models, and turn settings to match the new state."""
        meta: StateMetadata = self.fsm.get_metadata(new_state)

        # 1. DNC Close handling (canned apology line and immediate shutdown)
        if new_state == State.DNC_CLOSE:
            logger.info("Entering DNC_CLOSE: Delivering literal canned apology speech.")
            session = self.active_session
            if session is not None:
                speech_handle = session.say(
                    DNC_APOLOGY_SPEECH,
                    allow_interruptions=False,
                )
                await speech_handle.wait_for_playout()

            # Conclude call via FSM to END
            await self.fsm.transition(State.END, reason="dnc_apology_delivered")
            session = self.active_session
            if session is not None:
                session.shutdown()
            return

        # 2. Terminal state handling
        if new_state == State.END:
            logger.info("Entering END state: shutting down session.")
            session = self.active_session
            if session is not None:
                session.shutdown()
            return

        # 3. Conversational state update
        new_prompt = self.prompt_manager.render_prompt(new_state, self.variables)
        await self.update_instructions(new_prompt)

        # Dynamically switch LLM: CONVERSATION uses gemini-3.5-flash, others use flash-lite
        if new_state == State.CONVERSATION:
            self.update_options(llm=self.llm_conversation)
        else:
            self.update_options(llm=self.llm_primary)

        # Update endpointing delay for the new state
        session = self.active_session
        if session is not None and hasattr(session, "update_options"):
            session.update_options(endpointing_opts={"min_delay": meta.min_endpointing_delay})
