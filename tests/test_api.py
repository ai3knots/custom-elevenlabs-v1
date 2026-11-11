"""Unit tests for FastAPI endpoints: health and the ElevenLabs-compatible dial route."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.app import create_app
from src.config import Settings
from src.db.base import Base
from src.telephony.mock import MockTelephonyProvider


@pytest.fixture
async def test_env() -> AsyncGenerator[
    tuple[httpx.AsyncClient, MockTelephonyProvider, async_sessionmaker[AsyncSession]], None
]:
    """Setup in-memory SQLite database, mock telephony, and FastAPI test client."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    mock_telephony = MockTelephonyProvider()
    settings = Settings(
        elevenlabs_api_key="test_xi_api_key_12345",
        telephony={"provider": "mock", "max_concurrent_calls": 2},  # type: ignore[arg-type]
    )

    app = create_app(
        settings=settings,
        session_factory=session_factory,
        telephony_provider=mock_telephony,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, mock_telephony, session_factory

    await engine.dispose()


@pytest.mark.asyncio
async def test_health_probes(test_env: Any) -> None:
    """Test /health/live and /health/ready probes."""
    client, _, _ = test_env

    # 1. Liveness
    live_res = await client.get("/health/live")
    assert live_res.status_code == 200
    assert live_res.json() == {"status": "alive"}

    # 2. Readiness
    ready_res = await client.get("/health/ready")
    assert ready_res.status_code == 200
    assert ready_res.json() == {"status": "ready", "database": "connected"}


@pytest.mark.asyncio
async def test_convai_emulation_authentication(test_env: Any) -> None:
    """Test ElevenLabs authentication headers on emulation endpoints."""
    client, _, _ = test_env

    payload = {
        "to_number": "+14045551234",
        "conversation_initiation_client_data": {
            "dynamic_variables": {
                "business_name": "Tony's Pizza",
            }
        },
    }

    # 1. Missing xi-api-key header
    resp_no_auth = await client.post("/v1/convai/sip-trunk/outbound-call", json=payload)
    assert resp_no_auth.status_code == 401
    assert "Invalid or missing xi-api-key" in resp_no_auth.json()["detail"]

    # 2. Invalid xi-api-key header
    resp_bad_auth = await client.post(
        "/v1/convai/sip-trunk/outbound-call",
        json=payload,
        headers={"xi-api-key": "wrong_key"},
    )
    assert resp_bad_auth.status_code == 401

    # 3. Valid xi-api-key header
    resp_ok = await client.post(
        "/v1/convai/sip-trunk/outbound-call",
        json=payload,
        headers={"xi-api-key": "test_xi_api_key_12345"},
    )
    assert resp_ok.status_code == 200
    res_data = resp_ok.json()
    assert res_data["success"] is True
    assert res_data["conversation_id"].startswith("lk_")
    assert res_data["callSid"].startswith("CA")
    assert res_data["sip_call_id"].startswith("sip_")


@pytest.mark.asyncio
async def test_concurrency_limit(test_env: Any) -> None:
    """Test concurrency limiting (max_concurrent_calls = 2 in test_env)."""
    client, _, _ = test_env

    headers = {"xi-api-key": "test_xi_api_key_12345"}
    payload = {
        "to_number": "+14045551234",
        "conversation_initiation_client_data": {
            "dynamic_variables": {"business_name": "Call 1"}
        },
    }

    # Call 1 -> OK
    res1 = await client.post("/v1/convai/sip-trunk/outbound-call", json=payload, headers=headers)
    assert res1.status_code == 200

    # Call 2 -> OK
    res2 = await client.post("/v1/convai/sip-trunk/outbound-call", json=payload, headers=headers)
    assert res2.status_code == 200

    # Call 3 -> 429 Too Many Requests
    res3 = await client.post("/v1/convai/sip-trunk/outbound-call", json=payload, headers=headers)
    assert res3.status_code == 429
    assert "Maximum concurrent calls limit reached" in res3.json()["detail"]


