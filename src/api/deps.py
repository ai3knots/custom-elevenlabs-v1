"""FastAPI dependency injection providers."""

from __future__ import annotations

import hmac
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

    # A dedicated POSIVEX_API_KEY wins; otherwise the ElevenLabs key doubles as the shared key.
    expected = settings.voice_api_key or settings.elevenlabs_api_key
    if expected and not hmac.compare_digest(xi_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing xi-api-key",
        )

    return xi_api_key



def verify_callback_secret(
    x_posivex_callback_secret: str | None = Header(None, alias="X-Posivex-Callback-Secret"),
    settings: Settings = Depends(get_settings),
) -> None:
    """Authenticate a LiveKit Cloud-hosted agent reporting a finished call."""
    expected = settings.results_callback_secret
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Results callback is not configured (RESULTS_CALLBACK_SECRET)",
        )
    if not x_posivex_callback_secret or not hmac.compare_digest(x_posivex_callback_secret, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid callback secret")
