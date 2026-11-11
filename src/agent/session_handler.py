"""The LiveKit entrypoint of a call: read the dispatch, build the providers, hand the call to `CallSession`.

The call itself (opener, silence, greeting check, outcome, report) lives in `src/agent/call_session/`. This module
only wires it up, plus the worker plumbing (prewarm, server factory) and the turn-handling options.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from dotenv import load_dotenv
from livekit.agents import (
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    inference,
)
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from src.agent.call_clock import CallClock
from src.agent.call_session import (
    CallSession,
    CallTimeline,
    CallTimings,
    usage_into_costs,
    wait_for_answer,
    wait_for_caller_quiet,
)
from src.agent.greeting_detector import GeminiGreetingClassifier
from src.agent.silence import watch_for_silence
from src.analysis import classify_transcript
from src.config import CallRequest, Settings, load_settings
from src.db.base import get_engine, get_sessionmaker
from src.logging import mask_phone, set_call_id
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

logger = logging.getLogger("posivex.session")
load_dotenv()

# The names the tests (and the pieces of a call) know these helpers by.
_wait_for_answer = wait_for_answer
_wait_for_caller_quiet = wait_for_caller_quiet
_usage_into_costs = usage_into_costs

# Grace period after an agent goodbye before the call is ended if the model did not call end_call.
CLOSING_GRACE_SECS = 2.5

# The callee (the SIP participant, or a browser tester) joins the room within seconds of the dial request. If nobody
# has by then, the call is reported as not answered instead of the job waiting in an empty room.
PARTICIPANT_JOIN_TIMEOUT_SECS = 60.0

# Shutdown callbacks (transcripts, costs, outcome, the report to the API) must finish before the job is killed;
# LiveKit's default is 10 seconds. Worst case: the outcome classifier (20 s) plus the report to the API (its 60 s
# retry budget plus one 10 s request), with margin. Only a down API takes this long.
SHUTDOWN_PROCESS_TIMEOUT_SECS = 120.0

# Stand-in number used only with ALLOW_DEFAULT_CALL_REQUEST=true (local development).
DEV_PLACEHOLDER_PHONE = "+14045550000"


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
        logger.error(
            "Failed to parse call metadata (%s, %d characters): %s",
            type(e).__name__,
            len(room_metadata),
            _describe_parse_error(e),
        )
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


def _choose_recorder(
    settings: Settings, recorder: CallRecorder | RemoteRecorder | None
) -> tuple[AsyncEngine | None, CallRecorder | RemoteRecorder]:
    """Where the call is stored.

    The worker owns its engine (asyncpg pools are event-loop bound) and writes best-effort so a database problem can
    never take down a live call. An agent hosted on LiveKit Cloud cannot reach the database, so it reports the
    finished call to the API instead.
    """
    if recorder is not None:
        return None, recorder
    if settings.results_callback_url:
        return None, RemoteRecorder(settings.results_callback_url, settings.results_callback_secret)
    engine = get_engine(settings.database_url)
    return engine, CallRecorder(get_sessionmaker(engine))


async def _build_session(ctx: JobContext, settings: Settings) -> AgentSession:
    """The LiveKit session with this call's speech-to-text, LLM (with fallback), text-to-speech and VAD."""
    stt_provider = get_stt(settings.stt, api_key=settings.deepgram_api_key)
    vad_provider = _prewarmed_vad(ctx, settings) or get_vad(settings.vad)
    tts_provider = get_tts(settings.tts, api_key=settings.elevenlabs_api_key)

    # Primary and fallback Gemini models. Building a Gemini client loads the CA bundle (~300 ms of CPU),
    # so it runs in a thread instead of stalling the event loop at call start.
    def build_llms() -> tuple[Any, Any]:
        key = settings.gemini_api_key
        return (
            get_llm(settings.llm.default_model, settings.llm, api_key=key),
            get_llm(settings.llm.fallback_model, settings.llm, api_key=key),
        )

    llm_primary_base, llm_fallback_base = await asyncio.to_thread(build_llms)
    return AgentSession(
        stt=stt_provider,
        llm=create_fallback_llm(llm_primary_base, llm_fallback_base, settings.llm),
        tts=tts_provider,
        vad=vad_provider,
        user_away_timeout=settings.conversation.silence_end_call_seconds,
        turn_handling=build_turn_handling(settings),
    )


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
    config = resolved_settings.conversation

    # Call time is counted from pickup, as ElevenLabs does, so transcript times and the duration lead-gen shows
    # do not include 5 to 75 s of ringing. The job start only tells how long it rang.
    engine, call_recorder = _choose_recorder(resolved_settings, recorder)
    timeline = CallTimeline(call_id, call_recorder, CallClock())

    # The API normally created the row at dispatch time; browser/test calls have none yet.
    await timeline.persist(
        "ensure_call",
        call_recorder.ensure_call(
            call_id=call_id,
            phone=call_request.phone,
            persona=call_request.persona,
            language=call_request.language,
            lead_id=call_request.lead_id,
            campaign_id=call_request.campaign_id,
            metadata_json={"dynamic_variables": call_request.dynamic_variables.model_dump()},
        ),
    )

    prompt_manager = PromptManager(persona=call_request.persona, kb_config=resolved_settings.kb)
    instructions = await asyncio.to_thread(prompt_manager.render_prompt, call_request.dynamic_variables)
    session = await _build_session(ctx, resolved_settings)

    # The fast Gemini check that hangs up on voicemails and phone menus needs a key.
    classifier = None
    if config.machine_detection_enabled and resolved_settings.gemini_api_key:
        classifier = GeminiGreetingClassifier(resolved_settings.gemini_api_key, config.machine_detection_model)

    async def classify_outcome(transcript: Any, signals: Any) -> Any:
        return await classify_transcript(
            transcript,
            signals,
            api_key=resolved_settings.gemini_api_key,
            model=resolved_settings.llm.default_model,
        )

    call = CallSession(
        ctx=ctx,
        session=session,
        call_request=call_request,
        settings=resolved_settings,
        timeline=timeline,
        engine=engine,
        instructions=instructions,
        first_message=prompt_manager.get_first_message(call_request.dynamic_variables),
        classifier=classifier,
        classify_outcome=classify_outcome,
        silence_watch=watch_for_silence,
        timings=CallTimings(
            closing_grace_secs=CLOSING_GRACE_SECS,
            participant_join_timeout_secs=PARTICIPANT_JOIN_TIMEOUT_SECS,
        ),
    )
    await call.run()


_configured_settings: Settings | None = None


async def default_agent_entrypoint(ctx: JobContext) -> None:
    """Top-level RTC session entrypoint picklable by multiprocessing process pools."""
    await run_agent_session(ctx, settings=_configured_settings)


def create_agent_server(settings: Settings | None = None) -> AgentServer:
    """Factory creating configured AgentServer with session entrypoint."""
    global _configured_settings
    app_settings = settings or load_settings()
    _configured_settings = app_settings

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
