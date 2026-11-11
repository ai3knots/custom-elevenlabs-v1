"""ElevenLabs emulation route for Posivex-lead-gen zero-code migration."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from src.api.deps import (
    get_call_recorder,
    get_settings,
    get_telephony,
    verify_xi_api_key,
)
from src.api.services.dispatch import ConcurrencyLimitReached, DialFailed, place_call
from src.config import CallRequest, Settings
from src.persistence.recorder import CallRecorder
from src.telephony import TelephonyProvider

router = APIRouter(tags=["ElevenLabs Emulation"])


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
    try:
        placed = await place_call(request, settings=settings, recorder=recorder, telephony=telephony)
    except ConcurrencyLimitReached:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Maximum concurrent calls limit reached",
        ) from None
    except DialFailed as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Outbound dial failure: {e}",
        ) from e

    dispatch = placed.dispatch
    return {
        "success": True,
        "conversation_id": placed.conversation_id,
        "callSid": dispatch.call_sid or f"CA{uuid.uuid4().hex[:16]}",
        "sip_call_id": dispatch.sip_call_id or f"sip_{uuid.uuid4().hex[:12]}",
    }
