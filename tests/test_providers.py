"""Unit tests for the provider factories."""

import pytest
from livekit.agents.llm import FallbackAdapter
from livekit.plugins import deepgram, elevenlabs, google, silero

from src.config import LLMConfig, STTConfig, TTSConfig, VADConfig
from src.providers import create_fallback_llm, get_llm, get_stt, get_tts, get_vad
from tests.support.mock_providers import MockLLM


def test_stt_builds_deepgram():
    """The STT factory builds Deepgram with the configured model and language."""
    real_cfg = STTConfig(provider="deepgram", model="nova-3", language_mode="en")
    real_instance = get_stt(real_cfg, api_key="test_deepgram_key")
    assert isinstance(real_instance, deepgram.STT)
    assert real_instance._opts.model == "nova-3"
    assert real_instance._opts.language == "en-US"


def test_stt_language_modes():
    """Verify STT configures Spanish and multilingual modes correctly."""
    # Spanish mode
    es_cfg = STTConfig(provider="deepgram", model="nova-3", language_mode="es")
    es_stt = get_stt(es_cfg, api_key="test_key")
    assert isinstance(es_stt, deepgram.STT)
    assert es_stt._opts.language == "es"

    # Multilingual mode
    multi_cfg = STTConfig(
        provider="deepgram",
        model="nova-3",
        language_mode="multi",
        language_hint=["en", "es"],
    )
    multi_stt = get_stt(multi_cfg, api_key="test_key")
    assert isinstance(multi_stt, deepgram.STT)
    assert multi_stt._opts.language == "multi"
    assert multi_stt._opts.language_hint == ["en", "es"]


def test_llm_builds_google_gemini():
    """The LLM factory builds Gemini, with thinking_level for 3.5 and thinking_budget for 2.5."""
    # Gemini 3.5 supports thinking_level
    google_cfg = LLMConfig(provider="google", temperature=0.0, thinking_level="minimal")
    google_instance = get_llm("gemini-3.5-flash-lite", google_cfg, api_key="test_gemini_key")
    assert isinstance(google_instance, google.LLM)
    assert google_instance._opts.model == "gemini-3.5-flash-lite"
    assert google_instance._opts.temperature == 0.0

    # Real Google Gemini LLM (2.5 uses thinking_budget to prevent ValueError)
    google_25_instance = get_llm("gemini-2.5-flash", google_cfg, api_key="test_gemini_key")
    assert isinstance(google_25_instance, google.LLM)
    assert google_25_instance._opts.model == "gemini-2.5-flash"


def test_tts_builds_elevenlabs():
    """The TTS factory builds ElevenLabs with the configured model and voice."""
    eleven_cfg = TTSConfig(
        provider="elevenlabs",
        model="eleven_v3_conversational",
        voice_id="eXpIbVcVbLo8ZJQDlDnl",
    )
    eleven_instance = get_tts(eleven_cfg, api_key="test_eleven_key")
    assert isinstance(eleven_instance, elevenlabs.TTS)
    assert eleven_instance._opts.model == "eleven_v3_conversational"
    assert eleven_instance._opts.voice_id == "eXpIbVcVbLo8ZJQDlDnl"


def test_vad_builds_silero():
    """The VAD factory builds Silero."""
    silero_cfg = VADConfig(provider="silero", min_speech_duration=0.05)
    silero_instance = get_vad(silero_cfg)
    assert isinstance(silero_instance, silero.VAD)


def test_fallback_adapter_wiring():
    """Verify FallbackAdapter chains primary and secondary LLMs with configured timeout."""
    primary = MockLLM(default_response="primary")
    secondary = MockLLM(default_response="secondary")
    cfg = LLMConfig(cascade_timeout=12.0, max_retries=0)

    adapter = create_fallback_llm(primary, secondary, cfg)
    assert isinstance(adapter, FallbackAdapter)
    assert len(adapter._llm_instances) == 2
    assert adapter._attempt_timeout == 12.0
    assert adapter._max_retry_per_llm == 0

    # Minimum timeout clamped to 10.0s for Gemini API compliance
    clamped_cfg = LLMConfig(cascade_timeout=4.0)
    clamped_adapter = create_fallback_llm(primary, secondary, clamped_cfg)
    assert clamped_adapter._attempt_timeout == 10.0


def test_missing_api_keys_raise_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that calling factory without API key raises descriptive ValueError."""
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    stt_cfg = STTConfig(provider="deepgram")
    with pytest.raises(ValueError, match="Deepgram API key must be provided"):
        get_stt(stt_cfg, api_key=None)

    llm_cfg = LLMConfig(provider="google")
    with pytest.raises(ValueError, match="Gemini API key must be provided"):
        get_llm("gemini-3.5-flash", llm_cfg, api_key=None)

    tts_cfg = TTSConfig(provider="elevenlabs")
    with pytest.raises(ValueError, match="ElevenLabs API key must be provided"):
        get_tts(tts_cfg, api_key=None)
