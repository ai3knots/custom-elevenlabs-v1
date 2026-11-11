"""Health and readiness probe endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.deps import get_session_factory

logger = logging.getLogger("posivex.api.health")
router = APIRouter(prefix="/health", tags=["Health"])


@router.get("", status_code=status.HTTP_200_OK)
@router.get("/", status_code=status.HTTP_200_OK)
async def health_check() -> dict[str, str]:
    """Default health check confirming the HTTP service is running."""
    return {"status": "healthy"}


@router.get("/live", status_code=status.HTTP_200_OK)
async def liveness_probe() -> dict[str, str]:
    """Basic liveness probe confirming the HTTP server is running."""
    return {"status": "alive"}


@router.get("/ready", status_code=status.HTTP_200_OK)
async def readiness_probe(
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> dict[str, Any]:
    """Readiness probe checking database connectivity."""
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ready", "database": "connected"}
    except Exception as e:
        logger.exception("Readiness probe database connection failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "unhealthy", "database": "disconnected", "error": str(e)},
        ) from e

