"""Mock telephony provider for testing and offline environments."""

from __future__ import annotations

import uuid

from src.config import CallRequest
from src.telephony.base import DispatchResult, TelephonyProvider


class MockTelephonyProvider(TelephonyProvider):
    """Mock telephony provider tracking calls in memory."""

    def __init__(self) -> None:
        self.dispatched_calls: list[tuple[CallRequest, str]] = []
        self.hung_up_calls: list[tuple[str, str]] = []
        self.should_fail: bool = False
        self.failure_message: str = "Mock dial failure"

    async def dial(self, request: CallRequest, room_name: str) -> DispatchResult:
        if self.should_fail:
            raise RuntimeError(self.failure_message)

        call_id = request.call_id or f"conv_{uuid.uuid4().hex[:14]}"
        result = DispatchResult(
            call_id=call_id,
            room_name=room_name,
            participant_id=f"part_{uuid.uuid4().hex[:8]}",
            sip_call_id=f"sip_{uuid.uuid4().hex[:12]}",
            call_sid=f"CA{uuid.uuid4().hex[:16]}",
        )
        self.dispatched_calls.append((request, room_name))
        return result

    async def hangup(self, room_name: str, participant_id: str) -> None:
        self.hung_up_calls.append((room_name, participant_id))

    async def close(self) -> None:
        pass

