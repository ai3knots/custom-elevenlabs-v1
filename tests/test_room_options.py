"""Noise cancellation on the callee's audio: on by default, switchable, and never the reason a call fails."""

import logging

import pytest
from livekit import rtc
from livekit.agents import room_io
from livekit.plugins import noise_cancellation
from pydantic import ValidationError

from src.agent.room_options import build_noise_cancellation, build_room_options
from src.config import Settings, load_settings


def test_krisp_nc_is_the_default_and_the_filter_is_a_real_livekit_option() -> None:
    assert load_settings().conversation.noise_cancellation == "krisp_nc"
    assert isinstance(build_noise_cancellation("krisp_nc"), rtc.NoiseCancellationOptions)


def test_room_options_with_noise_cancellation_keep_hanging_up_on_close() -> None:
    options, on = build_room_options("krisp_nc")
    assert on is True
    assert options.delete_room_on_close is True
    assert isinstance(options.audio_input, room_io.AudioInputOptions)
    assert isinstance(options.audio_input.noise_cancellation, rtc.NoiseCancellationOptions)


def test_turning_it_off_leaves_the_room_options_as_they_were() -> None:
    options, on = build_room_options("none")
    assert on is False
    assert options.delete_room_on_close is True
    assert options == room_io.RoomOptions(delete_room_on_close=True)


def test_a_filter_that_cannot_load_does_not_fail_the_call(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> None:
        raise RuntimeError("plugin not available on this platform")

    monkeypatch.setattr(noise_cancellation, "NC", broken)
    with caplog.at_level(logging.WARNING, logger="posivex.session"):
        options, on = build_room_options("krisp_nc")
    assert on is False
    assert options == room_io.RoomOptions(delete_room_on_close=True)
    assert "could not be loaded" in caplog.text


def test_environment_variable_switches_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOISE_CANCELLATION", "none")
    assert load_settings().conversation.noise_cancellation == "none"


def test_an_unknown_mode_is_rejected_by_the_settings() -> None:
    with pytest.raises(ValidationError):
        Settings(conversation={"noise_cancellation": "bvc"})  # type: ignore[arg-type]


def test_a_single_call_can_turn_it_off_through_the_allowed_overrides() -> None:
    """For comparing a call with and without it: `conversation` is an allowed override section."""
    settings = load_settings(overrides={"conversation": {"noise_cancellation": "none"}})
    assert settings.conversation.noise_cancellation == "none"


def test_concurrency_ceiling_matches_the_ship_plan() -> None:
    assert load_settings().telephony.max_concurrent_calls == 20
