"""LiveKit SIP participant telephony provider."""

from __future__ import annotations

import logging
import uuid

from livekit import api

from src.config import CallRequest, Settings
from src.telephony.base import DispatchResult, TelephonyProvider

logger = logging.getLogger("posivex.telephony.livekit")


class LiveKitSIPProvider(TelephonyProvider):
    """Telephony provider that dials via LiveKit SIP participant API."""

    def __init__(
        self,
        settings: Settings,
        api_client: api.LiveKitAPI | None = None,
    ) -> None:
        self._settings = settings
        self._lkapi: api.LiveKitAPI | None = api_client

    def _get_api(self) -> api.LiveKitAPI:
        if self._lkapi is None:
            self._lkapi = api.LiveKitAPI(
                url=self._settings.livekit_url,
                api_key=self._settings.livekit_api_key,
                api_secret=self._settings.livekit_api_secret,
            )
        return self._lkapi

    async def dial(self, request: CallRequest, room_name: str) -> DispatchResult:
        lkapi = self._get_api()
        call_id = request.call_id or f"conv_{uuid.uuid4().hex[:14]}"
        trunk_id = request.phone_number_id or self._settings.telephony.sip_trunk_id

        if not trunk_id:
            raise ValueError("No SIP trunk ID configured or provided in call request")

        logger.info("Dispatching voice agent worker to room %s for call %s", room_name, call_id)
        # 1. Dispatch Voice Agent worker to the room
        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name="posivex-agent",
                room=room_name,
                metadata=request.model_dump_json(),
            )
        )

        callee_name = (
            request.dynamic_variables.contact_name
            or request.dynamic_variables.business_name
            or "Callee"
        )
        logger.info(
            "Creating SIP participant to dial %s in room %s via trunk %s (%s)",
            request.phone,
            room_name,
            trunk_id,
            callee_name,
        )

        # 2. Dial SIP participant
        sip_part = await lkapi.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=room_name,
                sip_trunk_id=trunk_id,
                sip_call_to=request.phone,
                participant_identity=f"phone_{request.phone}",
                participant_name=callee_name,
                participant_metadata=request.model_dump_json(),
                participant_attributes={
                    "call_id": call_id,
                    "persona": request.persona,
                    "language": request.language,
                    "phone": request.phone,
                },
                play_ringtone=True,
                max_call_duration={"seconds": self._settings.conversation.max_duration_seconds},
            )
        )

        return DispatchResult(
            call_id=call_id,
            room_name=room_name,
            participant_id=sip_part.participant_id,
            sip_call_id=sip_part.sip_call_id or f"sip_{uuid.uuid4().hex[:12]}",
            call_sid=f"CA{uuid.uuid4().hex[:16]}",
        )

    async def hangup(self, room_name: str, participant_id: str) -> None:
        lkapi = self._get_api()
        logger.info("Removing participant %s from room %s", participant_id, room_name)
        await lkapi.room.remove_participant(
            api.RoomParticipantIdentity(room=room_name, identity=participant_id)
        )

    async def close(self) -> None:
        if self._lkapi is not None:
            await self._lkapi.aclose()
            self._lkapi = None
