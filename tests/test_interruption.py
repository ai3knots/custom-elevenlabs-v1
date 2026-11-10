"""Tests for backchannel filtering and turn-handling options."""

from __future__ import annotations

from livekit.agents import stt

from src.agent.interruption import build_ignore_keys, drop_backchannel_transcripts, is_ignorable
from src.agent.session_handler import build_turn_handling
from src.config import ConversationConfig, Settings

KEYS = build_ignore_keys(ConversationConfig().interruption_ignore_terms)


def test_is_ignorable_matches_backchannels_only():
    assert is_ignorable("Mm-hmm.", KEYS)
    assert is_ignorable("uh-huh", KEYS)
    assert is_ignorable("Okay, okay.", KEYS)
    assert is_ignorable("sí claro", KEYS)
    assert not is_ignorable("okay but what does it cost", KEYS)
    assert not is_ignorable("yes", KEYS)
    assert not is_ignorable("", KEYS)
    assert not is_ignorable("anything", frozenset())


def _event(kind: stt.SpeechEventType, text: str) -> stt.SpeechEvent:
    return stt.SpeechEvent(
        type=kind, alternatives=[stt.SpeechData(language="en", text=text)]
    )


async def _collect(events, speaking: bool):
    async def gen():
        for ev in events:
            yield ev

    return [ev async for ev in drop_backchannel_transcripts(gen(), KEYS, lambda: speaking)]


async def test_backchannel_dropped_only_while_agent_speaks():
    events = [
        _event(stt.SpeechEventType.INTERIM_TRANSCRIPT, "mm-hmm"),
        _event(stt.SpeechEventType.FINAL_TRANSCRIPT, "mm-hmm"),
        _event(stt.SpeechEventType.FINAL_TRANSCRIPT, "okay wait what"),
        stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH),
    ]
    spoken_over = await _collect(events, speaking=True)
    assert [e.type for e in spoken_over] == [
        stt.SpeechEventType.FINAL_TRANSCRIPT,
        stt.SpeechEventType.END_OF_SPEECH,
    ]
    assert spoken_over[0].alternatives[0].text == "okay wait what"

    # After a check question, "mm-hmm" is a real yes and must reach the agent.
    assert len(await _collect(events, speaking=False)) == 4


def test_build_turn_handling_maps_settings():
    settings = Settings()
    th = build_turn_handling(settings)
    assert th["turn_detection"] == "vad"
    assert th["interruption"] == {"min_words": 1, "discard_audio_if_uninterruptible": False}
    assert th["endpointing"] == {"min_delay": 0.5, "max_delay": 1.5}

    settings.turn_detector.mode = "stt-only"
    assert build_turn_handling(settings)["turn_detection"] == "stt"
    settings.turn_detector.mode = "eou"
    assert build_turn_handling(settings)["turn_detection"] == "vad"


def test_build_turn_handling_audio_turn_detector(monkeypatch):
    from livekit.agents import inference

    monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret")

    settings = Settings()
    for mode in ("v1", "v1-mini"):
        settings.turn_detector.mode = mode
        th = build_turn_handling(settings)
        assert isinstance(th["turn_detection"], inference.TurnDetector)
        # Endpointing and interruption settings are unchanged by the detector choice.
        assert th["endpointing"] == {"min_delay": 0.5, "max_delay": 1.5}
        assert th["interruption"] == {"min_words": 1, "discard_audio_if_uninterruptible": False}


def test_turn_detector_mode_env_override(monkeypatch):
    from src.config import load_settings

    monkeypatch.setenv("TURN_DETECTOR_MODE", "v1")
    assert load_settings().turn_detector.mode == "v1"


def test_tts_defaults_are_consistent():
    """config.py default and defaults.yaml must name the same TTS model."""
    from src.config import TTSConfig, load_settings

    assert load_settings().tts.model == TTSConfig().model == "eleven_v3_conversational"
