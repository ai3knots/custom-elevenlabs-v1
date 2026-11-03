"""Tests for backchannel filtering and turn-handling options."""

from __future__ import annotations

from livekit.agents import stt

from src.agent.interruption import build_ignore_keys, drop_backchannel_transcripts, is_ignorable
from src.agent.session_handler import build_turn_handling
from src.config import ConversationConfig, Settings
from src.fsm.states import STATE_REGISTRY, State

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
    th = build_turn_handling(settings, STATE_REGISTRY[State.OPENER])
    assert th["turn_detection"] == "vad"
    assert th["interruption"] == {"min_words": 1}
    assert th["endpointing"] == {"min_delay": 0.3, "max_delay": 2.0}

    patient = build_turn_handling(settings, STATE_REGISTRY[State.LOCK_IN])
    assert patient["endpointing"] == {"min_delay": 1.0, "max_delay": 6.0}

    settings.turn_detector.mode = "stt-only"
    assert build_turn_handling(settings, STATE_REGISTRY[State.OPENER])["turn_detection"] == "stt"
    settings.turn_detector.mode = "eou"
    assert build_turn_handling(settings, STATE_REGISTRY[State.OPENER])["turn_detection"] == "vad"


def test_tts_defaults_are_consistent():
    """config.py default and defaults.yaml must name the same TTS model."""
    from src.config import TTSConfig, load_settings

    assert load_settings().tts.model == TTSConfig().model == "eleven_v3_conversational"
