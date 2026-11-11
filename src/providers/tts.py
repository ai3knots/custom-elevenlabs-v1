"""Text-to-Speech provider factory and voice configuration mapper."""

from __future__ import annotations

import os
from typing import Any

from livekit.agents import tts
from livekit.plugins import elevenlabs

from src.config import TTSConfig


def get_tts(config: TTSConfig, api_key: str | None = None) -> tts.TTS:
    """Instantiate a TTS engine according to configuration.

    Supports:
    - 'elevenlabs': ElevenLabs conversational v3 or multilingual v2 streaming via WebSockets.
    """
    if config.provider == "elevenlabs":
        resolved_key = api_key if api_key is not None else os.getenv("ELEVENLABS_API_KEY")
        if not resolved_key:
            raise ValueError(
                "ElevenLabs API key must be provided via argument or ELEVENLABS_API_KEY environment variable."
            )

        voice_settings = elevenlabs.VoiceSettings(
            stability=config.stability,
            similarity_boost=config.similarity_boost,
        )

        eleven_kwargs: dict[str, Any] = {
            "model": config.model,
            "voice_id": config.voice_id,
            "api_key": resolved_key,
            "voice_settings": voice_settings,
        }

        return elevenlabs.TTS(**eleven_kwargs)

    raise ValueError(f"Unsupported TTS provider: {config.provider}")

