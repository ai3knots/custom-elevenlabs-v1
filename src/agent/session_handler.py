"""Session lifecycle, AMD layering, and AgentServer entrypoint orchestration."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from dotenv import load_dotenv
from livekit.agents import (
    AMD,
    AgentServer,
    AgentSession,
    JobContext,
)

from src.agent.persona_agent import PersonaAgent
from src.config import CallRequest, Settings, load_settings
from src.fsm.machine import ConversationFSM
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
    """Parse and validate CallRequest from room metadata JSON."""
    if not room_metadata:
        logger.warning("Empty room metadata; creating default CallRequest.")
        return CallRequest(phone="+14045550000")

    try:
        data = json.loads(room_metadata)
        if isinstance(data, dict):
            return CallRequest.model_validate(data)
    except Exception as e:
        logger.error("Failed to parse room metadata JSON: %s (raw: %s)", e, room_metadata)

    return CallRequest(phone="+14045550000")


async def run_agent_session(
    ctx: JobContext,
    settings: Settings | None = None,
    recorder: CallRecorder | None = None,
) -> None:
    """Core entrypoint execution flow for an individual call session."""
    # Ensure room connection is established so room and remote participants are available
    await ctx.connect()

    # Resolve metadata from Room, Job, or remote participant
    raw_metadata = ctx.room.metadata
    if not raw_metadata and hasattr(ctx, "job") and ctx.job:
        if getattr(ctx.job, "metadata", None):
            raw_metadata = ctx.job.metadata
        elif getattr(ctx.job, "room", None) and getattr(ctx.job.room, "metadata", None):
            raw_metadata = ctx.job.room.metadata

    if not raw_metadata:
        for p in ctx.room.remote_participants.values():
            if p.metadata:
                raw_metadata = p.metadata
                break

    call_request = parse_call_request_from_room(raw_metadata)

    call_id = call_request.call_id or f"call_{ctx.room.name}"
    set_call_id(call_id)

    logger.info(
        "Starting agent session for call_id=%s, phone=%s, persona=%s, language=%s",
        call_id,
        call_request.phone,
        call_request.persona,
        call_request.language,
    )

    resolved_settings = settings or load_settings(
        persona=call_request.persona,
        overrides=call_request.config_overrides,
    )

    # Initialize persistence and cost accumulator
    cost_accumulator = CostAccumulator()
    if recorder:
        await recorder.create_call(
            call_id=call_id,
            phone=call_request.phone,
            persona=call_request.persona,
            language=call_request.language,
            lead_id=call_request.lead_id,
            campaign_id=call_request.campaign_id,
            metadata_json=call_request.dynamic_variables.model_dump(),
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
        if recorder:
            await recorder.record_event(
                call_id=call_id,
                event_type=event_type,
                state=payload.get("to_state"),
                payload=payload,
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
    )

    session = AgentSession(
        stt=stt_provider,
        llm=llm_primary,
        tts=tts_provider,
        vad=vad_provider,
    )

    # Attach event listeners
    def handle_silence_timeout(*args: Any, **kwargs: Any) -> None:
        fsm.record_silence()
        import asyncio
        asyncio.create_task(
            broadcast_telemetry("silence_timeout", {
                "silence_count": fsm.silence_count,
                "max_silence_count": resolved_settings.conversation.max_silence_count,
                "message": f"User silence timeout ({fsm.silence_count}/{resolved_settings.conversation.max_silence_count})",
            })
        )

    session.on("user_transcription_timeout", handle_silence_timeout)

    def handle_agent_state_changed(*args: Any, **kwargs: Any) -> None:
        if args and hasattr(args[0], "new_state"):
            new_state = str(args[0].new_state)
        elif len(args) >= 2:
            new_state = str(args[1])
        elif args:
            new_state = str(args[0])
        else:
            new_state = "unknown"
        import asyncio
        asyncio.create_task(
            broadcast_telemetry("agent_state_changed", {
                "agent_state": new_state,
                "current_node": fsm.current_state.value,
                "silence_count": fsm.silence_count,
            })
        )

    session.on("agent_state_changed", handle_agent_state_changed)

    def handle_user_state_changed(*args: Any, **kwargs: Any) -> None:
        if args and hasattr(args[0], "new_state"):
            new_state = str(args[0].new_state)
        elif len(args) >= 2:
            new_state = str(args[1])
        elif args:
            new_state = str(args[0])
        else:
            new_state = "unknown"
        import asyncio
        asyncio.create_task(
            broadcast_telemetry("user_state_changed", {
                "user_state": new_state,
                "current_node": fsm.current_state.value,
                "silence_count": fsm.silence_count,
            })
        )

    session.on("user_state_changed", handle_user_state_changed)

    # Resolve webhook outbox destination URL
    webhook_destination = resolved_settings.webhook.lead_gen_base_url
    if webhook_destination:
        if "/api/webhooks" not in webhook_destination:
            webhook_destination = f"{webhook_destination.rstrip('/')}/api/webhooks/elevenlabs"

    async def on_session_shutdown() -> None:
        if recorder:
            await recorder.record_usage_batch(call_id, cost_accumulator.records)
            await recorder.finalize_call(
                call_id=call_id,
                call_outcome=fsm.call_outcome or "other",
                outcome_rationale=fsm.outcome_rationale or "",
                total_cost=cost_accumulator.total_cost,
                charging_details=cost_accumulator.get_charging_breakdown(),
                destination_url=webhook_destination,
            )

    ctx.add_shutdown_callback(on_session_shutdown)

    await session.start(agent=agent, room=ctx.room)

    # Broadcast initial session state
    await broadcast_telemetry("session_init", {
        "call_id": call_id,
        "persona": call_request.persona,
        "current_node": fsm.current_state.value,
        "silence_count": fsm.silence_count,
        "active_model": resolved_settings.llm.default_model,
        "dynamic_variables": call_request.dynamic_variables.model_dump(),
    })

    # Layered AMD execution if telephony identity is present
    participant_identity = os.getenv("SIP_PARTICIPANT_IDENTITY")
    if participant_identity and session.room_io:
        session.room_io.set_participant(participant_identity)
        async with AMD(session, participant_identity=participant_identity) as detector:
            result = await detector.execute()
            logger.info("AMD detection result: %s (transcript: %s)", result.category, result.transcript)
            if result.category == "machine-vm":
                logger.info("Voicemail detected by AMD; terminating call without pitching.")
                fsm.set_outcome("voicemail", rationale="AMD detected voicemail greeting.")
                if recorder:
                    await recorder.finalize_call(
                        call_id=call_id,
                        call_outcome="voicemail",
                        outcome_rationale="AMD detected voicemail greeting.",
                        termination_reason="voicemail_detected",
                        destination_url=webhook_destination,
                    )
                session.shutdown()
                return
            elif result.category == "machine-ivr":
                logger.info("Automated menu detected by AMD; terminating call.")
                fsm.set_outcome("automated_menu", rationale="AMD detected automated IVR menu.")
                if recorder:
                    await recorder.finalize_call(
                        call_id=call_id,
                        call_outcome="automated_menu",
                        outcome_rationale="AMD detected automated IVR menu.",
                        termination_reason="automated_menu_detected",
                        destination_url=webhook_destination,
                    )
                session.shutdown()
                return

    # Callee is human: Deliver the opening first message
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

    server_kwargs: dict[str, Any] = {}
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
    server.rtc_session(default_agent_entrypoint)

    return server


if __name__ == "__main__":
    from livekit.agents import cli

    cli.run_app(create_agent_server())

