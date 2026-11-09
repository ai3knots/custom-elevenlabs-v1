"""Session lifecycle, AMD layering, and AgentServer entrypoint orchestration."""

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
    AMD,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    inference,
    room_io,
)
from livekit.agents.metrics.usage import LLMModelUsage, STTModelUsage, TTSModelUsage
from sqlalchemy.ext.asyncio import AsyncEngine

from src.agent.persona_agent import CallState, PersonaAgent, is_closing_line
from src.analysis import CallSignals, classify_transcript, resolve_call_outcome
from src.config import CallRequest, Settings, load_settings
from src.db.base import get_engine, get_sessionmaker
from src.logging import set_call_id
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
            return CallRequest(phone="+14045550000")
        raise ValueError("No call metadata supplied with the dispatch or room.")

    try:
        data = json.loads(room_metadata)
        if not isinstance(data, dict):
            raise ValueError("metadata JSON must be an object")
        return CallRequest.model_validate(data)
    except Exception as e:
        logger.error("Failed to parse call metadata: %s (raw: %s)", e, room_metadata[:500])
        if allow_default:
            return CallRequest(phone="+14045550000")
        raise ValueError(f"Invalid call metadata: {e}") from e


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


def _prewarmed_vad(ctx: JobContext, settings: Settings) -> Any | None:
    """VAD loaded during process warm-up, if its settings match this call's."""
    try:
        cached = ctx.proc.userdata.get("vad")
    except Exception:
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
        await asyncio.sleep(0.5)
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
        await asyncio.sleep(0.05)
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
        "interruption": {"min_words": settings.conversation.interruption_min_words},
    }
    mode = settings.turn_detector.mode
    if mode == "vad":
        turn_handling["turn_detection"] = "vad"
    elif mode == "stt-only":
        turn_handling["turn_detection"] = "stt"
    elif mode in ("v1", "v1-mini"):
        # Audio turn detector (reads the caller's audio). VAD is still used for speech
        # start/stop and interruptions. "v1" falls back to the local "v1-mini" on errors
        # or when the LiveKit Inference allowance is exhausted.
        turn_handling["turn_detection"] = inference.TurnDetector(version=mode)
    else:
        logger.warning("Turn detector mode '%s' is not available; using VAD.", mode)
        turn_handling["turn_detection"] = "vad"
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

    logger.info(
        "Starting agent session for call_id=%s, phone=%s, persona=%s, language=%s",
        call_id,
        call_request.phone,
        call_request.persona,
        call_request.language,
    )

    resolved_settings = _settings_for_call(settings, call_request)
    started_at = time.monotonic()

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

    # Share a single thought_signatures dictionary across the Gemini LLM instances
    shared_thought_signatures: dict[str, bytes] = {}
    for base_llm in (llm_primary_base, llm_fallback_base):
        if hasattr(base_llm, "_thought_signatures"):
            object.__setattr__(base_llm, "_thought_signatures", shared_thought_signatures)

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

    transcript_buffer: list[dict[str, Any]] = []

    def record_held_turn(text: str) -> None:
        """Store what the callee said before the opening line (the LLM does not answer it)."""
        transcript_buffer.append({
            "role": "user",
            "message": text,
            "time_in_call_secs": time.monotonic() - started_at,
            "interrupted": False,
        })

    agent = PersonaAgent(
        instructions=prompt_manager.render_prompt(call_request.dynamic_variables),
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
            "time_in_call_secs": time.monotonic() - started_at,
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
            session.generate_reply(
                instructions='Say one casual "Hello?" once and nothing else. Do not repeat your introduction.'
            )
            return
        logger.info("Caller silent for %.0fs; ending the call.", conv_cfg.silence_end_call_seconds)
        call_state.end_reason = call_state.end_reason or "silence"
        end_call("silence_timeout", drain=False)

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
        spawn(broadcast_telemetry("agent_state_changed", {"agent_state": str(getattr(ev, "new_state", "unknown"))}))

    session.on("agent_state_changed", handle_agent_state_changed)

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

    async def on_session_shutdown() -> None:
        nonlocal finalized
        if guard_task is not None:
            guard_task.cancel()
        cancel_closing_watch()
        if finalized:
            return
        finalized = True
        duration = time.monotonic() - started_at
        signals.amd_category = call_state.amd_category
        signals.voicemail_tool_called = call_state.voicemail_detected
        signals.do_not_call_requested = call_state.do_not_call
        if call_state.voicemail_detected and not signals.forced_outcome:
            signals.forced_outcome = "voicemail"
            signals.forced_rationale = "The agent detected a voicemail or recorded message."
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
        callee = await ctx.wait_for_participant()
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
    if is_sip:
        session.room_io.set_participant(callee.identity)

        async def callee_answered() -> bool:
            answer = await _wait_for_answer(
                callee,
                conv_cfg.ring_timeout_seconds,
                is_gone=lambda: callee.identity not in ctx.room.remote_participants,
            )
            if answer != "active":
                logger.warning("Callee did not answer (%s).", answer)
                signals.forced_outcome, signals.forced_rationale = "no_answer", f"Call not answered ({answer})."
                end_call("no_answer", drain=False)
                return False
            call_flags["answered"] = True
            logger.info("SIP callee answered (%s).", callee.identity)
            spawn(begin_recording())
            return True

        if conv_cfg.amd_enabled:
            # AMD defers its timers until sip.callStatus is 'active', so it can be armed during ringing.
            result = None
            async with AMD(
                session,
                llm=None,
                stt=None,
                participant_identity=callee.identity,
                ivr_detection=False,
                # `timeout` is a hard cap measured from pickup (wait_until_finished=False), so a recording
                # notice followed by "Hello?" cannot hold the opening line back for 10+ seconds.
                detection_options={
                    "no_speech_threshold": conv_cfg.amd_no_speech_seconds,
                    "timeout": conv_cfg.amd_timeout_seconds,
                },
                wait_until_finished=False,
            ) as detector:
                if not await callee_answered():
                    return
                try:
                    result = await asyncio.wait_for(
                        detector.execute(), timeout=conv_cfg.amd_timeout_seconds + 5.0
                    )
                except TimeoutError:
                    logger.warning("AMD timed out; treating callee as human.")
            if result is not None:
                call_state.amd_category = str(getattr(result.category, "value", result.category))
                logger.info("AMD result: %s (transcript: %s)", result.category, result.transcript)
                if result.category in ("machine-vm", "machine-unavailable"):
                    signals.forced_outcome = "voicemail"
                    signals.forced_rationale = f"AMD: {result.category}. {result.transcript[:200]}"
                    end_call("voicemail_detected", drain=False)
                    return
                if result.category == "machine-ivr":
                    signals.forced_outcome = "automated_menu"
                    signals.forced_rationale = f"AMD: automated menu. {result.transcript[:200]}"
                    end_call("automated_menu_detected", drain=False)
                    return
        elif not await callee_answered():
            return
    else:
        logger.info("Non-SIP participant joined (%s).", callee.identity)
        call_flags["answered"] = True
        spawn(begin_recording())

    if termination["reason"]:
        return

    # Callee is human (or uncertain): deliver the opening first message. The LLM was held back until
    # now, so this is the first and only opener; from here on it may answer the callee normally.
    first_msg = prompt_manager.get_first_message(call_request.dynamic_variables)
    logger.info("Speaking first message: '%s'", first_msg)
    # Let the callee finish ("This call is being recorded. Hello?") before speaking, and make the
    # opener uninterruptible: it is ~4 s long, and when an impatient "Hello?" was allowed to cancel it
    # (and the replies generated for it) the callee heard nothing at all for 26 s.
    await _wait_for_caller_quiet(session)
    agent.open_reply_gate()
    speech_handle = session.say(first_msg, allow_interruptions=False)
    await speech_handle.wait_for_playout()
    opener_spoken = True
    session.reset_away_timer()


_configured_settings: Settings | None = None


async def default_agent_entrypoint(ctx: JobContext) -> None:
    """Top-level RTC session entrypoint picklable by multiprocessing process pools."""
    await run_agent_session(ctx, settings=_configured_settings)


def create_agent_server(settings: Settings | None = None) -> AgentServer:
    """Factory creating configured AgentServer with session entrypoint."""
    global _configured_settings
    load_dotenv()
    app_settings = settings or load_settings()
    _configured_settings = app_settings

    # Shutdown callbacks (transcripts, costs, finalize + outbox) must finish before the job is killed;
    # the LiveKit default is 10 seconds.
    server_kwargs: dict[str, Any] = {"shutdown_process_timeout": 30.0}
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
