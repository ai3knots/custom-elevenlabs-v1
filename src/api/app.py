"""FastAPI application factory for Posivex Voice Calling service."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.routes import calls_router, convai_router, health_router
from src.config import Settings, load_settings
from src.db.base import get_engine, get_sessionmaker
from src.telephony import TelephonyProvider, get_telephony_provider


def create_app(
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    telephony_provider: TelephonyProvider | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    app_settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # Lifecycle initialization
        engine = None
        if not hasattr(app.state, "sessionmaker") or app.state.sessionmaker is None:
            engine = get_engine(app.state.settings.database_url)
            app.state.sessionmaker = get_sessionmaker(engine)

        if not hasattr(app.state, "telephony_provider") or app.state.telephony_provider is None:
            app.state.telephony_provider = get_telephony_provider(app.state.settings)

        yield

        # Shutdown cleanup
        if hasattr(app.state, "telephony_provider") and app.state.telephony_provider:
            await app.state.telephony_provider.close()

        if engine is not None:
            await engine.dispose()

    app = FastAPI(
        title="Posivex Voice Agent Calling Service",
        description="LiveKit outbound sales voice calling service with ElevenLabs ConvAI emulation.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Pre-populate state so it is immediately accessible
    app.state.settings = app_settings
    if session_factory is not None:
        app.state.sessionmaker = session_factory
    if telephony_provider is not None:
        app.state.telephony_provider = telephony_provider

    # Custom exception handler for validation errors to ensure consistent { success: false, detail: ... }
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        error_msg = errors[0]["msg"] if errors else "Invalid request payload"
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"success": False, "detail": error_msg, "errors": jsonable_encoder(errors)},
        )

    # Register Routers
    app.include_router(health_router)
    app.include_router(calls_router)
    app.include_router(convai_router)

    return app


# Default app instance for ASGI runners (e.g. uvicorn src.api.app:app)
app = create_app()
