"""Unit tests for Prometheus metrics collectors and /metrics exposition endpoint."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.app import create_app
from src.config import Settings
from src.db.base import Base
from src.metrics.prometheus import (
    POSIVEX_CALLS_TOTAL,
    get_latest_metrics,
)
from src.telephony.mock import MockTelephonyProvider


def test_prometheus_metrics_generation() -> None:
    """Test generating Prometheus metrics exposition text."""
    POSIVEX_CALLS_TOTAL.labels(persona="amelia", language="en", status="in_progress").inc()

    data, content_type = get_latest_metrics()
    assert isinstance(data, bytes)
    text = data.decode("utf-8")
    assert "posivex_calls_total" in text
    assert "text/plain" in content_type


@pytest.mark.asyncio
async def test_api_metrics_endpoint() -> None:
    """Test scraping /metrics via FastAPI."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    settings = Settings()
    app = create_app(
        settings=settings,
        session_factory=session_factory,
        telephony_provider=MockTelephonyProvider(),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert "posivex_calls_total" in resp.text

    await engine.dispose()

