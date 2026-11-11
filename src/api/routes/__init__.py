"""API route packages."""

from __future__ import annotations

from src.api.routes.convai_emulation import router as convai_router
from src.api.routes.convai_read import router as convai_read_router
from src.api.routes.health import router as health_router
from src.api.routes.internal import router as internal_router

__all__ = ["convai_read_router", "convai_router", "health_router", "internal_router"]
