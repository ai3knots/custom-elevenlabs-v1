"""Native LiveKit call management routes."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from src.api.deps import get_call_recorder, get_settings, get_telephony
from src.config import CallRequest, Settings
from src.db.models import CallStatus
from src.persistence.recorder import CallRecorder
from src.telephony import TelephonyProvider

logger = logging.getLogger("posivex.api.calls")
router = APIRouter(prefix="/api/v1/calls", tags=["Calls"])


@router.post("", status_code=status.HTTP_200_OK)
async def initiate_call(
    request: CallRequest,
    settings: Settings = Depends(get_settings),
    recorder: CallRecorder = Depends(get_call_recorder),
    telephony: TelephonyProvider = Depends(get_telephony),
) -> dict[str, Any]:
    """Initiate an outbound phone call and dispatch the voice agent worker."""
    # 1. Concurrency check
    active_count = await recorder.get_active_calls_count()
    if active_count >= settings.telephony.max_concurrent_calls:
        logger.warning(
            "Concurrency limit reached: %d active calls >= limit of %d",
            active_count,
            settings.telephony.max_concurrent_calls,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Maximum concurrent calls limit reached",
        )

    # 2. Identifier generation
    call_id = request.call_id or f"conv_{uuid.uuid4().hex[:14]}"
    room_name = f"call_{call_id}"
    request.call_id = call_id

    # 3. Create initial Call row and audit event
    await recorder.create_call(
        call_id=call_id,
        phone=request.phone,
        persona=request.persona,
        language=request.language,
        lead_id=request.lead_id,
        campaign_id=request.campaign_id,
        status=CallStatus.QUEUED.value,
        metadata_json={
            "dynamic_variables": request.dynamic_variables.model_dump(),
            "config_overrides": request.config_overrides,
        },
    )
    await recorder.record_event(
        call_id=call_id,
        event_type="call_requested",
        state="opener",
        payload={"phone": request.phone, "persona": request.persona},
    )

    # 4. Dispatch telephony
    try:
        dispatch_res = await telephony.dial(request, room_name=room_name)
    except Exception as e:
        logger.exception("Failed to dispatch call %s: %s", call_id, e)
        await recorder.update_call_status(
            call_id=call_id,
            status=CallStatus.FAILED.value,
            termination_reason="dial_failed",
        )
        await recorder.record_event(
            call_id=call_id,
            event_type="dial_failed",
            payload={"error": str(e)},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telephony dispatch failed: {e}",
        ) from e

    # 5. Update call status and log dispatch event
    await recorder.update_call_status(call_id=call_id, status=CallStatus.IN_PROGRESS.value)
    await recorder.record_event(
        call_id=call_id,
        event_type="call_dispatched",
        payload={
            "room_name": room_name,
            "participant_id": dispatch_res.participant_id,
            "sip_call_id": dispatch_res.sip_call_id,
        },
    )

    return {
        "success": True,
        "call_id": call_id,
        "room_name": room_name,
        "status": CallStatus.IN_PROGRESS.value,
    }


@router.get("/{call_id}", status_code=status.HTTP_200_OK)
async def get_call_details(
    call_id: str,
    recorder: CallRecorder = Depends(get_call_recorder),
) -> dict[str, Any]:
    """Retrieve call details and current status by call ID."""
    call = await recorder.get_call(call_id)
    if not call:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Call '{call_id}' not found",
        )

    return {
        "call_id": call.id,
        "lead_id": call.lead_id,
        "campaign_id": call.campaign_id,
        "phone": call.phone,
        "persona": call.persona,
        "language": call.language,
        "status": call.status,
        "call_outcome": call.call_outcome,
        "call_successful": call.call_successful,
        "total_cost": call.total_cost,
        "duration_secs": call.duration_secs,
        "started_at": call.started_at.isoformat() if call.started_at else None,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
    }
