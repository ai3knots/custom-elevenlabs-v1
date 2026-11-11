"""Console-based telephony provider for interactive CLI demonstrations and dry-runs."""

from __future__ import annotations

import logging
import uuid

from src.config import CallRequest
from src.telephony.base import DispatchResult, TelephonyProvider, require_call_id

logger = logging.getLogger("posivex.telephony.console")


class ConsoleTelephonyProvider(TelephonyProvider):
    """Console provider that logs dialing events and tracks calls in memory."""

    def __init__(self) -> None:
        self.dispatched_calls: list[tuple[CallRequest, str]] = []

    async def dial(self, request: CallRequest, room_name: str) -> DispatchResult:
        call_id = require_call_id(request)
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

    async def close(self) -> None:
        logger.info("[CONSOLE TELEPHONY] Provider closed.")

