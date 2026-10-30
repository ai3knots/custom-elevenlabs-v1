"""Voice Activity Detection provider factory."""

from __future__ import annotations

from livekit.agents import vad
from livekit.plugins import silero

from src.config import VADConfig
from src.providers.mock import MockVAD


def get_vad(config: VADConfig) -> vad.VAD:
    """Instantiate a Voice Activity Detector according to configuration.

    Supports:
    - 'silero': Silero VAD with configurable speech/silence thresholds.
    - 'mock': MockVAD for offline dry runs and test execution.
    """
    if config.provider == "mock":
        return MockVAD()

    if config.provider == "silero":
        return silero.VAD.load(
            min_speech_duration=config.min_speech_duration,
            min_silence_duration=config.min_silence_duration,
            prefix_padding_duration=config.prefix_padding_duration,
        )

    raise ValueError(f"Unsupported VAD provider: {config.provider}")

