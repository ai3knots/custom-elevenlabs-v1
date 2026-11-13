"""Unit tests for telephony abstraction layer: Mock, Console, LiveKitSIP, and factory."""

from __future__ import annotations

import logging
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
        call_id="conv_test_1",
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
    assert res.call_id == "conv_test_1"
    assert res.room_name == "room_123"
    assert res.participant_id.startswith("part_")
    assert res.call_sid is not None and res.call_sid.startswith("CA")
    assert len(provider.dispatched_calls) == 1

    # 2. Simulated failure
    provider.should_fail = True
    provider.failure_message = "Simulated carrier error"
    with pytest.raises(RuntimeError, match="Simulated carrier error"):
        await provider.dial(req, room_name="room_456")

    # 3. Close
    await provider.close()


@pytest.mark.asyncio
async def test_console_telephony_provider() -> None:
    """Test ConsoleTelephonyProvider operations."""
    provider = ConsoleTelephonyProvider()
    req = _make_call_request()

    res = await provider.dial(req, room_name="console_room_1")
    assert res.call_id == "conv_test_1"
    assert res.room_name == "console_room_1"
    assert res.participant_id.startswith("console_")
    assert len(provider.dispatched_calls) == 1

    await provider.close()


@pytest.mark.asyncio
async def test_livekit_sip_provider_dial() -> None:
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
    assert res.call_id == "conv_test_1"
    assert res.room_name == "sip_room_1"
    assert res.participant_id == "part_livekit_999"
    assert res.sip_call_id == "sip_livekit_call_888"

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



@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("phnum_posivex_trunk", "ST_default_trunk"),  # lead-gen's number id is not a LiveKit trunk
        (None, "ST_default_trunk"),
        ("ST_explicit_trunk", "ST_explicit_trunk"),  # an explicit LiveKit trunk id still wins
    ],
)
async def test_livekit_trunk_choice_ignores_leadgen_number_ids(requested: str | None, expected: str) -> None:
    mock_lkapi = MagicMock()
    mock_lkapi.agent_dispatch.create_dispatch = AsyncMock()
    mock_lkapi.sip.create_sip_participant = AsyncMock(
        return_value=api.SIPParticipantInfo(participant_id="p", sip_call_id="s")
    )
    settings = Settings(telephony={"provider": "livekit_sip", "sip_trunk_id": "ST_default_trunk"})  # type: ignore[arg-type]
    provider = LiveKitSIPProvider(settings, api_client=mock_lkapi)

    await provider.dial(_make_call_request(trunk_id=requested), room_name="room")

    assert mock_lkapi.sip.create_sip_participant.call_args[0][0].sip_trunk_id == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [MockTelephonyProvider(), ConsoleTelephonyProvider()])
async def test_dialing_without_a_call_id_is_an_error(provider: object) -> None:
    """The API assigns the call id before dialing; a provider never invents one."""
    request = _make_call_request()
    request.call_id = None
    with pytest.raises(ValueError, match="call_id must be set"):
        await provider.dial(request, room_name="room")  # type: ignore[attr-defined]


def _lk_provider(sip_error: Exception | None = None, delete_error: Exception | None = None) -> tuple[LiveKitSIPProvider, MagicMock]:
    lkapi = MagicMock()
    lkapi.agent_dispatch.create_dispatch = AsyncMock()
    lkapi.sip.create_sip_participant = AsyncMock(side_effect=sip_error)
    lkapi.room.delete_room = AsyncMock(side_effect=delete_error)
    settings = Settings(telephony={"provider": "livekit_sip", "sip_trunk_id": "ST_default_trunk"})  # type: ignore[arg-type]
    return LiveKitSIPProvider(settings, api_client=lkapi), lkapi


@pytest.mark.asyncio
async def test_a_failed_phone_leg_deletes_the_room_so_the_dispatched_agent_does_not_wait() -> None:
    provider, lkapi = _lk_provider(sip_error=RuntimeError("trunk rejected"))
    with pytest.raises(RuntimeError, match="trunk rejected"):
        await provider.dial(_make_call_request(), room_name="sip_room_9")
    lkapi.agent_dispatch.create_dispatch.assert_awaited_once()
    lkapi.room.delete_room.assert_awaited_once()
    assert lkapi.room.delete_room.call_args[0][0].room == "sip_room_9"


@pytest.mark.asyncio
async def test_the_original_dial_error_survives_a_failed_room_cleanup() -> None:
    provider, _ = _lk_provider(sip_error=RuntimeError("trunk rejected"), delete_error=RuntimeError("api down"))
    with pytest.raises(RuntimeError, match="trunk rejected"):
        await provider.dial(_make_call_request(), room_name="sip_room_9")


@pytest.mark.asyncio
async def test_a_successful_dial_does_not_touch_the_room() -> None:
    provider, lkapi = _lk_provider()
    lkapi.sip.create_sip_participant.return_value = MagicMock(participant_id="p1", sip_call_id="s1")
    await provider.dial(_make_call_request(), room_name="sip_room_9")
    lkapi.room.delete_room.assert_not_awaited()


def _provider_with_numbers() -> tuple[LiveKitSIPProvider, MagicMock]:
    mock_lkapi = MagicMock()
    mock_lkapi.agent_dispatch.create_dispatch = AsyncMock()
    mock_lkapi.sip.create_sip_participant = AsyncMock(return_value=api.SIPParticipantInfo(participant_id="p", sip_call_id="s"))
    settings = Settings(
        telephony={"provider": "livekit_sip", "sip_trunk_id": "ST_default"},  # type: ignore[arg-type]
        phone_numbers=[  # type: ignore[arg-type]
            {"phone_number_id": "phnum_a", "phone_number": "+14705550001", "sip_trunk_id": "ST_a"},
            {"phone_number_id": "phnum_b", "phone_number": "+14705550002", "sip_trunk_id": "ST_b"},
            {"phone_number_id": "phnum_untrunked", "phone_number": "+14705550003"},
        ],
    )
    return LiveKitSIPProvider(settings, api_client=mock_lkapi), mock_lkapi


async def _trunk_used(provider: LiveKitSIPProvider, lkapi: MagicMock, phone_number_id: str | None) -> str:
    await provider.dial(_make_call_request(trunk_id=phone_number_id), room_name="room")
    return lkapi.sip.create_sip_participant.call_args[0][0].sip_trunk_id


@pytest.mark.asyncio
async def test_each_calling_number_dials_through_its_own_trunk() -> None:
    """Every Telnyx number has its own connection, so the number lead-gen picks decides the LiveKit trunk."""
    provider, lkapi = _provider_with_numbers()
    assert await _trunk_used(provider, lkapi, "phnum_a") == "ST_a"
    assert await _trunk_used(provider, lkapi, "phnum_b") == "ST_b"


@pytest.mark.asyncio
async def test_a_number_without_a_trunk_or_no_number_at_all_uses_the_default_trunk() -> None:
    provider, lkapi = _provider_with_numbers()
    assert await _trunk_used(provider, lkapi, "phnum_untrunked") == "ST_default"
    assert await _trunk_used(provider, lkapi, None) == "ST_default"


@pytest.mark.asyncio
async def test_an_explicit_livekit_trunk_id_is_used_as_given() -> None:
    provider, lkapi = _provider_with_numbers()
    assert await _trunk_used(provider, lkapi, "ST_special") == "ST_special"


@pytest.mark.asyncio
async def test_an_unknown_number_id_still_dials_through_the_default_trunk_and_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    provider, lkapi = _provider_with_numbers()
    with caplog.at_level(logging.WARNING):
        assert await _trunk_used(provider, lkapi, "phnum_mystery") == "ST_default"
    assert "phnum_mystery" in caplog.text and "not configured" in caplog.text
