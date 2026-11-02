"""Console-based telephony provider for interactive CLI demonstrations and dry-runs."""

from __future__ import annotations

import logging
import uuid

from src.config import CallRequest
from src.telephony.base import DispatchResult, TelephonyProvider

logger = logging.getLogger("posivex.telephony.console")


class ConsoleTelephonyProvider(TelephonyProvider):
    """Console provider that logs dialing events and tracks calls in memory."""

    def __init__(self) -> None:
        self.dispatched_calls: list[tuple[CallRequest, str]] = []
        self.hung_up_calls: list[tuple[str, str]] = []

    async def dial(self, request: CallRequest, room_name: str) -> DispatchResult:
        call_id = request.call_id or f"conv_{uuid.uuid4().hex[:14]}"
        logger.info(
            "[CONSOLE TELEPHONY] Dialing %s in room %s (Persona: %s)",
            request.phone,
            room_name,
            request.persona,
        )

        result = DispatchResult(
            call_id=call_id,
            room_name=room_name,
            participant_id=f"console_{uuid.uuid4().hex[:8]}",
            sip_call_id=f"sip_console_{uuid.uuid4().hex[:12]}",
            call_sid=f"CA_console_{uuid.uuid4().hex[:16]}",
        )
        self.dispatched_calls.append((request, room_name))
        return result

    async def hangup(self, room_name: str, participant_id: str) -> None:
        logger.info(
            "[CONSOLE TELEPHONY] Hangup participant %s in room %s",
            participant_id,
            room_name,
        )
        self.hung_up_calls.append((room_name, participant_id))

    async def close(self) -> None:
        logger.info("[CONSOLE TELEPHONY] Provider closed.")

