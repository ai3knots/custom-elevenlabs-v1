"""Read side of the ElevenLabs ConvAI API that Posivex-lead-gen uses.

Lead-gen lists agents and phone numbers for its Settings page, fetches a conversation to ingest or reap a
call, lists recent conversations to find calls it lost track of, and proxies the recording to the browser.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from src.api.deps import get_call_recorder, get_settings, verify_xi_api_key
from src.config import Settings
from src.db.models import Call, CallStatus
from src.persistence.recorder import CallRecorder
from src.storage import open_recording
from src.webhook.payload import build_conversation_data, conversation_status

logger = logging.getLogger("posivex.api.convai_read")
router = APIRouter(prefix="/v1/convai", tags=["ElevenLabs Emulation"], dependencies=[Depends(verify_xi_api_key)])


def _agent_for_call(call: Call, settings: Settings) -> tuple[str, str]:
    """(agent_id, agent_name) a call was placed with: the id lead-gen dialed, else its persona's agent."""
    dialed = (call.metadata_json or {}).get("agent_id")
    entry = settings.agent_for_id(dialed) or settings.agent_for_persona(call.persona)
    return (dialed or entry.agent_id), entry.name


@router.get("/agents")
async def list_agents(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return {
        "agents": [{"agent_id": a.agent_id, "name": a.name} for a in settings.agents],
        "has_more": False,
        "next_cursor": None,
    }


@router.get("/phone-numbers")
async def list_phone_numbers(settings: Settings = Depends(get_settings)) -> list[dict[str, Any]]:
    return [
        {
            "phone_number_id": n.phone_number_id,
            "phone_number": n.phone_number,
            "label": n.label,
            "provider": "sip_trunk",
            "supports_outbound": True,
            "supports_inbound": False,
        }
        for n in settings.phone_numbers
    ]


@router.get("/conversations")
async def list_conversations(
    agent_id: str | None = None,
    call_start_after_unix: int | None = None,
    page_size: int = Query(100, ge=1, le=500),
    settings: Settings = Depends(get_settings),
    recorder: CallRecorder = Depends(get_call_recorder),
) -> dict[str, Any]:
    since = datetime.fromtimestamp(call_start_after_unix or 0, tz=UTC)
    calls = await recorder.list_calls_since(since, limit=page_size)
    items: list[dict[str, Any]] = []
    for call in calls:
        call_agent_id, call_agent_name = _agent_for_call(call, settings)
        if agent_id and call_agent_id != agent_id:
            continue
        items.append(
            {
                "conversation_id": call.id,
                "agent_id": call_agent_id,
                "agent_name": call_agent_name,
                "start_time_unix_secs": int(call.started_at.replace(tzinfo=call.started_at.tzinfo or UTC).timestamp()),
                "call_duration_secs": int(call.duration_secs or 0),
                "status": conversation_status(call.status),
                "direction": (call.metadata_json or {}).get("direction", "outbound"),
            }
        )
    return {"conversations": items, "has_more": False, "next_cursor": None}


async def _require_call(call_id: str, recorder: CallRecorder) -> Call:
    call = await recorder.get_call(call_id)
    if call is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Conversation '{call_id}' not found")
    return call


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    settings: Settings = Depends(get_settings),
    recorder: CallRecorder = Depends(get_call_recorder),
) -> dict[str, Any]:
    call = await _require_call(conversation_id, recorder)
    agent_id, agent_name = _agent_for_call(call, settings)
    return build_conversation_data(
        call,
        await recorder.get_transcripts(call.id),
        await recorder.get_events(call.id),
        agent_id=agent_id,
        agent_name=agent_name,
    )


RECORDING_PROCESSING_SECS = 300


def _utc(moment: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; they are UTC."""
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _no_recording_reason(call: Call, key: str | None, settings: Settings) -> str | None:
    """Why there is nothing to play for this call, in words that point at the fix; None when there is a file."""
    cfg = settings.recording
    if not cfg.enabled:
        return "Recording is turned off on the voice service (RECORDING_ENABLED is not true)."
    if not cfg.ready:
        return "Recording is on but not fully configured (RECORDING_BUCKET, RECORDING_ACCESS_KEY, RECORDING_SECRET)."
    if call.status in (CallStatus.QUEUED.value, CallStatus.IN_PROGRESS.value):
        return "The call is still in progress; the recording is available after it ends."
    if not key:
        return "This call has no recording: it ended before the callee answered, or the recording failed to start."
    return None


@router.get("/conversations/{conversation_id}/audio")
async def get_conversation_audio(
    conversation_id: str,
    settings: Settings = Depends(get_settings),
    recorder: CallRecorder = Depends(get_call_recorder),
) -> StreamingResponse:
    call = await _require_call(conversation_id, recorder)
    recording = (call.metadata_json or {}).get("recording") or {}
    key = recording.get("key")
    reason = _no_recording_reason(call, key, settings)
    if reason:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=reason)
    try:
        stream = await open_recording(settings.recording, key, recording.get("file_type", "ogg"))
    except Exception as e:
        logger.exception("Could not open recording %s: %s", key, e)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Recording storage unavailable") from e
    if stream is None:
        ended = _utc(call.ended_at)
        just_ended = ended is not None and (datetime.now(UTC) - ended).total_seconds() < RECORDING_PROCESSING_SECS
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "The recording is still being processed; try again in a minute."
                if just_ended
                else f"The recording file is missing from the bucket ({key})."
            ),
        )
    headers = {"Content-Length": str(stream.content_length)} if stream.content_length else {}
    return StreamingResponse(stream.chunks, media_type=stream.content_type, headers=headers)
