"""Session lifecycle, greeting check, and AgentServer entrypoint orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    inference,
    room_io,
)
from livekit.agents.metrics.usage import LLMModelUsage, STTModelUsage, TTSModelUsage
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from src.agent.call_clock import CallClock
from src.agent.greeting_detector import (
    GeminiGreetingClassifier,
    GreetingDetector,
    GreetingVerdict,
    machine_outcome,
)
from src.agent.opener import OVERLAP_REPLY_INSTRUCTIONS, needs_reply
from src.agent.persona_agent import EARLY_CALL_USER_TURNS, CallState, PersonaAgent, is_closing_line
from src.agent.silence import nudge_and_rearm, watch_for_silence
from src.analysis import CallSignals, classify_transcript, resolve_call_outcome
from src.config import CallRequest, Settings, load_settings
from src.db.base import get_engine, get_sessionmaker
from src.logging import mask_phone, set_call_id
from src.metrics.costs import CostAccumulator
from src.persistence.recorder import CallRecorder
from src.persistence.remote import RemoteRecorder
from src.prompts.manager import PromptManager
from src.providers import (
    create_fallback_llm,
    get_llm,
    get_stt,
    get_tts,
    get_vad,
)
from src.telephony.recording import start_call_recording

logger = logging.getLogger("posivex.session")
load_dotenv()

def parse_call_request_from_room(room_metadata: str | None) -> CallRequest:
    """Parse and validate CallRequest from dispatch/room metadata JSON.

    Raises ValueError when the metadata is missing or invalid, unless ALLOW_DEFAULT_CALL_REQUEST=true
    (local development only), so a malformed dispatch never speaks to a placeholder number.
    """
    allow_default = os.getenv("ALLOW_DEFAULT_CALL_REQUEST", "").lower() == "true"

    if not room_metadata:
        if allow_default:
            logger.warning("Empty metadata; using default CallRequest (ALLOW_DEFAULT_CALL_REQUEST).")
            return CallRequest(phone=DEV_PLACEHOLDER_PHONE)
        raise ValueError("No call metadata supplied with the dispatch or room.")

    try:
        data = json.loads(room_metadata)
        if not isinstance(data, dict):
            raise ValueError("metadata JSON must be an object")
        return CallRequest.model_validate(data)
    except Exception as e:
        # Not the metadata itself or the error text (a validation error repeats the values): they hold the phone number.
        logger.error("Failed to parse call metadata (%s, %d characters): %s", type(e).__name__, len(room_metadata), _describe_parse_error(e))
        if allow_default:
            return CallRequest(phone=DEV_PLACEHOLDER_PHONE)
        raise ValueError(f"Invalid call metadata: {e}") from e


def _describe_parse_error(e: Exception) -> str:
    """Which fields were wrong, without their values."""
    if isinstance(e, ValidationError):
        return ", ".join(".".join(str(part) for part in err["loc"]) + f" ({err['type']})" for err in e.errors())
    return "not valid JSON, or not a JSON object"


def _resolve_raw_metadata(ctx: JobContext) -> str | None:
    """Dispatch (job) metadata is authoritative; fall back to room, then participant metadata."""
    job = getattr(ctx, "job", None)
    if job is not None and getattr(job, "metadata", None):
        return job.metadata
    if ctx.room.metadata:
        return ctx.room.metadata
    if job is not None and getattr(getattr(job, "room", None), "metadata", None):
        return job.room.metadata
    for p in ctx.room.remote_participants.values():
        if p.metadata:
            return p.metadata
    return None


def _usage_into_costs(session: AgentSession, costs: CostAccumulator, duration_secs: float) -> None:
    """Fold LiveKit's per-model session usage into the cost accumulator."""
    try:
        for usage in session.usage.model_usage:
            if isinstance(usage, LLMModelUsage):
                costs.add_llm_usage(
                    usage.model, usage.input_tokens, usage.output_tokens, cached_tokens=usage.input_cached_tokens
                )
                if usage.input_tokens:
                    logger.info(
                        "LLM usage: model=%s input=%d cached=%d (%.0f%%) output=%d",
                        usage.model,
                        usage.input_tokens,
                        usage.input_cached_tokens,
                        usage.input_cached_tokens / usage.input_tokens * 100,
                        usage.output_tokens,
                    )
            elif isinstance(usage, TTSModelUsage):
                costs.add_tts_usage(usage.model, usage.characters_count)
            elif isinstance(usage, STTModelUsage):
                costs.add_stt_usage(usage.model, usage.audio_duration)
    except Exception as e:
        logger.warning("Could not read session usage: %s", e)
    costs.add_telephony_usage(duration_secs)


# Grace period after an agent goodbye before the call is ended if the model did not call end_call.
CLOSING_GRACE_SECS = 2.5

# The opener waits until the callee has been quiet this long, checking this often.
OPENER_QUIET_SECS = 0.25
QUIET_POLL_SECS = 0.05

# How often the SIP call status is read while the phone rings.
ANSWER_POLL_SECS = 0.5

# The callee (the SIP participant, or a browser tester) joins the room within seconds of the dial request. If nobody
# has by then, the call is reported as not answered instead of the job waiting in an empty room.
PARTICIPANT_JOIN_TIMEOUT_SECS = 60.0

# Shutdown callbacks (transcripts, costs, outcome, the report to the API) must finish before the job is killed;
# LiveKit's default is 10 seconds. Worst case: the outcome classifier (20 s) plus the report to the API (its 60 s
# retry budget plus one 10 s request), with margin. Only a down API takes this long.
SHUTDOWN_PROCESS_TIMEOUT_SECS = 120.0

# Stand-in number used only with ALLOW_DEFAULT_CALL_REQUEST=true (local development).
DEV_PLACEHOLDER_PHONE = "+14045550000"

# How much of the callee's words is kept in a rationale.
RATIONALE_TEXT_CHARS = 200


def _prewarmed_vad(ctx: JobContext, settings: Settings) -> Any | None:
    """VAD loaded during process warm-up, if its settings match this call's."""
    try:
        cached = ctx.proc.userdata.get("vad")
    except Exception:
        logger.debug("No prewarmed VAD available; it will be loaded for this call.", exc_info=True)
        return None
    if not cached:
        return None
    cached_config, vad_instance = cached
    return vad_instance if cached_config == settings.vad.model_dump() else None


def prewarm_process(proc: JobProcess) -> None:
    """Runs once in each job process before it receives a call.

    Moves slow one-time work (imports, TLS setup, loading the VAD model) out of the live call,
    where it blocked the event loop for hundreds of milliseconds and delayed the first audio.
    """
    import ssl

    import asyncpg  # noqa: F401
    import httpcore  # noqa: F401
    import sqlalchemy.dialects.postgresql  # noqa: F401

    ssl.create_default_context()
    settings = _configured_settings or load_settings()
    try:
        proc.userdata["vad"] = (settings.vad.model_dump(), get_vad(settings.vad))
    except Exception:
        logger.exception("Could not prewarm the VAD model; it will be loaded per call.")


async def _wait_for_answer(
    callee: rtc.RemoteParticipant, timeout: float, is_gone: Callable[[], bool] = lambda: False
) -> str:
    """Wait for sip.callStatus to become 'active'.

    Returns 'active', 'hangup', 'disconnected' (the callee left the room while still ringing, for example
    by declining) or 'timeout'. A declined call never publishes a final status, so without `is_gone` this
    kept polling for the whole ring timeout and held the job open after the call was over.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = callee.attributes.get("sip.callStatus")
        if status == "active":
            return "active"
        if status == "hangup":
            return "hangup"
        if is_gone():
            return "disconnected"
        await asyncio.sleep(ANSWER_POLL_SECS)
    return "timeout"


async def _wait_for_caller_quiet(
    session: AgentSession, *, quiet_secs: float = 0.4, max_wait: float = 3.0
) -> bool:
    """Wait until the caller has been silent for `quiet_secs` (True), or `max_wait` passes (False)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    quiet_since: float | None = None
    while loop.time() < deadline:
        if session.user_state == "speaking":
            quiet_since = None
        else:
            now = loop.time()
            quiet_since = quiet_since if quiet_since is not None else now
            if now - quiet_since >= quiet_secs:
                return True
        await asyncio.sleep(QUIET_POLL_SECS)
    return False


def build_turn_handling(settings: Settings) -> TurnHandlingOptions:
    """Turn-taking options for AgentSession: end-of-turn strategy, endpointing and interruptions."""
    turn_handling: TurnHandlingOptions = {
        "endpointing": {
            "min_delay": settings.conversation.min_endpointing_delay,
            "max_delay": settings.conversation.max_endpointing_delay,
        },
        # A transcribed word is required before the caller can interrupt, which is what lets
        # PersonaAgent.stt_node swallow "mm-hmm" style acknowledgements.
        # By default LiveKit replaces the callee's audio with silence while uninterruptible speech (our opener)
        # plays, so their first words were never transcribed and the greeting check was blind for ~7 s. Keep it
        # flowing; a turn that finishes during the opener is still skipped by LiveKit, not answered twice.
        "interruption": {
            "min_words": settings.conversation.interruption_min_words,
            "discard_audio_if_uninterruptible": False,
        },
    }
    mode = settings.turn_detector.mode
    if mode == "vad":
        turn_handling["turn_detection"] = "vad"
    elif mode == "stt-only":
        turn_handling["turn_detection"] = "stt"
    else:
        # "v1" or "v1-mini": the audio turn detector (reads the caller's audio). VAD is still used for speech
        # start/stop and interruptions. "v1" falls back to the local "v1-mini" on errors or when the LiveKit
        # Inference allowance is exhausted.
        turn_handling["turn_detection"] = inference.TurnDetector(version=mode)
    return turn_handling


def _settings_for_call(base: Settings | None, call_request: CallRequest) -> Settings:
    """Reuse the worker's settings unless the call needs a different persona or has overrides."""
    if base is None or call_request.config_overrides or base.persona != call_request.persona:
        return load_settings(persona=call_request.persona, overrides=call_request.config_overrides)
    return base


async def run_agent_session(
    ctx: JobContext,
    settings: Settings | None = None,
    recorder: CallRecorder | RemoteRecorder | None = None,
) -> None:
    """Core entrypoint execution flow for an individual call session."""
    # Ensure room connection is established so room and remote participants are available
    await ctx.connect()

    try:
        call_request = parse_call_request_from_room(_resolve_raw_metadata(ctx))
    except ValueError as e:
        logger.error("Refusing to start session in room %s: %s", ctx.room.name, e)
        ctx.shutdown(reason="invalid call metadata")
        return

    call_id = call_request.call_id or f"call_{ctx.room.name}"
    set_call_id(call_id)
    ctx.log_context_fields = {"call_id": call_id, "room": ctx.room.name}

    logger.info(
        "Starting agent session for call_id=%s, phone=%s, persona=%s, language=%s",
        call_id,
        mask_phone(call_request.phone),
        call_request.persona,
        call_request.language,
    )

    resolved_settings = await asyncio.to_thread(_settings_for_call, settings, call_request)
    # Call time is counted from pickup, as ElevenLabs does, so transcript times and the duration lead-gen shows
    # do not include 5 to 75 s of ringing. The job start only tells how long it rang.
    clock = CallClock()
    since_pickup = clock.since_pickup

    # Persistence: the worker owns its engine (asyncpg pools are event-loop bound) and writes
    # best-effort so a database problem can never take down a live call. An agent hosted on LiveKit
    # Cloud cannot reach the database, so it reports the finished call to the API instead.
    engine: AsyncEngine | None = None
    if recorder is None:
        if resolved_settings.results_callback_url:
            recorder = RemoteRecorder(resolved_settings.results_callback_url, resolved_settings.results_callback_secret)
        else:
            engine = get_engine(resolved_settings.database_url)
            recorder = CallRecorder(get_sessionmaker(engine))

    # The agent id lead-gen dialed with (or the persona's own) is echoed back in the post-call webhook.
    agent_entry = resolved_settings.agent_for_id(call_request.agent_id) or resolved_settings.agent_for_persona(
        call_request.persona
    )
    reported_agent_id = call_request.agent_id or agent_entry.agent_id

    async def persist(label: str, coro: Awaitable[Any]) -> Any:
        try:
            return await coro
        except Exception as e:
            logger.error("Persistence step '%s' failed: %s", label, e, exc_info=True)
            return None

    cost_accumulator = CostAccumulator()
    # The API normally created the row at dispatch time; browser/test calls have none yet.
    await persist(
        "ensure_call",
        recorder.ensure_call(
            call_id=call_id,
            phone=call_request.phone,
            persona=call_request.persona,
            language=call_request.language,
            lead_id=call_request.lead_id,
            campaign_id=call_request.campaign_id,
            metadata_json={"dynamic_variables": call_request.dynamic_variables.model_dump()},
        ),
    )

    # Initialize components via provider registry
    stt_provider = get_stt(resolved_settings.stt, api_key=resolved_settings.deepgram_api_key)
    vad_provider = _prewarmed_vad(ctx, resolved_settings) or get_vad(resolved_settings.vad)
    tts_provider = get_tts(resolved_settings.tts, api_key=resolved_settings.elevenlabs_api_key)

    # Primary and fallback Gemini models. Building a Gemini client loads the CA bundle (~300 ms of CPU),
    # so it runs in a thread instead of stalling the event loop at call start.
    def _build_llms() -> tuple[Any, Any]:
        key = resolved_settings.gemini_api_key
        return (
            get_llm(resolved_settings.llm.default_model, resolved_settings.llm, api_key=key),
            get_llm(resolved_settings.llm.fallback_model, resolved_settings.llm, api_key=key),
        )

    llm_primary_base, llm_fallback_base = await asyncio.to_thread(_build_llms)

    llm_primary = create_fallback_llm(llm_primary_base, llm_fallback_base, resolved_settings.llm)

    call_state = CallState()
    prompt_manager = PromptManager(persona=call_request.persona, kb_config=resolved_settings.kb)

    background_tasks: set[asyncio.Task[Any]] = set()

    def spawn(coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    async def begin_recording() -> None:
        """Record the call to the configured bucket; never allowed to disturb the call itself."""
        info = await start_call_recording(ctx.api, ctx.room.name, call_id, resolved_settings.recording)
        if info:
            await persist("recording", recorder.record_recording(call_id, info))

    async def broadcast_telemetry(event_type: str, payload: dict[str, Any]) -> None:
        """Broadcast real-time state and metrics to the frontend over WebRTC data channel."""
        try:
            if ctx.room and ctx.room.local_participant:
                msg = json.dumps({
                    "type": event_type,
                    "timestamp": time.time(),
                    **payload,
                })
                await ctx.room.local_participant.publish_data(
                    msg.encode("utf-8"),
                    reliable=True,
                    topic="posivex_telemetry",
                )
        except Exception as e:
            logger.debug("Failed to publish telemetry to room: %s", e)

    async def handle_agent_event(event_type: str, payload: dict[str, Any]) -> None:
        await persist(
            f"event:{event_type}",
            recorder.record_event(call_id=call_id, event_type=event_type, state=None, payload=payload),
        )
        await broadcast_telemetry(event_type, {"reason": payload.get("reason", "")})

    event_tasks: set[asyncio.Task[Any]] = set()

    def mark(name: str, **extra: Any) -> None:
        """Log and store a timing event with its time since pickup, for call analysis after the fact.

        The agent's logs disappear when the worker scales to zero; these events travel with the call.
        """
        secs = round(since_pickup(), 2)
        logger.info("timing %s at +%.2fs since pickup %s", name, secs, extra or "")
        task = asyncio.create_task(
            persist(
                f"event:timing_{name}",
                recorder.record_event(
                    call_id=call_id, event_type=f"timing_{name}", state=None, payload={"since_pickup_secs": secs, **extra}
                ),
            )
        )
        event_tasks.add(task)
        task.add_done_callback(event_tasks.discard)

    transcript_buffer: list[dict[str, Any]] = []

    def record_held_turn(text: str) -> None:
        """Store what the callee said before the opening line (the LLM does not answer it)."""
        transcript_buffer.append({
            "role": "user",
            "message": text,
            "time_in_call_secs": since_pickup(),
            "interrupted": False,
        })

    agent = PersonaAgent(
        instructions=await asyncio.to_thread(prompt_manager.render_prompt, call_request.dynamic_variables),
        call_state=call_state,
        on_end_call=lambda reason, drain: end_call(reason, drain=drain),
        call_id=call_id,
        event_handler=handle_agent_event,
        ignore_terms=resolved_settings.conversation.interruption_ignore_terms,
        hold_duration_seconds=resolved_settings.conversation.hold_duration_seconds,
        # The LLM stays quiet until the scripted opening line is spoken (see below).
        hold_replies=True,
        on_gated_turn=record_held_turn,
    )

    conv_cfg = resolved_settings.conversation
    session = AgentSession(
        stt=stt_provider,
        llm=llm_primary,
        tts=tts_provider,
        vad=vad_provider,
        user_away_timeout=conv_cfg.silence_end_call_seconds,
        turn_handling=build_turn_handling(resolved_settings),
    )

    # ---- Transcript capture -------------------------------------------------------------------
    def on_conversation_item(ev: Any) -> None:
        item = getattr(ev, "item", None)
        if item is None or getattr(item, "type", None) != "message":
            return
        role = {"user": "user", "assistant": "agent"}.get(item.role)
        text = item.text_content
        if role is None or not text:
            return
        interrupted = bool(getattr(item, "interrupted", False))
        transcript_buffer.append({
            "role": role,
            "message": text,
            "time_in_call_secs": since_pickup(),
            "interrupted": interrupted,
        })
        if role == "agent" and not interrupted and is_closing_line(text):
            watch_for_closing()

    session.on("conversation_item_added", on_conversation_item)

    # ---- Safety net for a goodbye without end_call --------------------------------------------
    closing_task: dict[str, asyncio.Task[Any] | None] = {"task": None}

    async def hang_up_after_goodbye() -> None:
        await asyncio.sleep(CLOSING_GRACE_SECS)
        if termination["reason"]:
            return  # end_call (or another path) is already closing the call; nothing to rescue
        if session.user_state == "speaking" or session.agent_state == "speaking":
            return
        logger.info("Goodbye was spoken without end_call; ending the call.")
        call_state.end_reason = call_state.end_reason or "closing_line_detected"
        end_call("closing_line_detected")

    def cancel_closing_watch() -> None:
        task = closing_task["task"]
        if task is not None and not task.done():
            task.cancel()
        closing_task["task"] = None

    def watch_for_closing() -> None:
        cancel_closing_watch()
        task = asyncio.create_task(hang_up_after_goodbye())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        closing_task["task"] = task

    # ---- Termination bookkeeping --------------------------------------------------------------
    termination: dict[str, str] = {"reason": ""}

    def end_call(reason: str, *, drain: bool = True) -> None:
        if not termination["reason"]:
            termination["reason"] = reason
        session.shutdown(drain=drain)

    # ---- Silence handling (user_away_timeout -> user_state_changed 'away') --------------------
    # Like the reference ElevenLabs agent: silence ends the call, except while the caller asked us to
    # hold (skip_turn). Before the caller has said anything, the agent gets one casual "Hello?" first.
    hello_sent = False
    opener_spoken = False

    async def handle_away() -> None:
        nonlocal hello_sent
        if not opener_spoken or call_state.is_holding:
            # Still ringing / detecting an answering machine, or the caller asked us to hold.
            session.reset_away_timer()
            return
        if call_state.user_turns == 0 and not hello_sent:
            hello_sent = True
            mark("silence_nudge")
            await nudge_and_rearm(session)
            return
        logger.info("Caller silent for %.0fs; ending the call.", conv_cfg.silence_end_call_seconds)
        call_state.end_reason = call_state.end_reason or "silence"
        end_call("silence_timeout", drain=False)

    # Last time anything was heard from the callee, and last time the agent finished speaking. The watchdog
    # below uses them, not VAD, so hold music or a looping recording (VAD "speaking", no words) still times out.
    activity = {"heard": time.monotonic(), "agent_done": time.monotonic()}
    first_speech = {"marked": False}
    opener_state = {"requested": False, "playing": False}
    overlap_finals: list[str] = []  # what the callee said (final transcripts) while the opener played

    def on_no_speech(quiet: float) -> None:
        logger.info("No words heard from the callee for %.0fs; ending the call.", quiet)
        mark("no_speech_watchdog", quiet_secs=round(quiet, 1))
        call_state.end_reason = call_state.end_reason or "silence"
        end_call("no_speech_timeout", drain=False)

    def no_speech_watchdog() -> Coroutine[Any, Any, None]:
        return watch_for_silence(
            limit=conv_cfg.no_speech_watchdog_seconds,
            last_activity=lambda: max(activity["heard"], activity["agent_done"]),
            is_paused=lambda: call_state.is_holding or session.agent_state in ("speaking", "thinking"),
            is_over=lambda: bool(termination["reason"]),
            on_timeout=on_no_speech,
        )

    long_turn_task: dict[str, asyncio.Task[Any] | None] = {"task": None}

    async def force_long_turn_answer(limit: float) -> None:
        """Answer when VAD reports one uninterrupted caller turn for `limit` seconds.

        Background noise or off-script chatter can keep VAD in "speaking" for 10+ seconds while the
        transcript is complete, leaving the caller in silence. Committing the turn makes the agent reply
        to what STT has heard so far.
        """
        await asyncio.sleep(limit)
        if session.user_state != "speaking" or session.agent_state not in ("listening", "idle"):
            return
        logger.warning("Caller turn open for %.0fs; committing it so the agent can answer.", limit)
        try:
            await session.commit_user_turn()
        except Exception:
            logger.exception("Could not commit the long caller turn.")

    def handle_user_state_changed(ev: Any) -> None:
        new_state = str(getattr(ev, "new_state", "unknown"))
        previous = long_turn_task["task"]
        if previous is not None and not previous.done():
            previous.cancel()
        long_turn_task["task"] = None
        limit = resolved_settings.conversation.max_user_speech_seconds
        if new_state == "speaking" and limit > 0:
            task = asyncio.create_task(force_long_turn_answer(limit))
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)
            long_turn_task["task"] = task
        if new_state == "away":
            spawn(handle_away())
        elif new_state == "speaking":
            call_state.release_hold()
            cancel_closing_watch()
        spawn(broadcast_telemetry("user_state_changed", {"user_state": new_state}))

    session.on("user_state_changed", handle_user_state_changed)

    def handle_agent_state_changed(ev: Any) -> None:
        new_state = str(getattr(ev, "new_state", "unknown"))
        if str(getattr(ev, "old_state", "")) == "speaking" and new_state != "speaking":
            activity["agent_done"] = time.monotonic()
        spawn(broadcast_telemetry("agent_state_changed", {"agent_state": new_state}))

    session.on("agent_state_changed", handle_agent_state_changed)

    # ---- Greeting check: is it a person, a voicemail or a phone menu? --------------------------
    # Runs beside the opener on the streaming transcript. On a confident machine verdict the agent stops
    # talking and hangs up, and the call gets the matching outcome instead of a guess after the fact.
    def handle_machine(verdict: GreetingVerdict, text: str) -> None:
        if termination["reason"] or call_state.voicemail_detected:
            return
        outcome, reason = machine_outcome(verdict.category)
        signals.forced_outcome = signals.forced_outcome or outcome
        signals.forced_rationale = signals.forced_rationale or (
            f"Greeting check: {verdict.category} ({verdict.confidence:.2f}). {text[:RATIONALE_TEXT_CHARS]}"
        )
        call_state.greeting_verdict = verdict.category
        call_state.end_reason = call_state.end_reason or "machine_detected"
        mark(
            "machine_detected",
            category=verdict.category,
            confidence=round(verdict.confidence, 2),
            model_secs=round(verdict.seconds, 2),
            words=len(text.split()),
        )
        try:
            session.interrupt(force=True)  # the opener is uninterruptible, so force it
        except Exception:
            logger.debug("Could not interrupt speech before hanging up on a machine.", exc_info=True)
        end_call(reason, drain=False)

    def note_verdict(verdict: GreetingVerdict, text: str) -> None:
        mark(
            "greeting_check",
            category=verdict.category,
            confidence=round(verdict.confidence, 2),
            model_secs=round(verdict.seconds, 2),
            words=len(text.split()),
        )
        if verdict.category == "human" and verdict.confidence >= conv_cfg.machine_detection_confidence:
            call_state.greeting_verdict = "human"  # lets the voicemail tool refuse a false "machine" call

    detector: GreetingDetector | None = None
    if conv_cfg.machine_detection_enabled and resolved_settings.gemini_api_key:
        greeting_classifier = GeminiGreetingClassifier(
            resolved_settings.gemini_api_key, conv_cfg.machine_detection_model
        )
        detector = GreetingDetector(
            greeting_classifier.classify,
            handle_machine,
            confidence=conv_cfg.machine_detection_confidence,
            window_seconds=conv_cfg.machine_detection_window_seconds,
            on_verdict=note_verdict,
        )
        # Build the Gemini client now, while the phone is still ringing, so the first check is not delayed.
        spawn(greeting_classifier.warm())

    def handle_user_input_transcribed(ev: Any) -> None:
        text = str(getattr(ev, "transcript", "") or "").strip()
        if not text:
            return
        activity["heard"] = time.monotonic()
        if not first_speech["marked"] and clock.picked_up:
            first_speech["marked"] = True
            mark("first_callee_speech", opener_requested=opener_state["requested"])
        if opener_state["playing"] and bool(getattr(ev, "is_final", False)):
            overlap_finals.append(text)
        if detector is not None:
            if call_state.user_turns >= EARLY_CALL_USER_TURNS:
                detector.stop()  # a real conversation is under way
            else:
                detector.feed(text, bool(getattr(ev, "is_final", False)))

    session.on("user_input_transcribed", handle_user_input_transcribed)

    def handle_close(ev: Any) -> None:
        if not termination["reason"]:
            reason = getattr(ev, "reason", None)
            termination["reason"] = str(getattr(reason, "value", reason) or "session_closed")
        # Release the job so shutdown callbacks (finalize + webhook outbox) run promptly.
        ctx.shutdown(reason="session closed")

    session.on("close", handle_close)

    # Resolve webhook outbox destination URL
    webhook_destination = resolved_settings.webhook_destination()

    signals = CallSignals()
    # Whether this is a phone call, and whether the callee ever picked up (set once the callee joins).
    call_flags = {"sip": False, "answered": False}
    finalized = False
    guard_task: asyncio.Task[None] | None = None

    watchdog_task: dict[str, asyncio.Task[None] | None] = {"task": None}

    async def on_session_shutdown() -> None:
        nonlocal finalized
        if guard_task is not None:
            guard_task.cancel()
        if watchdog_task["task"] is not None:
            watchdog_task["task"].cancel()
        if detector is not None:
            detector.stop()
        cancel_closing_watch()
        if finalized:
            return
        finalized = True
        # Counted from pickup, not job start: ringing is not call time (0 for a call nobody answered).
        duration = since_pickup()
        signals.greeting_verdict = call_state.greeting_verdict
        signals.voicemail_tool_called = call_state.voicemail_detected
        signals.do_not_call_requested = call_state.do_not_call
        if call_state.voicemail_detected and not signals.forced_outcome:
            signals.forced_outcome = machine_outcome(call_state.machine_kind)[0]
            signals.forced_rationale = (
                "The agent detected a phone menu, call screener or system message."
                if signals.forced_outcome == "automated_menu"
                else "The agent detected a voicemail or recorded message."
            )
        if call_flags["sip"] and not call_flags["answered"] and not signals.forced_outcome:
            # The phone leg ended while still ringing (declined, busy, network refusal): not a conversation.
            signals.forced_outcome = "no_answer"
            signals.forced_rationale = "The callee did not answer (the call was rejected or dropped while ringing)."

        async def classify(transcript: list[Any], sig: CallSignals) -> Any:
            return await classify_transcript(
                transcript,
                sig,
                api_key=resolved_settings.gemini_api_key,
                model=resolved_settings.llm.default_model,
            )

        result = await resolve_call_outcome(list(transcript_buffer), signals, classify)
        logger.info("Call outcome: %s (%s) [%s]", result.outcome, result.rationale, result.source)
        _usage_into_costs(session, cost_accumulator, duration)
        mark(
            "call_end",
            reason=termination["reason"] or "normal_completion",
            outcome=result.outcome,
            ring_secs=round(clock.ring_secs, 2),
            talk_secs=round(duration, 2),
        )
        if event_tasks:  # store the timing events before the call is finalized
            await asyncio.gather(*event_tasks, return_exceptions=True)
        try:
            await persist("transcripts", recorder.record_transcript_batch(call_id, transcript_buffer))
            await persist("usage", recorder.record_usage_batch(call_id, cost_accumulator.records))
            await persist(
                "finalize",
                recorder.finalize_call(
                    call_id=call_id,
                    duration_secs=duration,
                    termination_reason=termination["reason"] or "normal_completion",
                    call_outcome=result.outcome,
                    outcome_rationale=result.rationale,
                    transcript_summary=result.summary,
                    total_cost=cost_accumulator.total_cost,
                    charging_details=cost_accumulator.get_charging_breakdown(),
                    destination_url=webhook_destination,
                    agent_id=reported_agent_id,
                    agent_name=agent_entry.name,
                ),
            )
        finally:
            if engine is not None:
                await engine.dispose()

    ctx.add_shutdown_callback(on_session_shutdown)

    # Hard cap on call length (the SIP max_call_duration is only a backstop).
    async def duration_guard() -> None:
        await asyncio.sleep(conv_cfg.max_duration_seconds)
        logger.warning("Max call duration (%ss) reached; ending call.", conv_cfg.max_duration_seconds)
        signals.notes.append("The call was cut off at the maximum call duration.")
        end_call("max_duration", drain=False)

    guard_task = asyncio.create_task(duration_guard())

    # Deleting the room on close hangs up the SIP leg; otherwise the callee is left on a silent line.
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(delete_room_on_close=True),
    )

    # Broadcast initial session state
    await broadcast_telemetry("session_init", {
        "call_id": call_id,
        "persona": call_request.persona,
        "active_model": resolved_settings.llm.default_model,
        "dynamic_variables": call_request.dynamic_variables.model_dump(),
    })

    # Wait for the callee (SIP participant or browser tester) to join the room
    logger.info("Waiting for callee to join the room...")
    try:
        callee = await asyncio.wait_for(ctx.wait_for_participant(), PARTICIPANT_JOIN_TIMEOUT_SECS)
    except Exception as e:
        logger.warning("No callee joined: %s", e)
        signals.forced_outcome, signals.forced_rationale = "no_answer", "No participant joined the room."
        end_call("no_participant", drain=False)
        return

    is_sip = (
        callee.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        or callee.identity.startswith("phone_")
    )

    call_flags["sip"] = is_sip

    def register_pickup() -> None:
        """The callee is on the line: start the call clock, the recording and the greeting check."""
        ring_secs = clock.pick_up()
        call_flags["answered"] = True
        activity["heard"] = activity["agent_done"] = time.monotonic()
        mark("pickup", ring_secs=round(ring_secs, 2), sip=is_sip)
        spawn(begin_recording())
        if detector is not None:
            detector.start()

    if is_sip:
        session.room_io.set_participant(callee.identity)
        answer = await _wait_for_answer(
            callee,
            conv_cfg.ring_timeout_seconds,
            is_gone=lambda: callee.identity not in ctx.room.remote_participants,
        )
        if answer != "active":
            logger.warning("Callee did not answer (%s).", answer)
            signals.forced_outcome, signals.forced_rationale = "no_answer", f"Call not answered ({answer})."
            end_call("no_answer", drain=False)
            return
        logger.info("SIP callee answered (%s).", callee.identity)
        register_pickup()
    else:
        logger.info("Non-SIP participant joined (%s).", callee.identity)
        register_pickup()

    # Speak the opening line about `opener_delay_seconds` after pickup (it used to wait 5.9 s at the median
    # for LiveKit's AMD, which never classified anything). If the callee is mid-sentence at that moment, wait
    # a little for them to finish, then speak anyway. The greeting check runs meanwhile and hangs up on a
    # machine, cutting the opener short, so a mailbox hears at most a couple of seconds of it.
    delay = conv_cfg.opener_delay_seconds - since_pickup()
    if delay > 0:
        await asyncio.sleep(delay)
    if termination["reason"]:
        return
    quiet_wait = conv_cfg.opener_quiet_wait_seconds
    quiet = await _wait_for_caller_quiet(session, quiet_secs=OPENER_QUIET_SECS, max_wait=quiet_wait) if quiet_wait > 0 else True
    if termination["reason"]:
        return

    # The LLM was held back until now, so this is the first and only opener; from here on it may answer
    # the callee normally. The opener is uninterruptible: an impatient "Hello?" used to cancel it, and
    # the callee then heard nothing for 26 s.
    first_msg = prompt_manager.get_first_message(call_request.dynamic_variables)
    logger.info("Speaking first message: '%s'", first_msg)
    agent.open_reply_gate()
    opener_state["requested"] = True
    opener_state["playing"] = True
    mark("opener_started", waited_for_quiet=quiet)
    speech_handle = session.say(first_msg, allow_interruptions=False)
    await speech_handle.wait_for_playout()
    opener_state["playing"] = False
    if termination["reason"]:
        return
    opener_spoken = True
    mark("opener_finished")
    session.reset_away_timer()
    # The callee may have answered over the opener ("Yes, this is the owner"). LiveKit skipped that turn, so
    # answer it now, unless it was only "Hello?", an echo of the opener, or the greeting check has not
    # confirmed a person (a voicemail greeting must never get a reply).
    overlap = " ".join(t for t in overlap_finals if needs_reply(t, first_msg))
    if overlap and (detector is None or call_state.greeting_verdict == "human"):
        call_state.user_turns += 1
        mark("replying_to_overlap", words=len(overlap.split()))
        session.generate_reply(user_input=overlap, instructions=OVERLAP_REPLY_INSTRUCTIONS)
    if conv_cfg.no_speech_watchdog_seconds > 0:
        watchdog_task["task"] = asyncio.create_task(no_speech_watchdog())


_configured_settings: Settings | None = None


async def default_agent_entrypoint(ctx: JobContext) -> None:
    """Top-level RTC session entrypoint picklable by multiprocessing process pools."""
    await run_agent_session(ctx, settings=_configured_settings)


def create_agent_server(settings: Settings | None = None) -> AgentServer:
    """Factory creating configured AgentServer with session entrypoint."""
    global _configured_settings
    app_settings = settings or load_settings()
    _configured_settings = app_settings

    # Shutdown callbacks (transcripts, costs, finalize + outbox) must finish before the job is killed;
    # the LiveKit default is 10 seconds.
    server_kwargs: dict[str, Any] = {"shutdown_process_timeout": SHUTDOWN_PROCESS_TIMEOUT_SECS}
    if app_settings.livekit_url:
        server_kwargs["ws_url"] = app_settings.livekit_url
        os.environ["LIVEKIT_URL"] = app_settings.livekit_url
    if app_settings.livekit_api_key:
        server_kwargs["api_key"] = app_settings.livekit_api_key
        os.environ["LIVEKIT_API_KEY"] = app_settings.livekit_api_key
    if app_settings.livekit_api_secret:
        server_kwargs["api_secret"] = app_settings.livekit_api_secret
        os.environ["LIVEKIT_API_SECRET"] = app_settings.livekit_api_secret

    server = AgentServer(**server_kwargs)
    server.setup_fnc = prewarm_process
    agent_name = os.environ.get("LIVEKIT_AGENT_NAME", "posivex-agent")
    server.rtc_session(default_agent_entrypoint, agent_name=agent_name)

    return server


if __name__ == "__main__":
    from livekit.agents import cli

    cli.run_app(create_agent_server())
