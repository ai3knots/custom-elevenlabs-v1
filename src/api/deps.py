"""FastAPI dependency injection providers."""

from __future__ import annotations

from typing import cast

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import Settings, load_settings
from src.persistence.recorder import CallRecorder
from src.telephony import TelephonyProvider, get_telephony_provider


def get_settings(request: Request) -> Settings:
    """Retrieve global settings from application state or environment."""
    if hasattr(request.app.state, "settings") and request.app.state.settings:
        return cast(Settings, request.app.state.settings)
    return load_settings()


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    """Retrieve database sessionmaker from application state."""
    if hasattr(request.app.state, "sessionmaker") and request.app.state.sessionmaker:
        return cast(async_sessionmaker[AsyncSession], request.app.state.sessionmaker)
    raise RuntimeError("Database sessionmaker not configured on application state.")


def get_call_recorder(
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> CallRecorder:
    """Provide a CallRecorder instance."""
    return CallRecorder(session_factory)


def get_telephony(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> TelephonyProvider:
    """Retrieve telephony provider from application state or create standard provider."""
    if hasattr(request.app.state, "telephony_provider") and request.app.state.telephony_provider:
        return cast(TelephonyProvider, request.app.state.telephony_provider)
    return get_telephony_provider(settings)


def verify_xi_api_key(
    xi_api_key: str | None = Header(None, alias="xi-api-key"),
    settings: Settings = Depends(get_settings),
) -> str:
    """Verify ElevenLabs emulation API key header."""
    if not xi_api_key or not xi_api_key.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing xi-api-key",
        )

    # If an elevenlabs_api_key is configured, enforce strict match
    if settings.elevenlabs_api_key and xi_api_key != settings.elevenlabs_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing xi-api-key",
        )

    return xi_api_key
