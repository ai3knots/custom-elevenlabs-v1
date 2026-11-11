"""Placing an outbound call: the flow behind the ElevenLabs-compatible dial route.

It has no FastAPI in it. The route turns `ConcurrencyLimitReached` and `DialFailed` into HTTP 429 and 500.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from src.config import CallRequest, Settings
from src.db.models import CallStatus
from src.metrics.prometheus import POSIVEX_CALLS_TOTAL
from src.persistence.recorder import CallRecorder
from src.telephony import DispatchResult, TelephonyProvider

logger = logging.getLogger("posivex.api.dispatch")


class ConcurrencyLimitReached(Exception):
    """Too many calls are already queued or in progress."""


class DialFailed(Exception):
    """The telephony provider could not dial. The message is the provider's error."""


@dataclass(frozen=True)
class PlacedCall:
    conversation_id: str
    dispatch: DispatchResult


async def place_call(
    request: CallRequest,
    *,
    settings: Settings,
    recorder: CallRecorder,
    telephony: TelephonyProvider,
) -> PlacedCall:
    """Record the call, dispatch the agent and dial. Raises `ConcurrencyLimitReached` or `DialFailed`."""
    # 1. Concurrency check
    active_count = await recorder.get_active_calls_count()
    if active_count >= settings.telephony.max_concurrent_calls:
        logger.warning(
            "Concurrency limit reached: %d active calls >= limit of %d",
            active_count,
            settings.telephony.max_concurrent_calls,
        )
        raise ConcurrencyLimitReached

    # Lead-gen picks an agent by id; the persona (prompt) behind that id decides who speaks.
    request.persona = settings.persona_for_agent(request.agent_id, default=request.persona)  # type: ignore[assignment]

    # 2. Conversation id. The "lk_" prefix is how lead-gen tells our conversations from ElevenLabs' (which are
    # "conv_..."), so it reads each one from the right provider.
    conversation_id = request.call_id or f"lk_{uuid.uuid4().hex[:14]}"
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
        raise DialFailed(str(e)) from e

    # 5. Update call status and log event
    await recorder.update_call_status(call_id=conversation_id, status=CallStatus.IN_PROGRESS.value)
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
    return PlacedCall(conversation_id=conversation_id, dispatch=dispatch_res)
