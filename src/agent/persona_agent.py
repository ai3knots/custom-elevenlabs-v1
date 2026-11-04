"""PersonaAgent driving the sales workflow with dynamic prompt updates and tool execution."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterable, Callable, Coroutine, Iterable
from typing import Any

from livekit import rtc
from livekit.agents import Agent, ModelSettings, RunContext, StopResponse, stt
from livekit.agents import llm as lk_llm
from livekit.agents.llm import LLM, ToolError, ToolResult, function_tool

from src.agent.interruption import build_ignore_keys, drop_backchannel_transcripts
from src.config import DynamicVariables
from src.db.models import CallOutcome
from src.fsm.events import FSMError
from src.fsm.machine import ConversationFSM
from src.fsm.states import DNC_APOLOGY_SPEECH, State, StateMetadata
from src.prompts.manager import PromptManager

logger = logging.getLogger("posivex.agent")

CLOSING_LINE_TIMEOUT_SECS = 20.0

# Phrases that really do indicate a voicemail greeting or phone menu. A recording notice such as
# "this call is being recorded" is deliberately NOT here.
MACHINE_CUES = re.compile(
    r"leave (?:a|your) (?:\w+ )?message|after the (?:tone|beep)|at the (?:tone|beep)|voice ?mail|mailbox"
    r"|press (?:\d|one|two|three|star|pound)|you(?:'ve| have) reached|(?:is|isn't|is not) available"
    r"|unable to take your call|can't (?:come|take)|record your message|not accepting calls",
    re.IGNORECASE,
)

TERMINAL_STATES = frozenset({State.END, State.DNC_CLOSE})
MACHINE_OUTCOMES = frozenset({"voicemail", "automated_menu"})

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
        ignore_terms: Iterable[str] = (),
        hold_replies: bool = False,
        on_gated_turn: Callable[[str], None] | None = None,
    ) -> None:
        self.fsm = fsm
        self._ignore_keys = build_ignore_keys(ignore_terms)
        # While True the LLM does not answer the callee, so the scripted opener is the first and
        # only thing spoken after pickup (answering-machine detection runs during this time).
        self._hold_replies = hold_replies
        self._on_gated_turn = on_gated_turn
        # Verdict of answering-machine detection ("human", "machine-vm", "uncertain", ...).
        self.amd_category: str | None = None
        self._recent_user_text: list[str] = []
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
            max_endpointing_delay=initial_meta.max_endpointing_delay,
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

    def _agent_is_speaking(self) -> bool:
        session = self.active_session
        return session is not None and getattr(session, "agent_state", None) == "speaking"

    async def stt_node(
        self, audio: AsyncIterable[rtc.AudioFrame], model_settings: ModelSettings
    ) -> AsyncIterable[stt.SpeechEvent | str]:
        """Default STT, minus backchannel-only transcripts while the agent is talking."""
        events = Agent.default.stt_node(self, audio, model_settings)
        if not self._ignore_keys:
            return events
        return drop_backchannel_transcripts(events, self._ignore_keys, self._agent_is_speaking)

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

    def open_reply_gate(self) -> None:
        """Let the LLM answer the callee again (called right before the opening line is spoken)."""
        self._hold_replies = False

    async def on_user_turn_completed(self, turn_ctx: lk_llm.ChatContext, new_message: lk_llm.ChatMessage) -> None:
        """Remember what the callee said and, while replies are held, swallow the turn.

        Held turns are still added to the conversation history and the stored transcript, but the
        LLM does not answer them. Otherwise it would write its own opener while answering-machine
        detection is running, and that opener would play together with the scripted one.
        """
        text = (new_message.text_content or "").strip()
        if text:
            self._recent_user_text = (self._recent_user_text + [text])[-3:]
        if not self._hold_replies:
            return
        if text:
            logger.info("Holding reply until the opening line has played (callee said: %r)", text)
            try:
                chat_ctx = self.chat_ctx.copy()
                chat_ctx.add_message(role="user", content=text)
                await self.update_chat_ctx(chat_ctx)
            except Exception:
                logger.exception("Could not add the held turn to the chat context.")
            if self._on_gated_turn is not None:
                try:
                    self._on_gated_turn(text)
                except Exception:
                    logger.exception("Held-turn handler failed.")
        raise StopResponse()

    def _machine_claim_unsupported(self) -> bool:
        """True when the LLM tries to treat the callee as a machine against the AMD verdict.

        Answering-machine detection is the single source of truth for who answered. If it said
        "human" and nothing the callee said sounds like a voicemail or menu, the LLM may not end
        the opener on its own (a recording notice alone is not a machine).
        """
        if self.amd_category != "human" or self.fsm.current_state != State.OPENER:
            return False
        return not MACHINE_CUES.search(" ".join(self._recent_user_text))

    def _reject_unsupported_machine_claim(self) -> ToolError:
        return ToolError(
            "A live person was confirmed on the line, and nothing they said is a voicemail greeting or "
            "phone menu. A notice such as 'this call is being recorded' is not a machine. Do not end the "
            "call. Answer them naturally and ask the owner / point-of-sale role question."
        )

    @function_tool
    async def transition(self, to: str, reason: str = "", context: RunContext = None) -> str | ToolResult:  # type: ignore[assignment]
        """Request a transition to another state in the sales conversation.

        Args:
            to: Target state name ('reach_owner', 'conversation', 'lock_in', 'close', 'dnc_close', 'end')
            reason: Justification based on what the prospect said
        """
        logger.info("Agent requesting transition to '%s' (reason: %s)", to, reason)
        try:
            target = self.fsm.validate_transition(to, reason=reason)
        except FSMError as e:
            allowed = ", ".join(sorted(s.value for s in self.fsm.metadata.allowed_transitions))
            raise ToolError(
                f"{e}. From '{self.fsm.current_state.value}' you may only move to: {allowed or 'nowhere'}."
            ) from e

        if target == State.END and self._machine_claim_unsupported():
            logger.warning("Refusing LLM request to end the opener as a machine; AMD said human.")
            raise self._reject_unsupported_machine_claim()

        # Build the new prompt BEFORE committing the state change, so a failure leaves the FSM and
        # the agent's instructions consistent with each other.
        prepared_prompt: str | None = None
        if target not in TERMINAL_STATES:
            try:
                prepared_prompt = self.prompt_manager.render_prompt(target, self.variables)
            except Exception as e:
                logger.exception("Could not build the prompt for state '%s'; staying in '%s'.",
                                 target.value, self.fsm.current_state.value)
                raise ToolError(
                    "That step is temporarily unavailable. Stay in the current step and keep the "
                    "conversation going."
                ) from e

        new_state = await self.fsm.transition(target, reason=reason)
        try:
            await self._apply_state(new_state, context, prepared_prompt)
        except Exception as e:
            if new_state in TERMINAL_STATES:
                raise
            logger.exception("Applying state '%s' failed; rolling back.", new_state.value)
            self.fsm.revert_last_transition()
            raise ToolError(
                "That step is temporarily unavailable. Stay in the current step and keep the "
                "conversation going."
            ) from e
        await self._emit_event(
            "state_transition",
            {
                "from_state": self.fsm.previous_state.value if self.fsm.previous_state else None,
                "to_state": new_state.value,
                "reason": reason,
                "call_id": self.call_id,
            },
        )
        message = f"Successfully transitioned to state: {new_state.value}"
        if new_state in TERMINAL_STATES:
            # The call is ending: do not ask the LLM for a follow-up reply. An empty reply here used
            # to trigger ~10 s of failing retries while the callee heard nothing.
            return ToolResult(message, reply_required=False)
        return message

    @function_tool
    async def set_call_outcome(self, outcome: str, rationale: str = "") -> str | ToolResult:
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
        if value in MACHINE_OUTCOMES and self._machine_claim_unsupported():
            logger.warning("Refusing LLM outcome '%s' in the opener; AMD said human.", value)
            raise self._reject_unsupported_machine_claim()
        self.fsm.set_outcome(value, rationale=rationale)
        logger.info("Agent recorded call outcome '%s' (%s)", value, rationale)
        await self._emit_event(
            "outcome_recorded",
            {"outcome": value, "reason": rationale, "call_id": self.call_id},
        )
        # Recording an outcome never needs a spoken reply of its own. A transition made in the same
        # turn decides whether the agent speaks next (e.g. the closing line after 'close').
        return ToolResult(f"Outcome recorded: {value}", reply_required=False)

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

    async def _apply_state(
        self,
        new_state: State,
        context: RunContext | None = None,
        prepared_prompt: str | None = None,
    ) -> None:
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
            # The closing line is spoken in the same step as this tool call; let it finish playing
            # before the session closes (and the room, and with it the SIP leg, is deleted).
            if context is not None:
                try:
                    await asyncio.wait_for(context.wait_for_playout(), timeout=CLOSING_LINE_TIMEOUT_SECS)
                except TimeoutError:
                    logger.warning("Closing line did not finish playing within %.0fs.", CLOSING_LINE_TIMEOUT_SECS)
            session = self.active_session
            if session is not None:
                session.shutdown()
            return

        # 3. Conversational state update
        new_prompt = (
            prepared_prompt
            if prepared_prompt is not None
            else self.prompt_manager.render_prompt(new_state, self.variables)
        )
        await self.update_instructions(new_prompt)

        # Dynamically switch LLM: CONVERSATION uses gemini-3.5-flash, others use flash-lite
        if new_state == State.CONVERSATION:
            self.update_options(llm=self.llm_conversation)
        else:
            self.update_options(llm=self.llm_primary)

        # Update endpointing delay for the new state
        session = self.active_session
        if session is not None and hasattr(session, "update_options"):
            session.update_options(
                endpointing_opts={
                    "min_delay": meta.min_endpointing_delay,
                    "max_delay": meta.max_endpointing_delay,
                }
            )
