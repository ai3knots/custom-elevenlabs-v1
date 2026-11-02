"""Unit tests for FastAPI endpoints: health, native calls, and ElevenLabs emulation."""

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
        environment="development",
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
async def test_native_call_flow(test_env: Any) -> None:
    """Test POST /api/v1/calls and GET /api/v1/calls/{call_id}."""
    client, mock_telephony, _ = test_env

    payload = {
        "phone": "+14045551234",
        "persona": "amelia",
        "language": "en",
        "lead_id": "lead_12345",
        "dynamic_variables": {
            "business_name": "Tony's Pizza & Grill",
            "business_context": "Pizza restaurant in Atlanta",
            "current_datetime": "Monday, Nov 2, 2026 at 4:00 PM",
            "contact_name": "Tony",
        },
    }

    # 1. Initiate call
    resp = await client.post("/api/v1/calls", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    call_id = data["call_id"]
    assert call_id.startswith("conv_")
    assert data["status"] == "in_progress"
    assert len(mock_telephony.dispatched_calls) == 1

    # 2. Retrieve call details
    get_resp = await client.get(f"/api/v1/calls/{call_id}")
    assert get_resp.status_code == 200
    details = get_resp.json()
    assert details["call_id"] == call_id
    assert details["phone"] == "+14045551234"
    assert details["lead_id"] == "lead_12345"
    assert details["persona"] == "amelia"
    assert details["status"] == "in_progress"

    # 3. Retrieve non-existent call
    missing_resp = await client.get("/api/v1/calls/conv_missing")
    assert missing_resp.status_code == 404


@pytest.mark.asyncio
async def test_native_call_invalid_phone(test_env: Any) -> None:
    """Test validation rejection for invalid phone number."""
    client, _, _ = test_env

    payload = {
        "phone": "invalid_phone_number",
        "dynamic_variables": {
            "business_name": "Invalid Phone LLC",
        },
    }

    resp = await client.post("/api/v1/calls", json=payload)
    assert resp.status_code == 400
    assert resp.json()["success"] is False


@pytest.mark.asyncio
async def test_native_call_telephony_failure(test_env: Any) -> None:
    """Test error handling when telephony provider fails."""
    client, mock_telephony, _ = test_env
    mock_telephony.should_fail = True
    mock_telephony.failure_message = "SIP gateway timeout"

    payload = {
        "phone": "+14045551234",
        "dynamic_variables": {
            "business_name": "Tony's Pizza",
        },
    }

    resp = await client.post("/api/v1/calls", json=payload)
    assert resp.status_code == 502
    assert "SIP gateway timeout" in resp.json()["detail"]


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

    # 3. Valid xi-api-key header on both endpoints
    for endpoint in ["/v1/convai/sip-trunk/outbound-call", "/v1/convai/twilio/outbound-call"]:
        resp_ok = await client.post(
            endpoint,
            json=payload,
            headers={"xi-api-key": "test_xi_api_key_12345"},
        )
        assert resp_ok.status_code == 200
        res_data = resp_ok.json()
        assert res_data["success"] is True
        assert res_data["conversation_id"].startswith("conv_")
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


@pytest.mark.asyncio
async def test_convai_inbound_initiation_lookup(test_env: Any) -> None:
    """Test /api/webhooks/elevenlabs/initiation dynamic variables lookup."""
    client, _, _ = test_env

    # 1. Inbound query for completely new caller
    new_res = await client.post(
        "/api/webhooks/elevenlabs/initiation",
        json={"caller_id": "+14045559999"},
    )
    assert new_res.status_code == 200
    new_data = new_res.json()
    assert new_data["type"] == "conversation_initiation_client_data"
    assert new_data["dynamic_variables"]["caller_status"] == "new"
    assert new_data["dynamic_variables"]["caller_phone"] == "+14045559999"
    assert new_data["dynamic_variables"]["is_followup"] == "false"

    # 2. Initiate an outbound call to populate phone history
    headers = {"xi-api-key": "test_xi_api_key_12345"}
    await client.post(
        "/v1/convai/sip-trunk/outbound-call",
        json={
            "to_number": "+14045557777",
            "conversation_initiation_client_data": {
                "dynamic_variables": {
                    "business_name": "Atlanta BBQ Shack",
                    "contact_name": "Marcus",
                }
            },
        },
        headers=headers,
    )

    # 3. Inbound query for known caller
    known_res = await client.post(
        "/api/webhooks/elevenlabs/initiation",
        json={"caller_id": "+14045557777"},
    )
    assert known_res.status_code == 200
    known_data = known_res.json()
    assert known_data["dynamic_variables"]["caller_status"] == "known"
    assert known_data["dynamic_variables"]["business_name"] == "Atlanta BBQ Shack"
    assert known_data["dynamic_variables"]["contact_name"] == "Marcus"
    assert known_data["dynamic_variables"]["is_followup"] == "true"
