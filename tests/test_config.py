"""Unit tests for configuration layering, schema validation, and phone normalization."""

import pytest
from pydantic import ValidationError

from src.config import (
    CallRequest,
    DynamicVariables,
    Settings,
    load_settings,
)


def test_load_defaults():
    """Verify that defaults.yaml loads expected baseline values."""
    settings = load_settings(persona="amelia")
    assert isinstance(settings, Settings)
    assert settings.persona == "amelia"
    assert settings.stt.provider == "deepgram"
    assert settings.stt.model == "nova-3"
    assert settings.llm.provider == "google"
    assert settings.llm.default_model == "gemini-3.5-flash"
    assert settings.tts.provider == "elevenlabs"
    assert settings.tts.voice_id == "eXpIbVcVbLo8ZJQDlDnl"
    assert settings.vad.provider == "silero"
    assert len(settings.conversation.interruption_ignore_terms) > 10


def test_persona_layering_michael():
    """Verify that Michael profile overrides persona-specific values while keeping defaults."""
    settings = load_settings(persona="michael")
    assert settings.persona == "michael"
    assert settings.tts.voice_id == "flq6f7yk4E4fJM5XTYuZ"
    # Shared defaults remain intact
    assert settings.stt.model == "nova-3"
    assert settings.llm.default_model == "gemini-3.5-flash"


def test_dispatch_overrides():
    """Verify that dispatch metadata overrides deep-merge over profiles and defaults."""
    overrides = {
        "stt": {"language_mode": "multi"},
        "llm": {"temperature": 0.3},
        "tts": {"voice_id": "custom_override_voice"},
    }
    settings = load_settings(persona="amelia", overrides=overrides)
    assert settings.stt.provider == "deepgram"  # preserved from defaults
    assert settings.stt.language_mode == "multi"
    assert settings.stt.model == "nova-3"  # Preserved from defaults
    assert settings.llm.temperature == 0.3
    assert settings.tts.voice_id == "custom_override_voice"


def test_call_request_phone_normalization():
    """Verify that phone numbers in various formats are normalized to E.164."""
    # Standard 10-digit US number
    req1 = CallRequest(phone="4045551234")
    assert req1.phone == "+14045551234"

    # US formatted with symbols
    req2 = CallRequest(phone="(404) 555-1234")
    assert req2.phone == "+14045551234"

    # 11-digit US number with leading 1
    req3 = CallRequest(phone="14045551234")
    assert req3.phone == "+14045551234"

    # Already valid E.164
    req4 = CallRequest(phone="+14045551234")
    assert req4.phone == "+14045551234"


def test_call_request_invalid_phone():
    """Verify that invalid phone numbers are rejected."""
    with pytest.raises(ValidationError):
        CallRequest(phone="not-a-phone-number")

    with pytest.raises(ValidationError):
        CallRequest(phone="123")  # Too short


def test_call_request_elevenlabs_aliases():
    """Verify ElevenLabs backwards-compatible field mapping (to_number and initiation data)."""
    payload = {
        "to_number": "+14045559999",
        "agent_id": "agent_test_123",
        "conversation_initiation_client_data": {
            "dynamic_variables": {
                "business_name": "Tony's Pizza & Grill",
                "business_context": "Pizza shop in Atlanta",
                "current_datetime": "Tuesday, October 20, 2026",
                "is_followup": "true",
                "followup_notes": "Needs terminal",
                "contact_name": "Tony",
            }
        },
    }
    req = CallRequest.model_validate(payload)
    assert req.phone == "+14045559999"
    assert req.agent_id == "agent_test_123"
    assert isinstance(req.dynamic_variables, DynamicVariables)
    assert req.dynamic_variables.business_name == "Tony's Pizza & Grill"
    assert req.dynamic_variables.is_followup == "true"
    assert req.dynamic_variables.contact_name == "Tony"



def test_single_model_is_gemini_flash():
    """One Gemini 3.5 Flash model serves the whole call; there is no per-state model."""
    settings = load_settings()
    assert settings.llm.default_model == "gemini-3.5-flash"
    assert not hasattr(settings.llm, "conversation_model")


def test_fallback_llm_differs_from_primary_model():
    from src.config import LLMConfig

    cfg = LLMConfig()
    assert cfg.fallback_model != cfg.default_model


def test_silence_and_endpointing_defaults():
    conv = load_settings().conversation
    assert conv.silence_end_call_seconds == 15.0
    assert conv.hold_duration_seconds == 30.0
    assert conv.no_speech_watchdog_seconds == 40.0
    assert (conv.min_endpointing_delay, conv.max_endpointing_delay) == (0.5, 1.5)


def test_opener_machine_detection_defaults_and_llm_env_overrides(monkeypatch):
    from src.config import load_settings

    conv = load_settings().conversation
    assert (conv.opener_delay_seconds, conv.opener_quiet_wait_seconds) == (1.0, 1.0)
    assert conv.machine_detection_enabled is True
    assert conv.machine_detection_model == "gemini-3.5-flash-lite"
    assert not hasattr(conv, "amd_timeout_seconds")  # the 3 s AMD gate is gone
    assert load_settings().conversation.max_user_speech_seconds == 8.0
    monkeypatch.setenv("LLM_DEFAULT_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "gemini-3.5-flash")
    settings = load_settings()
    assert settings.llm.default_model == "gemini-3.5-flash-lite"
    assert settings.llm.fallback_model == "gemini-3.5-flash"
