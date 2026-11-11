"""The scripted opening line: when it starts, and what to do about words said over it."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.agent.call_session.greeting_check import GreetingCheck
from src.agent.call_session.lifecycle import CallLifecycle
from src.agent.call_session.timeline import CallTimeline
from src.agent.call_session.waits import wait_for_caller_quiet
from src.agent.opener import OVERLAP_REPLY_INSTRUCTIONS, needs_reply
from src.agent.persona_agent import CallState, PersonaAgent
from src.config import ConversationConfig

logger = logging.getLogger("posivex.session")

# The opener waits until the callee has been quiet this long.
OPENER_QUIET_SECS = 0.25


class OpenerFlow:
    """Speak the opening line about `opener_delay_seconds` after pickup (it used to wait 5.9 s at the median for
    LiveKit's AMD, which never classified anything). If the callee is mid-sentence at that moment, wait a little for
    them to finish, then speak anyway. The greeting check runs meanwhile and hangs up on a machine, cutting the opener
    short, so a mailbox hears at most a couple of seconds of it.
    """

    def __init__(
        self,
        *,
        session: Any,
        agent: PersonaAgent,
        call_state: CallState,
        lifecycle: CallLifecycle,
        timeline: CallTimeline,
        greeting: GreetingCheck,
        config: ConversationConfig,
        first_message: str,
    ) -> None:
        self._session = session
        self._agent = agent
        self._call_state = call_state
        self._lifecycle = lifecycle
        self._timeline = timeline
        self._greeting = greeting
        self._config = config
        self._first_message = first_message
        self.requested = False  # the opening line has been handed to the session
        self.playing = False
        self.spoken = False  # it played to the end
        self._overlap_finals: list[str] = []  # what the callee said (final transcripts) while it played

    def on_transcript(self, text: str, is_final: bool) -> None:
        if self.playing and is_final:
            self._overlap_finals.append(text)

    async def run(self) -> bool:
        """Speak the opener and reply to an answer given over it. False when the call ended before it finished."""
        delay = self._config.opener_delay_seconds - self._timeline.clock.since_pickup()
        if delay > 0:
            await asyncio.sleep(delay)
        if self._lifecycle.reason:
            return False
        quiet_wait = self._config.opener_quiet_wait_seconds
        quiet = (
            await wait_for_caller_quiet(self._session, quiet_secs=OPENER_QUIET_SECS, max_wait=quiet_wait)
            if quiet_wait > 0
            else True
        )
        if self._lifecycle.reason:
            return False

        # The LLM was held back until now, so this is the first and only opener; from here on it may answer
        # the callee normally. The opener is uninterruptible: an impatient "Hello?" used to cancel it, and
        # the callee then heard nothing for 26 s.
        logger.info("Speaking first message: '%s'", self._first_message)
        self._agent.open_reply_gate()
        self.requested = True
        self.playing = True
        self._timeline.mark("opener_started", waited_for_quiet=quiet)
        speech_handle = self._session.say(self._first_message, allow_interruptions=False)
        await speech_handle.wait_for_playout()
        self.playing = False
        if self._lifecycle.reason:
            return False
        self.spoken = True
        self._timeline.mark("opener_finished")
        self._session.reset_away_timer()
        self._reply_to_overlap()
        return True

    def _reply_to_overlap(self) -> None:
        """The callee may have answered over the opener ("Yes, this is the owner"). LiveKit skipped that turn, so
        answer it now, unless it was only "Hello?", an echo of the opener, or the greeting check has not confirmed
        a person (a voicemail greeting must never get a reply).
        """
        overlap = " ".join(t for t in self._overlap_finals if needs_reply(t, self._first_message))
        if overlap and self._greeting.allows_reply_to_overlap():
            self._call_state.user_turns += 1
            self._timeline.mark("replying_to_overlap", words=len(overlap.split()))
            self._session.generate_reply(user_input=overlap, instructions=OVERLAP_REPLY_INSTRUCTIONS)
