"""Base abstract class and models for telephony providers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from src.config import CallRequest


class DispatchResult(BaseModel):
    """Result of an outbound call dispatch operation."""

    call_id: str
    room_name: str
    participant_id: str
    sip_call_id: str | None = None
    call_sid: str | None = None
    extra: dict[str, str] = Field(default_factory=dict)


class TelephonyProvider(ABC):
    """Abstract interface for initiating and managing telephony calls."""

    @abstractmethod
    async def dial(self, request: CallRequest, room_name: str) -> DispatchResult:
        """Dial an outbound phone number and connect it to a room."""
        ...

    @abstractmethod
    async def hangup(self, room_name: str, participant_id: str) -> None:
        """Hang up or remove a participant from an active room."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close provider resources, HTTP clients, and connections."""
        ...
