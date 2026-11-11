"""PersonaAgent: one system prompt, three tools (end_call, skip_turn, voicemail_detection)."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterable, Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from typing import Any

from livekit import rtc
from livekit.agents import Agent, ModelSettings, RunContext, StopResponse, stt
from livekit.agents import llm as lk_llm
from livekit.agents.llm import ToolError, function_tool

from src.agent.interruption import build_ignore_keys, drop_backchannel_transcripts

logger = logging.getLogger("posivex.agent")

# Spoken verbatim when someone asks not to be called again (matches the ElevenLabs production agent).
DNC_APOLOGY_SPEECH = "Understood, my apologies. I'll make sure we don't call this number again. Take care."

# Phrases that really do indicate a voicemail greeting or phone menu. A recording notice such as
# "this call is being recorded" is deliberately NOT here.
MACHINE_CUES = re.compile(
    r"leave (?:a|your) (?:\w+ )?message|after the (?:tone|beep)|at the (?:tone|beep)|voice ?mail|mailbox"
    r"|press (?:\d|one|two|three|star|pound|any)|you(?:'ve| have) reached|(?:is|isn't|is not) available"
    r"|unable to take your call|can't (?:come|take)|record your message|not accepting calls"
    r"|hold while we connect|currently closed|office hours|state your name|number you have dialed"
    r"|person you are trying to reach|text us at",
    re.IGNORECASE,
)

# The prompt's mandated closing lines ("Thanks, talk soon.", "I've got everything noted for the owner.",
# the do-not-call apology) and common goodbyes. Gemini sometimes speaks the line but forgets to call
# end_call, so the session handler hangs up shortly after one of these ends a turn.
CLOSING_CUES = re.compile(
    r"talk soon|noted for the owner|call this number again|take care[.!]?\s*$"
    r"|have a (?:great|good|wonderful|nice|lovely)(?: rest of your)? (?:day|one|evening|weekend|afternoon)"
    r"|good ?bye|bye now",
    re.IGNORECASE,
)


def is_closing_line(text: str) -> bool:
    """True when an agent message ends with a goodbye (only the tail is checked)."""
    return bool(CLOSING_CUES.search((text or "").strip()[-90:]))


# end_call reasons that are allowed without a spoken line in the same turn.
SILENT_END_REASONS = frozenset({"voicemail", "silence", "no_answer", "do_not_call"})

# The voicemail guard only applies while the call is young (a recording notice at the very start).
MACHINE_GUARD_MAX_USER_TURNS = 3

EventHandler = Callable[[str, dict[str, Any]], None] | Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]
EndCallHandler = Callable[[str, bool], None]


@dataclass
class CallState:
    """Facts about the live call that the tools and the session handler share."""

    # What the greeting check decided about whoever answered: human | voicemail | menu | unclear (None: not yet).
    greeting_verdict: str | None = None
    user_turns: int = 0
    recent_user_text: list[str] = field(default_factory=list)
    voicemail_detected: bool = False
    # What the voicemail_detection tool heard: "voicemail" (a mailbox) or "menu" (phone menu, screener, system message).
    machine_kind: str = "voicemail"
    do_not_call: bool = False
    end_reason: str | None = None
    hold_until: float = 0.0  # time.monotonic() deadline set by skip_turn

    @property
    def is_holding(self) -> bool:
        return time.monotonic() < self.hold_until

    def start_hold(self, seconds: float) -> None:
        self.hold_until = time.monotonic() + seconds

    def release_hold(self) -> None:
        self.hold_until = 0.0


class PersonaAgent(Agent):
    """Amelia or Michael: all behavior lives in the system prompt; tools only act on the call."""

    def __init__(
        self,
        *,
        instructions: str,
        call_state: CallState,
        on_end_call: EndCallHandler,
        call_id: str | None = None,
        event_handler: EventHandler | None = None,
        ignore_terms: Iterable[str] = (),
        hold_duration_seconds: float = 30.0,
        hold_replies: bool = False,
        on_gated_turn: Callable[[str], None] | None = None,
    ) -> None:
        self.call_state = call_state
        self._on_end_call = on_end_call
        self.call_id = call_id
        self.event_handler = event_handler
        self._ignore_keys = build_ignore_keys(ignore_terms)
        self._hold_duration_seconds = hold_duration_seconds
        # While True the LLM does not answer the callee, so the scripted opener is the first and
        # only thing spoken after pickup.
        self._hold_replies = hold_replies
        self._on_gated_turn = on_gated_turn
        self._generation_text = ""
        self._end_call_warned = False
        super().__init__(instructions=instructions)

    # ---- session access ------------------------------------------------------------------------
    @property
    def active_session(self) -> Any | None:
        """The active AgentSession, or None before the agent is attached to one."""
        try:
            return self.session
        except RuntimeError:
            return None

    def _agent_is_speaking(self) -> bool:
        session = self.active_session
        return session is not None and getattr(session, "agent_state", None) == "speaking"

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

    # ---- pipeline nodes ------------------------------------------------------------------------
    async def stt_node(
        self, audio: AsyncIterable[rtc.AudioFrame], model_settings: ModelSettings
    ) -> AsyncIterable[stt.SpeechEvent | str]:
        """Default STT, minus backchannel-only transcripts while the agent is talking."""
        events = Agent.default.stt_node(self, audio, model_settings)
        if not self._ignore_keys:
            return events
        return drop_backchannel_transcripts(events, self._ignore_keys, self._agent_is_speaking)

    async def llm_node(
        self,
        chat_ctx: lk_llm.ChatContext,
        tools: list[lk_llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[lk_llm.ChatChunk | str | Any]:
        """Default LLM node, remembering what the model says in this turn (used by the end_call guard)."""
        self._generation_text = ""
        async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
            if isinstance(chunk, str):
                self._generation_text += chunk
            else:
                delta = getattr(chunk, "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
                if content:
                    self._generation_text += content
            yield chunk

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
            self.call_state.user_turns += 1
            self.call_state.recent_user_text = (self.call_state.recent_user_text + [text])[-3:]
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

    # ---- guards --------------------------------------------------------------------------------
    def _machine_claim_unsupported(self) -> bool:
        """True when the LLM tries to treat the callee as a machine against the greeting check's verdict.

        If the greeting check said "human" and nothing the callee said sounds like a voicemail greeting
        or menu, the model may not hang up early in the call (a recording notice alone is not a machine).
        """
        state = self.call_state
        if state.greeting_verdict != "human" or state.user_turns > MACHINE_GUARD_MAX_USER_TURNS:
            return False
        return not MACHINE_CUES.search(" ".join(state.recent_user_text))

    # ---- tools ---------------------------------------------------------------------------------
    @function_tool
    async def end_call(self, reason: str = "conversation_complete", context: RunContext = None) -> None:  # type: ignore[assignment]
        """End the call. Use it in the same turn as the final closing line, or right after the Do Not Call apology. Never use it before the closing line has been spoken.

        Args:
            reason: Why the call ends: 'conversation_complete', 'do_not_call' or 'silence'
        """
        value = (reason or "conversation_complete").strip().lower().replace(" ", "_")
        spoke = bool(self._generation_text.strip())
        if not spoke and value not in SILENT_END_REASONS and not self._end_call_warned:
            # Give the model one chance to say its closing line first; a second silent attempt goes through.
            self._end_call_warned = True
            logger.warning("end_call('%s') without a spoken line; asking the model to say its closing line.", value)
            raise ToolError("Say your short closing line out loud, then use end_call in that same turn.")

        session = context.session if context is not None else self.active_session
        if value == "do_not_call":
            self.call_state.do_not_call = True
            if "call this number again" not in self._generation_text.lower() and session is not None:
                logger.info("Do-not-call: speaking the apology line.")
                handle = session.say(DNC_APOLOGY_SPEECH, allow_interruptions=False)
                await handle.wait_for_playout()

        self.call_state.end_reason = value
        logger.info("Agent ending the call (reason: %s)", value)
        await self._emit_event("end_call", {"reason": value, "call_id": self.call_id})

        # Hang up once the closing line (this turn's speech) has finished playing.
        if context is not None:
            context.speech_handle.add_done_callback(lambda _h: self._on_end_call(value, True))
        else:
            self._on_end_call(value, True)
        return None

    @function_tool
    async def voicemail_detection(  # type: ignore[assignment]
        self, reason: str = "voicemail", kind: str = "voicemail", context: RunContext = None
    ) -> None:
        """Call this immediately, without speaking, when the call is answered by a voicemail greeting, answering machine, recorded or carrier message, call screener or automated menu. Pass a reason of three to five words. Never use it for a live person.

        Args:
            reason: Three to five words, for example 'voicemail: leave a message'
            kind: 'voicemail' for a mailbox or answering machine; 'menu' for a phone menu, call screener, hold or transfer recording, or a carrier or system message
        """
        if self._machine_claim_unsupported():
            logger.warning("Refusing voicemail_detection(%r); the greeting check said human and no machine cue was heard.", reason)
            raise ToolError(
                "A live person was confirmed on the line, and nothing they said is a voicemail greeting or "
                "phone menu. A notice such as 'this call is being recorded' is not a machine. Do not end the "
                "call. Answer them naturally and ask whether they are the owner."
            )
        self.call_state.voicemail_detected = True
        self.call_state.machine_kind = "menu" if (kind or "").strip().lower() == "menu" else "voicemail"
        self.call_state.end_reason = "voicemail_detected"
        logger.info("Voicemail detected by the agent (%s)", reason)
        await self._emit_event("voicemail_detected", {"reason": reason, "call_id": self.call_id})
        self._on_end_call("voicemail_detected", False)
        return None

    @function_tool
    async def skip_turn(self, reason: str = "Merchant requested hold") -> None:
        """Use it, after a brief acknowledgment, when a live person says "hold on", "one second", or goes to get someone. Stay silent until they speak again.

        Args:
            reason: Why the hold was requested (e.g. 'checking with owner', 'one second')
        """
        logger.info("Agent requested skip_turn/hold (reason: %s)", reason)
        self.call_state.start_hold(self._hold_duration_seconds)
        await self._emit_event("hold_requested", {"reason": reason, "call_id": self.call_id})
        # Returning None completes the tool without a follow-up reply, so the agent stays quiet.
        return None
