"""Live call state for a connected front end (the browser tester), over the room's data channel."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger("posivex.session")

TOPIC = "posivex_telemetry"


class Telemetry:
    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        """Broadcast real-time state and metrics over the WebRTC data channel. Never raises."""
        try:
            room = self._ctx.room
            if room and room.local_participant:
                message = json.dumps({"type": event_type, "timestamp": time.time(), **payload})
                await room.local_participant.publish_data(message.encode("utf-8"), reliable=True, topic=TOPIC)
        except Exception as e:
            logger.debug("Failed to publish telemetry to room: %s", e)
