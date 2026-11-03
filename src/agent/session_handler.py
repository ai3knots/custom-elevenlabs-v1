"""Session lifecycle, AMD layering, and AgentServer entrypoint orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Coroutine
from typing import Any

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    AMD,
    AgentServer,
    AgentSession,
    JobContext,
    TurnHandlingOptions,
    room_io,
)
from livekit.agents.metrics.usage import LLMModelUsage, STTModelUsage, TTSModelUsage
from sqlalchemy.ext.asyncio import AsyncEngine

from src.agent.persona_agent import PersonaAgent
from src.config import CallRequest, Settings, load_settings
from src.db.base import get_engine, get_sessionmaker
from src.fsm.machine import ConversationFSM
from src.fsm.states import State, StateMetadata
from src.logging import set_call_id
from src.metrics.costs import CostAccumulator
from src.persistence.recorder import CallRecorder
from src.prompts.manager import PromptManager
from src.providers import (
    create_fallback_llm,
    get_llm,
    get_stt,
    get_tts,
    get_vad,
)

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
                costs.add_llm_usage(usage.model, usage.input_tokens, usage.output_tokens)
            elif isinstance(usage, TTSModelUsage):
                costs.add_tts_usage(usage.model, usage.characters_count)
            elif isinstance(usage, STTModelUsage):
                costs.add_stt_usage(usage.model, usage.audio_duration)
    except Exception as e:
        logger.warning("Could not read session usage: %s", e)
    costs.add_telephony_usage(duration_secs)


def _resolve_outcome(fsm: ConversationFSM, user_turns: int) -> tuple[str, str]:
    """Outcome to report; falls back to the CRM's fallback outcomes when the agent recorded none."""
    if fsm.call_outcome:
        return fsm.call_outcome, fsm.outcome_rationale or ""
    if user_turns == 0:
        return "hangup_no_speak", "Call ended without the callee speaking."
    if fsm.current_state in (State.CLOSE, State.END):
        return "other", "Call ended normally without a recorded outcome."
    return "unknown_dropped", f"Call ended while in state '{fsm.current_state.value}'."


async def _wait_for_answer(callee: rtc.RemoteParticipant, timeout: float) -> str:
    """Wait for sip.callStatus to become 'active'. Returns 'active', 'hangup' or 'timeout'."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = callee.attributes.get("sip.callStatus")
        if status == "active":
            return "active"
        if status == "hangup":
            return "hangup"
        await asyncio.sleep(0.5)
    return "timeout"


def build_turn_handling(settings: Settings, meta: StateMetadata) -> TurnHandlingOptions:
    """Turn-taking options for AgentSession: end-of-turn strategy, endpointing and interruptions."""
    turn_handling: TurnHandlingOptions = {
        "endpointing": {
            "min_delay": meta.min_endpointing_delay,
            "max_delay": meta.max_endpointing_delay,
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
    recorder: CallRecorder | None = None,
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
    # best-effort so a database problem can never take down a live call.
    engine: AsyncEngine | None = None
    if recorder is None:
        engine = get_engine(resolved_settings.database_url)
        recorder = CallRecorder(get_sessionmaker(engine))

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
    vad_provider = get_vad(resolved_settings.vad)
    tts_provider = get_tts(resolved_settings.tts, api_key=resolved_settings.elevenlabs_api_key)

    # Setup primary and conversation LLMs with fallback
    llm_primary_base = get_llm(
        resolved_settings.llm.default_model,
        resolved_settings.llm,
        api_key=resolved_settings.gemini_api_key,
    )
    llm_fallback_base = get_llm(
        resolved_settings.llm.fallback_model,
        resolved_settings.llm,
        api_key=resolved_settings.gemini_api_key,
    )
    llm_conversation_base = get_llm(
        resolved_settings.llm.conversation_model,
        resolved_settings.llm,
        api_key=resolved_settings.gemini_api_key,
    )

    # Share a single thought_signatures dictionary across all Gemini LLM instances
    shared_thought_signatures: dict[str, bytes] = {}
    for base_llm in (llm_primary_base, llm_fallback_base, llm_conversation_base):
        if hasattr(base_llm, "_thought_signatures"):
            object.__setattr__(base_llm, "_thought_signatures", shared_thought_signatures)

    llm_primary = create_fallback_llm(llm_primary_base, llm_fallback_base, resolved_settings.llm)
    llm_conversation = create_fallback_llm(llm_conversation_base, llm_fallback_base, resolved_settings.llm)

    # Initialize FSM and PromptManager
    fsm = ConversationFSM(
        max_silence_count=resolved_settings.conversation.max_silence_count,
        hold_duration_seconds=resolved_settings.conversation.hold_duration_seconds,
    )

    prompt_manager = PromptManager(
        persona=call_request.persona,
        language=call_request.language,
        kb_config=resolved_settings.kb,
    )

    background_tasks: set[asyncio.Task[Any]] = set()

    def spawn(coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

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
            recorder.record_event(
                call_id=call_id,
                event_type=event_type,
                state=payload.get("to_state"),
                payload=payload,
            ),
        )
        await broadcast_telemetry(event_type, {
            "from_node": payload.get("from_state"),
            "to_node": payload.get("to_state"),
            "current_node": fsm.current_state.value,
            "reason": payload.get("reason", ""),
            "silence_count": fsm.silence_count,
            "active_model": (
                resolved_settings.llm.conversation_model
                if fsm.current_state.value == "conversation"
                else resolved_settings.llm.default_model
            ),
        })

    agent = PersonaAgent(
        fsm=fsm,
        prompt_manager=prompt_manager,
        variables=call_request.dynamic_variables,
        llm_primary=llm_primary,
        llm_conversation=llm_conversation,
        call_id=call_id,
        event_handler=handle_agent_event,
        ignore_terms=resolved_settings.conversation.interruption_ignore_terms,
    )

    conv_cfg = resolved_settings.conversation
    session = AgentSession(
        stt=stt_provider,
        llm=llm_primary,
        tts=tts_provider,
        vad=vad_provider,
        user_away_timeout=conv_cfg.silence_timeout_seconds,
        turn_handling=build_turn_handling(resolved_settings, fsm.metadata),
    )

    # ---- Transcript capture -------------------------------------------------------------------
    transcript_buffer: list[dict[str, Any]] = []

    def on_conversation_item(ev: Any) -> None:
        item = getattr(ev, "item", None)
        if item is None or getattr(item, "type", None) != "message":
            return
        role = {"user": "user", "assistant": "agent"}.get(item.role)
        text = item.text_content
        if role is None or not text:
            return
        transcript_buffer.append({
            "role": role,
            "message": text,
            "time_in_call_secs": time.monotonic() - started_at,
            "interrupted": bool(getattr(item, "interrupted", False)),
        })

    session.on("conversation_item_added", on_conversation_item)

    # ---- Termination bookkeeping --------------------------------------------------------------
    termination: dict[str, str] = {"reason": ""}

    def end_call(reason: str, *, drain: bool = True) -> None:
        if not termination["reason"]:
            termination["reason"] = reason
        session.shutdown(drain=drain)

    # ---- Silence handling (user_away_timeout -> user_state_changed 'away') --------------------
    async def handle_away() -> None:
        if fsm.is_holding:
            # The merchant asked us to wait; keep listening without counting silence.
            session.reset_away_timer()
            return
        count = await fsm.record_silence()
        await broadcast_telemetry("silence_timeout", {
            "silence_count": count,
            "max_silence_count": conv_cfg.max_silence_count,
            "message": f"User silence timeout ({count}/{conv_cfg.max_silence_count})",
        })
        if fsm.is_terminal:
            end_call("silence_timeout", drain=False)
            return
        session.generate_reply(
            instructions=(
                "The caller has gone quiet. In one short, natural sentence, check whether they are "
                "still there. Do not repeat your pitch."
            )
        )

    def handle_user_state_changed(ev: Any) -> None:
        new_state = str(getattr(ev, "new_state", "unknown"))
        if new_state == "away":
            spawn(handle_away())
        elif new_state == "speaking":
            fsm.reset_silence()
            if fsm.is_holding:
                fsm.release_hold()
        spawn(broadcast_telemetry("user_state_changed", {
            "user_state": new_state,
            "current_node": fsm.current_state.value,
            "silence_count": fsm.silence_count,
        }))

    session.on("user_state_changed", handle_user_state_changed)

    def handle_agent_state_changed(ev: Any) -> None:
        spawn(broadcast_telemetry("agent_state_changed", {
            "agent_state": str(getattr(ev, "new_state", "unknown")),
            "current_node": fsm.current_state.value,
            "silence_count": fsm.silence_count,
        }))

    session.on("agent_state_changed", handle_agent_state_changed)

    def handle_close(ev: Any) -> None:
        if not termination["reason"]:
            reason = getattr(ev, "reason", None)
            termination["reason"] = str(getattr(reason, "value", reason) or "session_closed")
        # Release the job so shutdown callbacks (finalize + webhook outbox) run promptly.
        ctx.shutdown(reason="session closed")

    session.on("close", handle_close)

    # Resolve webhook outbox destination URL
    webhook_destination = resolved_settings.webhook.lead_gen_base_url
    if webhook_destination and "/api/webhooks" not in webhook_destination:
        webhook_destination = f"{webhook_destination.rstrip('/')}/api/webhooks/elevenlabs"

    finalized = False
    guard_task: asyncio.Task[None] | None = None

    async def on_session_shutdown() -> None:
        nonlocal finalized
        if guard_task is not None:
            guard_task.cancel()
        if finalized:
            return
        finalized = True
        duration = time.monotonic() - started_at
        user_turns = sum(1 for t in transcript_buffer if t["role"] == "user")
        outcome, rationale = _resolve_outcome(fsm, user_turns)
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
                    call_outcome=outcome,
                    outcome_rationale=rationale,
                    total_cost=cost_accumulator.total_cost,
                    charging_details=cost_accumulator.get_charging_breakdown(),
                    destination_url=webhook_destination,
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
        if not fsm.call_outcome:
            fsm.set_outcome("other", rationale="Maximum call duration reached.")
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
        "current_node": fsm.current_state.value,
        "silence_count": fsm.silence_count,
        "active_model": resolved_settings.llm.default_model,
        "dynamic_variables": call_request.dynamic_variables.model_dump(),
    })

    # Wait for the callee (SIP participant or browser tester) to join the room
    logger.info("Waiting for callee to join the room...")
    try:
        callee = await ctx.wait_for_participant()
    except Exception as e:
        logger.warning("No callee joined: %s", e)
        fsm.set_outcome("no_answer", rationale="No participant joined the room.")
        end_call("no_participant", drain=False)
        return

    is_sip = (
        callee.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        or callee.identity.startswith("phone_")
    )

    if is_sip:
        session.room_io.set_participant(callee.identity)

        async def callee_answered() -> bool:
            answer = await _wait_for_answer(callee, conv_cfg.ring_timeout_seconds)
            if answer != "active":
                logger.warning("Callee did not answer (%s).", answer)
                fsm.set_outcome("no_answer", rationale=f"Call not answered ({answer}).")
                end_call("no_answer", drain=False)
                return False
            logger.info("SIP callee answered (%s).", callee.identity)
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
                detection_options={"no_speech_threshold": conv_cfg.amd_no_speech_seconds},
            ) as detector:
                if not await callee_answered():
                    return
                try:
                    result = await asyncio.wait_for(detector.execute(), timeout=45.0)
                except TimeoutError:
                    logger.warning("AMD timed out; treating callee as human.")
            if result is not None:
                logger.info("AMD result: %s (transcript: %s)", result.category, result.transcript)
                if result.category in ("machine-vm", "machine-unavailable"):
                    fsm.set_outcome("voicemail", rationale=f"AMD: {result.category}. {result.transcript[:200]}")
                    end_call("voicemail_detected", drain=False)
                    return
                if result.category == "machine-ivr":
                    fsm.set_outcome("automated_menu", rationale=f"AMD: automated menu. {result.transcript[:200]}")
                    end_call("automated_menu_detected", drain=False)
                    return
        elif not await callee_answered():
            return
    else:
        logger.info("Non-SIP participant joined (%s).", callee.identity)

    # Callee is human (or uncertain): deliver the opening first message
    first_msg = prompt_manager.get_first_message(call_request.dynamic_variables)
    logger.info("Speaking first message: '%s'", first_msg)
    speech_handle = session.say(first_msg, allow_interruptions=True)
    await speech_handle.wait_for_playout()


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
    agent_name = os.environ.get("LIVEKIT_AGENT_NAME", "posivex-agent")
    server.rtc_session(default_agent_entrypoint, agent_name=agent_name)

    return server


if __name__ == "__main__":
    from livekit.agents import cli

    cli.run_app(create_agent_server())
