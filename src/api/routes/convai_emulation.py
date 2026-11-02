"""ElevenLabs emulation routes for Posivex-lead-gen zero-code migration."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from src.api.deps import (
    get_call_recorder,
    get_settings,
    get_telephony,
    verify_xi_api_key,
)
from src.config import CallRequest, Settings
from src.db.models import CallStatus
from src.metrics.prometheus import POSIVEX_CALLS_TOTAL
from src.persistence.recorder import CallRecorder
from src.telephony import TelephonyProvider

logger = logging.getLogger("posivex.api.convai_emulation")
router = APIRouter(tags=["ElevenLabs Emulation"])


class InboundInitiationRequest(BaseModel):
    """Payload schema for inbound call dynamic variables lookup."""

    caller_id: str


async def _handle_emulated_outbound_call(
    request: CallRequest,
    settings: Settings,
    recorder: CallRecorder,
    telephony: TelephonyProvider,
) -> dict[str, Any]:
    """Shared dispatch logic for SIP trunk and Twilio emulation endpoints."""
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

    # 2. Identifier generation matching ElevenLabs conv_ prefix
    conversation_id = request.call_id or f"conv_{uuid.uuid4().hex[:14]}"
    room_name = f"call_{conversation_id}"
    request.call_id = conversation_id

    # 3. Create initial Call row and audit event
    await recorder.create_call(
        call_id=conversation_id,
        phone=request.phone,
        persona=request.persona,
        language=request.language,
        lead_id=request.lead_id,
        campaign_id=request.campaign_id,
        status=CallStatus.QUEUED.value,
        metadata_json={
            "agent_id": request.agent_id,
            "phone_number_id": request.phone_number_id,
            "dynamic_variables": request.dynamic_variables.model_dump(),
            "config_overrides": request.config_overrides,
        },
    )
    await recorder.record_event(
        call_id=conversation_id,
        event_type="call_requested",
        state="opener",
        payload={
            "phone": request.phone,
            "agent_id": request.agent_id,
            "emulated_endpoint": True,
        },
    )

    # 4. Telephony dispatch
    try:
        dispatch_res = await telephony.dial(request, room_name=room_name)
    except Exception as e:
        logger.exception("Emulated outbound dial failed for %s: %s", conversation_id, e)
        await recorder.update_call_status(
            call_id=conversation_id,
            status=CallStatus.FAILED.value,
            termination_reason="dial_failed",
        )
        await recorder.record_event(
            call_id=conversation_id,
            event_type="dial_failed",
            payload={"error": str(e)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Outbound dial failure: {e}",
        ) from e

    # 5. Update call status and log event
    await recorder.update_call_status(
        call_id=conversation_id, status=CallStatus.IN_PROGRESS.value
    )
    POSIVEX_CALLS_TOTAL.labels(
        persona=request.persona, language=request.language, status=CallStatus.IN_PROGRESS.value
    ).inc()
    await recorder.record_event(
        call_id=conversation_id,
        event_type="call_dispatched",
        payload={
            "room_name": room_name,
            "participant_id": dispatch_res.participant_id,
            "sip_call_id": dispatch_res.sip_call_id,
        },
    )

    return {
        "success": True,
        "conversation_id": conversation_id,
        "callSid": dispatch_res.call_sid or f"CA{uuid.uuid4().hex[:16]}",
        "sip_call_id": dispatch_res.sip_call_id or f"sip_{uuid.uuid4().hex[:12]}",
    }


@router.post(
    "/v1/convai/sip-trunk/outbound-call",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_xi_api_key)],
)
async def convai_sip_outbound_call(
    request: CallRequest,
    settings: Settings = Depends(get_settings),
    recorder: CallRecorder = Depends(get_call_recorder),
    telephony: TelephonyProvider = Depends(get_telephony),
) -> dict[str, Any]:
    """Emulated ElevenLabs SIP trunk outbound call endpoint."""
    return await _handle_emulated_outbound_call(request, settings, recorder, telephony)


@router.post(
    "/v1/convai/twilio/outbound-call",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_xi_api_key)],
)
async def convai_twilio_outbound_call(
    request: CallRequest,
    settings: Settings = Depends(get_settings),
    recorder: CallRecorder = Depends(get_call_recorder),
    telephony: TelephonyProvider = Depends(get_telephony),
) -> dict[str, Any]:
    """Emulated ElevenLabs Twilio outbound call endpoint."""
    return await _handle_emulated_outbound_call(request, settings, recorder, telephony)


@router.post(
    "/api/webhooks/elevenlabs/initiation",
    status_code=status.HTTP_200_OK,
)
async def convai_inbound_initiation_lookup(
    request: InboundInitiationRequest,
    recorder: CallRecorder = Depends(get_call_recorder),
) -> dict[str, Any]:
    """Emulated ElevenLabs inbound call dynamic variables lookup."""
    caller_phone = request.caller_id
    recent_call = await recorder.get_recent_call_by_phone(caller_phone)

    business_name = "Valued Merchant"
    contact_name = ""
    is_followup = "false"
    followup_notes = ""

    if recent_call and recent_call.metadata_json:
        dyn = recent_call.metadata_json.get("dynamic_variables", {})
        business_name = dyn.get("business_name", business_name)
        contact_name = dyn.get("contact_name", contact_name)
        is_followup = "true"
        followup_notes = "Inbound callback from previous contact"

    formatted_time = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")

    return {
        "type": "conversation_initiation_client_data",
        "dynamic_variables": {
            "call_direction": "inbound",
            "caller_status": "known" if recent_call else "new",
            "caller_phone": caller_phone,
            "business_name": business_name,
            "business_context": "Inbound merchant inquiry",
            "current_datetime": formatted_time,
            "is_followup": is_followup,
            "followup_notes": followup_notes,
            "contact_name": contact_name,
        },
    }

