"""Large Language Model provider factory and FallbackAdapter constructor."""

from __future__ import annotations

import os
from typing import Any

from google.genai import types
from livekit.agents import llm
from livekit.plugins import google

from src.config import LLMConfig
from src.providers.mock import MockLLM


def get_llm(model: str, config: LLMConfig, api_key: str | None = None) -> llm.LLM:
    """Instantiate an LLM provider according to configuration.

    Supports:
    - 'google': Google Gemini models (gemini-3.5-flash-lite, gemini-3.5-flash, gemini-2.5-flash)
      with minimal thinking config and temperature 0.0.
    - 'mock': MockLLM returning deterministic responses for offline unit tests.
    """
    if config.provider == "mock":
        return MockLLM()

    if config.provider == "google":
        resolved_key = api_key or os.getenv("GEMINI_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Gemini API key must be provided via argument or GEMINI_API_KEY environment variable."
            )

        # Map thinking_level string to Google GenAI ThinkingConfig
        thinking_level_upper = config.thinking_level.upper()
        thinking_cfg = types.ThinkingConfig(
            thinking_level=thinking_level_upper,  # type: ignore[arg-type]
            include_thoughts=False,
        )

        google_kwargs: dict[str, Any] = {
            "model": model,
            "api_key": resolved_key,
            "temperature": config.temperature,
            "thinking_config": thinking_cfg,
        }

        return google.LLM(**google_kwargs)

    raise ValueError(f"Unsupported LLM provider: {config.provider}")


def create_fallback_llm(
    primary: llm.LLM,
    secondary: llm.LLM,
    config: LLMConfig,
) -> llm.FallbackAdapter:
    """Wrap primary and secondary LLM instances in a LiveKit FallbackAdapter."""
    return llm.FallbackAdapter(
        llm=[primary, secondary],
        attempt_timeout=config.cascade_timeout,
        max_retry_per_llm=config.max_retries,
    )
