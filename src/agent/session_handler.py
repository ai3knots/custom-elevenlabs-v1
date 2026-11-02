"""Session lifecycle, AMD layering, and AgentServer entrypoint orchestration."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

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
    raw_metadata = ctx.room.metadata
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
    llm_primary = create_fallback_llm(llm_primary_base, llm_fallback_base, resolved_settings.llm)

    llm_conversation_base = get_llm(
        resolved_settings.llm.conversation_model,
        resolved_settings.llm,
        api_key=resolved_settings.gemini_api_key,
    )
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

    async def handle_agent_event(event_type: str, payload: dict[str, Any]) -> None:
        if recorder:
            await recorder.record_event(
                call_id=call_id,
                event_type=event_type,
                state=payload.get("to_state"),
                payload=payload,
            )

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
    session.on("user_transcription_timeout", lambda: fsm.record_silence())

    async def on_session_shutdown() -> None:
        if recorder:
            await recorder.record_usage_batch(call_id, cost_accumulator.records)
            await recorder.finalize_call(
                call_id=call_id,
                call_outcome=fsm.call_outcome or "other",
                outcome_rationale=fsm.outcome_rationale or "",
                total_cost=cost_accumulator.total_cost,
                charging_details=cost_accumulator.get_charging_breakdown(),
            )

    ctx.add_shutdown_callback(on_session_shutdown)

    await session.start(agent=agent, room=ctx.room)

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
                    )
                session.shutdown()
                return

    # Callee is human: Deliver the opening first message
    first_msg = prompt_manager.get_first_message(call_request.dynamic_variables)
    logger.info("Speaking first message: '%s'", first_msg)
    speech_handle = session.say(first_msg, allow_interruptions=True)
    await speech_handle.wait_for_playout()


def create_agent_server(settings: Settings | None = None) -> AgentServer:
    """Factory creating configured AgentServer with session entrypoint."""
    server = AgentServer()

    @server.rtc_session()
    async def entrypoint(ctx: JobContext) -> None:
        await run_agent_session(ctx, settings=settings)

    return server


if __name__ == "__main__":
    from livekit.agents import cli

    cli.run_app(create_agent_server())

