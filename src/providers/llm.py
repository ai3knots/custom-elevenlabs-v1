"""Large Language Model provider factory and FallbackAdapter constructor."""

from __future__ import annotations

import logging
import os
from typing import Any

from google.genai import types
from livekit.agents import llm
from livekit.plugins import google

from src.config import LLMConfig

logger = logging.getLogger("posivex.providers.llm")


# Model families identified by a substring of their name.
THINKING_BUDGET_MODEL_MARKERS = ("2.5", "2.0")  # use thinking_budget, not thinking_level
NO_MINIMAL_THINKING_MARKERS = ("3.8",)  # need thinking_level LOW at least


def uses_thinking_budget(model: str) -> bool:
    """True for Gemini 2.x models, which take `thinking_budget` instead of `thinking_level`."""
    return any(marker in model for marker in THINKING_BUDGET_MODEL_MARKERS)


def get_llm(model: str, config: LLMConfig, api_key: str | None = None) -> llm.LLM:
    """Instantiate an LLM provider according to configuration.

    Supports:
    - 'google': Google Gemini models (e.g. gemini-3.5-flash-lite, gemini-3.5-flash)
      with model-appropriate thinking config and temperature 0.0.
    """
    if config.provider == "google":
        resolved_key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Gemini API key must be provided via argument or GEMINI_API_KEY environment variable."
            )

        # Configure thinking parameters based on model family
        # Gemini 3.5+ supports thinking_level ("MINIMAL", "LOW", etc.)
        # Gemini 2.x uses thinking_budget (int)
        supports_level = not uses_thinking_budget(model)

        if supports_level:
            thinking_level_upper = config.thinking_level.upper()
            if any(m in model for m in NO_MINIMAL_THINKING_MARKERS) and thinking_level_upper == "MINIMAL":
                thinking_level_upper = "LOW"
            thinking_cfg = types.ThinkingConfig(
                thinking_level=thinking_level_upper,  # type: ignore[arg-type]
                include_thoughts=False,
            )
        else:
            thinking_cfg = types.ThinkingConfig(
                thinking_budget=0,
            )

        google_kwargs: dict[str, Any] = {
            "model": model,
            "api_key": resolved_key,
            "temperature": config.temperature,
            "thinking_config": thinking_cfg,
        }

        return google.LLM(**google_kwargs)

    raise ValueError(f"Unsupported LLM provider: {config.provider}")


def share_thought_signatures(primary: llm.LLM, secondary: llm.LLM) -> None:
    """Make both Gemini clients use one `_thought_signatures` dict, so a tool-calling turn that starts on one
    model can continue on the other (`FallbackAdapter` may switch mid-conversation).

    This writes a private attribute of the LiveKit Google plugin; the exact version pin (livekit-agents==1.8.10)
    is what keeps it safe, and LLMs without that attribute (the test mocks) are left alone.
    """
    p_sig = getattr(primary, "_thought_signatures", None)
    s_sig = getattr(secondary, "_thought_signatures", None)
    if isinstance(p_sig, dict) and isinstance(s_sig, dict):
        p_sig.update(s_sig)
        object.__setattr__(secondary, "_thought_signatures", p_sig)


def create_fallback_llm(
    primary: llm.LLM,
    secondary: llm.LLM,
    config: LLMConfig,
) -> llm.FallbackAdapter:
    """Wrap primary and secondary LLM instances in a LiveKit FallbackAdapter."""
    share_thought_signatures(primary, secondary)

    # Google GenAI API enforces a minimum RPC deadline of 10.0s.
    attempt_timeout = max(10.0, config.cascade_timeout)
    return llm.FallbackAdapter(
        llm=[primary, secondary],
        attempt_timeout=attempt_timeout,
        max_retry_per_llm=config.max_retries,
    )

