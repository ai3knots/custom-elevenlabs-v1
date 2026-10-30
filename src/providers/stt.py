"""Speech-to-Text provider factory and configuration mapper."""

from __future__ import annotations

import os
from typing import Any

from livekit.agents import stt
from livekit.plugins import deepgram

from src.config import STTConfig
from src.providers.mock import MockSTT


def get_stt(config: STTConfig, api_key: str | None = None) -> stt.STT:
    """Instantiate an STT engine according to configuration.

    Supports:
    - 'deepgram': Deepgram Nova-3 in fixed language ('en-US', 'es') or multilingual ('multi') mode.
    - 'mock': MockSTT producing deterministic local transcriptions for tests and dry runs.
    """
    if config.provider == "mock":
        return MockSTT()

    if config.provider == "deepgram":
        resolved_key = api_key or os.getenv("DEEPGRAM_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Deepgram API key must be provided via argument or DEEPGRAM_API_KEY environment variable."
            )

        deepgram_kwargs: dict[str, Any] = {
            "model": config.model,
            "api_key": resolved_key,
            "interim_results": config.interim_results,
            "punctuate": config.punctuate,
            "smart_format": config.smart_format,
            "endpointing_ms": config.endpointing_ms,
        }

        if config.language_mode == "multi":
            deepgram_kwargs["language"] = "multi"
            if config.language_hint:
                deepgram_kwargs["language_hint"] = config.language_hint
        elif config.language_mode == "es":
            deepgram_kwargs["language"] = "es"
        else:
            deepgram_kwargs["language"] = config.language or "en-US"

        return deepgram.STT(**deepgram_kwargs)

    if config.provider == "assemblyai":
        raise NotImplementedError("AssemblyAI STT provider is deferred to future phase.")

    raise ValueError(f"Unsupported STT provider: {config.provider}")
