"""One call, start to finish: the pieces of a call wired to the LiveKit session and run in order.

`CallSession` does no work of its own. It builds the collaborators (timeline, transcript, lifecycle, silence
control, greeting check, opener flow, finalizer), routes the session's events to them, and runs the sequence:
wait for the callee, wait for pickup, speak the opener, keep watching for silence until the call ends.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from livekit import rtc
from sqlalchemy.ext.asyncio import AsyncEngine

from src.agent.call_session.finalizer import CallFinalizer, OutcomeClassifier, PhoneLeg
from src.agent.call_session.greeting_check import GreetingCheck
from src.agent.call_session.lifecycle import CallLifecycle
from src.agent.call_session.opening import OpenerFlow
from src.agent.call_session.silence_control import SilenceControl
from src.agent.call_session.tasks import BackgroundTasks
from src.agent.call_session.telemetry import Telemetry
from src.agent.call_session.timeline import CallTimeline
from src.agent.call_session.transcript import TranscriptBuffer
from src.agent.call_session.waits import wait_for_answer
from src.agent.persona_agent import CallState, PersonaAgent
from src.agent.room_options import build_room_options
from src.analysis import CallSignals
from src.config import CallRequest, Settings
from src.telephony.recording import start_call_recording

logger = logging.getLogger("posivex.session")


@dataclass(frozen=True)
class CallTimings:
    """Waits that are tuned in one place (and shortened by tests)."""

    closing_grace_secs: float  # after an agent goodbye, before the call is ended if the model did not call end_call
    participant_join_timeout_secs: float  # how long the job waits for the callee to appear in the room


class CallSession:
    def __init__(
        self,
        *,
        ctx: Any,
        session: Any,
        call_request: CallRequest,
        settings: Settings,
        timeline: CallTimeline,
        engine: AsyncEngine | None,
        instructions: str,
        first_message: str,
        classifier: Any | None,
        classify_outcome: OutcomeClassifier,
        silence_watch: Callable[..., Coroutine[Any, Any, None]],
        timings: CallTimings,
    ) -> None:
        self._ctx = ctx
        self._session = session
        self._call_request = call_request
        self._settings = settings
        self._timeline = timeline
        self._timings = timings
        config = settings.conversation

        self.call_state = CallState()
        self.signals = CallSignals()
        self._leg = PhoneLeg()
        self._tasks = BackgroundTasks()
        self._telemetry = Telemetry(ctx)
        self.transcript = TranscriptBuffer(timeline.clock.since_pickup)
        self.lifecycle = CallLifecycle(
            session=session,
            call_state=self.call_state,
            signals=self.signals,
            tasks=self._tasks,
            closing_grace_secs=timings.closing_grace_secs,
            max_duration_secs=config.max_duration_seconds,
        )
        self.greeting = GreetingCheck(
            classifier=classifier,
            session=session,
            call_state=self.call_state,
            lifecycle=self.lifecycle,
            timeline=timeline,
            signals=self.signals,
            tasks=self._tasks,
            config=config,
        )
        # The agent id lead-gen dialed with (or the persona's own) is echoed back in the post-call webhook.
        agent_entry = settings.agent_for_id(call_request.agent_id) or settings.agent_for_persona(call_request.persona)
        self.agent = PersonaAgent(
            instructions=instructions,
            call_state=self.call_state,
            on_end_call=lambda reason, drain: self.lifecycle.end(reason, drain=drain),
            call_id=timeline.call_id,
            event_handler=self._on_agent_event,
            ignore_terms=config.interruption_ignore_terms,
            hold_duration_seconds=config.hold_duration_seconds,
            # The LLM stays quiet until the scripted opening line is spoken (see OpenerFlow).
            hold_replies=True,
            on_gated_turn=self.transcript.add_held_turn,
        )
        self.opener = OpenerFlow(
            session=session,
            agent=self.agent,
            call_state=self.call_state,
            lifecycle=self.lifecycle,
            timeline=timeline,
            greeting=self.greeting,
            config=config,
            first_message=first_message,
        )
        self.silence = SilenceControl(
            session=session,
            call_state=self.call_state,
            lifecycle=self.lifecycle,
            timeline=timeline,
            tasks=self._tasks,
            config=config,
            opener_spoken=lambda: self.opener.spoken,
            watch=silence_watch,
        )
        self._finalizer = CallFinalizer(
            session=session,
            call_state=self.call_state,
            lifecycle=self.lifecycle,
            timeline=timeline,
            transcript=self.transcript,
            signals=self.signals,
            leg=self._leg,
            engine=engine,
            webhook_destination=settings.webhook_destination(),
            agent_id=call_request.agent_id or agent_entry.agent_id,
            agent_name=agent_entry.name,
            classify_outcome=classify_outcome,
        )
        self._first_speech_marked = False

    # ---- the call, in order --------------------------------------------------------------------
    async def run(self) -> None:
        ctx, session, lifecycle = self._ctx, self._session, self.lifecycle
        self._register_events()
        self.greeting.warm()
        ctx.add_shutdown_callback(self._on_shutdown)
        lifecycle.start_duration_guard()

        room_options, noise_cancellation_on = build_room_options(self._settings.conversation.noise_cancellation)
        logger.info("Noise cancellation: %s", self._settings.conversation.noise_cancellation if noise_cancellation_on else "off")
        await session.start(agent=self.agent, room=ctx.room, room_options=room_options)
        await self._telemetry.publish(
            "session_init",
            {
                "call_id": self._timeline.call_id,
                "persona": self._call_request.persona,
                "active_model": self._settings.llm.default_model,
                "noise_cancellation": noise_cancellation_on,
                "dynamic_variables": self._call_request.dynamic_variables.model_dump(),
            },
        )

        callee = await self._wait_for_callee()
        if callee is None or not await self._wait_for_pickup(callee):
            return
        if await self.opener.run():
            self.silence.start_watchdog()

    async def _wait_for_callee(self) -> Any | None:
        """The callee (SIP participant or browser tester) joining the room. None when nobody does."""
        logger.info("Waiting for callee to join the room...")
        try:
            return await asyncio.wait_for(self._ctx.wait_for_participant(), self._timings.participant_join_timeout_secs)
        except Exception as e:
            logger.warning("No callee joined: %s", e)
            self.signals.forced_outcome, self.signals.forced_rationale = "no_answer", "No participant joined the room."
            self.lifecycle.end("no_participant", drain=False)
            return None

    async def _wait_for_pickup(self, callee: Any) -> bool:
        """For a phone call, wait until it is answered. False when it was not (the call has been ended)."""
        is_sip = callee.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP or callee.identity.startswith("phone_")
        self._leg.sip = is_sip
        if not is_sip:
            logger.info("Non-SIP participant joined (%s).", callee.identity)
            self._register_pickup(is_sip)
            return True

        self._session.room_io.set_participant(callee.identity)
        answer = await wait_for_answer(
            callee,
            self._settings.conversation.ring_timeout_seconds,
            is_gone=lambda: callee.identity not in self._ctx.room.remote_participants,
        )
        if answer != "active":
            logger.warning("Callee did not answer (%s).", answer)
            self.signals.forced_outcome, self.signals.forced_rationale = "no_answer", f"Call not answered ({answer})."
            self.lifecycle.end("no_answer", drain=False)
            return False
        logger.info("SIP callee answered (%s).", callee.identity)
        self._register_pickup(is_sip)
        return True

    def _register_pickup(self, is_sip: bool) -> None:
        """The callee is on the line: start the call clock, the recording and the greeting check."""
        ring_secs = self._timeline.clock.pick_up()
        self._leg.answered = True
        self.silence.reset_activity()
        self._timeline.mark("pickup", ring_secs=round(ring_secs, 2), sip=is_sip)
        self._tasks.spawn(self._begin_recording())
        self.greeting.start()

    async def _begin_recording(self) -> None:
        """Record the call to the configured bucket; never allowed to disturb the call itself."""
        timeline = self._timeline
        info = await start_call_recording(self._ctx.api, self._ctx.room.name, timeline.call_id, self._settings.recording)
        if info:
            await timeline.persist("recording", timeline.recorder.record_recording(timeline.call_id, info))

    async def _on_shutdown(self) -> None:
        self.lifecycle.stop()
        self.silence.stop_watchdog()
        self.greeting.stop()
        await self._finalizer.run()

    # ---- the session's events ------------------------------------------------------------------
    def _register_events(self) -> None:
        self._session.on("conversation_item_added", self._on_item)
        self._session.on("user_state_changed", self._on_user_state)
        self._session.on("agent_state_changed", self._on_agent_state)
        self._session.on("user_input_transcribed", self._on_transcribed)
        self._session.on("close", self._on_close)

    def _on_item(self, ev: Any) -> None:
        if self.transcript.add_item(ev):
            self.lifecycle.watch_for_closing()

    def _on_user_state(self, ev: Any) -> None:
        new_state = str(getattr(ev, "new_state", "unknown"))
        self.silence.on_user_state(new_state)
        self._tasks.spawn(self._telemetry.publish("user_state_changed", {"user_state": new_state}))

    def _on_agent_state(self, ev: Any) -> None:
        new_state = str(getattr(ev, "new_state", "unknown"))
        self.silence.on_agent_state(str(getattr(ev, "old_state", "")), new_state)
        self._tasks.spawn(self._telemetry.publish("agent_state_changed", {"agent_state": new_state}))

    def _on_transcribed(self, ev: Any) -> None:
        text = str(getattr(ev, "transcript", "") or "").strip()
        if not text:
            return
        is_final = bool(getattr(ev, "is_final", False))
        self.silence.note_heard()
        if not self._first_speech_marked and self._timeline.clock.picked_up:
            self._first_speech_marked = True
            self._timeline.mark("first_callee_speech", opener_requested=self.opener.requested)
        self.opener.on_transcript(text, is_final)
        self.greeting.on_transcript(text, is_final)

    def _on_close(self, ev: Any) -> None:
        self.lifecycle.session_closed(ev)
        # Release the job so shutdown callbacks (finalize + webhook outbox) run promptly.
        self._ctx.shutdown(reason="session closed")

    async def _on_agent_event(self, event_type: str, payload: dict[str, Any]) -> None:
        timeline = self._timeline
        await timeline.persist(
            f"event:{event_type}",
            timeline.recorder.record_event(call_id=timeline.call_id, event_type=event_type, state=None, payload=payload),
        )
        await self._telemetry.publish(event_type, {"reason": payload.get("reason", "")})
