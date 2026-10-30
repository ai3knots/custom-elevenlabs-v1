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
    assert settings.display_name == "Amelia"
    assert settings.gender == "female"
    assert settings.stt.provider == "deepgram"
    assert settings.stt.model == "nova-3"
    assert settings.llm.provider == "google"
    assert settings.llm.default_model == "gemini-3.5-flash-lite"
    assert settings.tts.provider == "elevenlabs"
    assert settings.tts.voice_id == "eXpIbVcVbLo8ZJQDlDnl"
    assert settings.vad.provider == "silero"
    assert len(settings.conversation.interruption_ignore_terms) > 10


def test_persona_layering_michael():
    """Verify that Michael profile overrides persona-specific values while keeping defaults."""
    settings = load_settings(persona="michael")
    assert settings.persona == "michael"
    assert settings.display_name == "Michael"
    assert settings.gender == "male"
    assert settings.tts.voice_id == "flq6f7yk4E4fJM5XTYuZ"
    # Shared defaults remain intact
    assert settings.stt.model == "nova-3"
    assert settings.llm.default_model == "gemini-3.5-flash-lite"


def test_dispatch_overrides():
    """Verify that dispatch metadata overrides deep-merge over profiles and defaults."""
    overrides = {
        "stt": {"provider": "mock", "language_mode": "multi"},
        "llm": {"temperature": 0.3},
        "tts": {"voice_id": "custom_override_voice"},
    }
    settings = load_settings(persona="amelia", overrides=overrides)
    assert settings.stt.provider == "mock"
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

