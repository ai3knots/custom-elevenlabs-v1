"""Unit tests for telephony abstraction layer: Mock, Console, LiveKitSIP, and factory."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from livekit import api

from src.config import CallRequest, DynamicVariables, Settings
from src.telephony import (
    ConsoleTelephonyProvider,
    LiveKitSIPProvider,
    MockTelephonyProvider,
    get_telephony_provider,
)


def _make_call_request(phone: str = "+14045551234", trunk_id: str | None = None) -> CallRequest:
    return CallRequest(
        phone=phone,
        persona="amelia",
        language="en",
        phone_number_id=trunk_id,
        dynamic_variables=DynamicVariables(
            business_name="Tony's Pizza",
            contact_name="Tony",
        ),
    )


@pytest.mark.asyncio
async def test_mock_telephony_provider() -> None:
    """Test MockTelephonyProvider operations and failure simulation."""
    provider = MockTelephonyProvider()
    req = _make_call_request()

    # 1. Successful dial
    res = await provider.dial(req, room_name="room_123")
    assert res.call_id.startswith("conv_")
    assert res.room_name == "room_123"
    assert res.participant_id.startswith("part_")
    assert res.call_sid is not None and res.call_sid.startswith("CA")
    assert len(provider.dispatched_calls) == 1

    # 2. Hangup
    await provider.hangup("room_123", res.participant_id)
    assert len(provider.hung_up_calls) == 1
    assert provider.hung_up_calls[0] == ("room_123", res.participant_id)

    # 3. Simulated failure
    provider.should_fail = True
    provider.failure_message = "Simulated carrier error"
    with pytest.raises(RuntimeError, match="Simulated carrier error"):
        await provider.dial(req, room_name="room_456")

    # 4. Close
    await provider.close()


@pytest.mark.asyncio
async def test_console_telephony_provider() -> None:
    """Test ConsoleTelephonyProvider operations."""
    provider = ConsoleTelephonyProvider()
    req = _make_call_request()

    res = await provider.dial(req, room_name="console_room_1")
    assert res.call_id.startswith("conv_")
    assert res.room_name == "console_room_1"
    assert res.participant_id.startswith("console_")
    assert len(provider.dispatched_calls) == 1

    await provider.hangup("console_room_1", res.participant_id)
    assert len(provider.hung_up_calls) == 1

    await provider.close()


@pytest.mark.asyncio
async def test_livekit_sip_provider_dial_and_hangup() -> None:
    """Test LiveKitSIPProvider dispatch and SIP participant creation with mocked LiveKitAPI."""
    mock_lkapi = MagicMock()
    mock_lkapi.agent_dispatch = MagicMock()
    mock_lkapi.agent_dispatch.create_dispatch = AsyncMock()

    mock_sip_info = api.SIPParticipantInfo(
        participant_id="part_livekit_999",
        participant_identity="phone_+14045551234",
        room_name="sip_room_1",
        sip_call_id="sip_livekit_call_888",
    )
    mock_lkapi.sip = MagicMock()
    mock_lkapi.sip.create_sip_participant = AsyncMock(return_value=mock_sip_info)

    mock_lkapi.room = MagicMock()
    mock_lkapi.room.remove_participant = AsyncMock()
    mock_lkapi.aclose = AsyncMock()

    settings = Settings(
        telephony={"provider": "livekit_sip", "sip_trunk_id": "ST_default_trunk"},  # type: ignore[arg-type]
    )
    provider = LiveKitSIPProvider(settings, api_client=mock_lkapi)

    req = _make_call_request()
    res = await provider.dial(req, room_name="sip_room_1")

    # 1. Verify agent dispatch
    mock_lkapi.agent_dispatch.create_dispatch.assert_awaited_once()
    dispatch_call_arg = mock_lkapi.agent_dispatch.create_dispatch.call_args[0][0]
    assert dispatch_call_arg.agent_name == "posivex-agent"
    assert dispatch_call_arg.room == "sip_room_1"

    # 2. Verify SIP participant creation
    mock_lkapi.sip.create_sip_participant.assert_awaited_once()
    sip_call_arg = mock_lkapi.sip.create_sip_participant.call_args[0][0]
    assert sip_call_arg.room_name == "sip_room_1"
    assert sip_call_arg.sip_trunk_id == "ST_default_trunk"
    assert sip_call_arg.sip_call_to == "+14045551234"
    assert sip_call_arg.participant_identity == "phone_+14045551234"
    assert sip_call_arg.participant_name == "Tony"
    assert sip_call_arg.participant_attributes["call_id"] == res.call_id

    # 3. Verify returned result
    assert res.call_id.startswith("conv_")
    assert res.room_name == "sip_room_1"
    assert res.participant_id == "part_livekit_999"
    assert res.sip_call_id == "sip_livekit_call_888"

    # 4. Verify hangup
    await provider.hangup("sip_room_1", "part_livekit_999")
    mock_lkapi.room.remove_participant.assert_awaited_once()
    remove_arg = mock_lkapi.room.remove_participant.call_args[0][0]
    assert remove_arg.room == "sip_room_1"
    assert remove_arg.identity == "part_livekit_999"

    # 5. Verify close
    await provider.close()
    mock_lkapi.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_livekit_sip_missing_trunk_id() -> None:
    """Test error when no SIP trunk ID is provided in request or config."""
    mock_lkapi = MagicMock()
    settings = Settings(
        telephony={"provider": "livekit_sip", "sip_trunk_id": None},  # type: ignore[arg-type]
    )
    provider = LiveKitSIPProvider(settings, api_client=mock_lkapi)

    req = _make_call_request(trunk_id=None)
    with pytest.raises(ValueError, match="No SIP trunk ID configured"):
        await provider.dial(req, room_name="room_missing_trunk")


def test_telephony_factory() -> None:
    """Test get_telephony_provider factory resolving based on settings."""
    # 1. Mock
    settings_mock = Settings(telephony={"provider": "mock"})  # type: ignore[arg-type]
    provider_mock = get_telephony_provider(settings_mock)
    assert isinstance(provider_mock, MockTelephonyProvider)

    # 2. Console
    settings_console = Settings(telephony={"provider": "console"})  # type: ignore[arg-type]
    provider_console = get_telephony_provider(settings_console)
    assert isinstance(provider_console, ConsoleTelephonyProvider)

    # 3. LiveKit SIP
    settings_livekit = Settings(telephony={"provider": "livekit_sip"})  # type: ignore[arg-type]
    provider_livekit = get_telephony_provider(settings_livekit)
    assert isinstance(provider_livekit, LiveKitSIPProvider)
