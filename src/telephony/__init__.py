"""Telephony provider interfaces and factory."""

from __future__ import annotations

from src.config import Settings
from src.telephony.base import DispatchResult, TelephonyProvider
from src.telephony.livekit_sip import LiveKitSIPProvider
from src.telephony.mock import MockTelephonyProvider


def get_telephony_provider(settings: Settings) -> TelephonyProvider:
    """Create a telephony provider based on configuration settings."""
    if settings.telephony.provider == "livekit_sip":
        return LiveKitSIPProvider(settings)
    return MockTelephonyProvider()


__all__ = [
    "DispatchResult",
    "LiveKitSIPProvider",
    "MockTelephonyProvider",
    "TelephonyProvider",
    "get_telephony_provider",
]
