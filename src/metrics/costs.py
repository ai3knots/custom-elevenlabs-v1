"""Provider pricing constants, cost calculations, and per-call cost accumulation."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("posivex.metrics.costs")

# Standard production pricing table (USD)
# Audio duration rates ($ / second)
RATE_DEEPGRAM_NOVA3_PER_SEC = 0.0043 / 60.0  # $0.0043 / min
RATE_LIVEKIT_TRANSPORT_PER_SEC = 0.0040 / 60.0  # $0.0040 / min

# ElevenLabs rates ($ / character)
RATE_ELEVENLABS_V3_PER_CHAR = 0.15 / 1000.0  # $0.15 per 1k characters

# Gemini rates ($ / token)
# gemini-3.5-flash-lite: $0.075 / 1M input, $0.30 / 1M output
RATE_GEMINI_FLASH_LITE_INPUT_PER_TOKEN = 0.075 / 1_000_000.0
RATE_GEMINI_FLASH_LITE_OUTPUT_PER_TOKEN = 0.30 / 1_000_000.0

# gemini-3.5-flash and gemini-2.5-flash: $0.15 / 1M input, $0.60 / 1M output
RATE_GEMINI_FLASH_INPUT_PER_TOKEN = 0.15 / 1_000_000.0
RATE_GEMINI_FLASH_OUTPUT_PER_TOKEN = 0.60 / 1_000_000.0


def calculate_cost(provider: str, model: str, metric_type: str, metric_value: float) -> float:
    """Calculate estimated cost in USD for a given provider, model, and metric."""
    prov = provider.lower()
    mod = model.lower()
    met = metric_type.lower()

    if prov == "deepgram":
        if met in ("audio_seconds", "duration"):
            return round(metric_value * RATE_DEEPGRAM_NOVA3_PER_SEC, 6)
        return 0.0

    if prov == "elevenlabs":
        if met in ("characters", "characters_count"):
            return round(metric_value * RATE_ELEVENLABS_V3_PER_CHAR, 6)
        return 0.0

    if prov in ("google", "gemini"):
        is_lite = "lite" in mod
        input_rate = RATE_GEMINI_FLASH_LITE_INPUT_PER_TOKEN if is_lite else RATE_GEMINI_FLASH_INPUT_PER_TOKEN
        output_rate = RATE_GEMINI_FLASH_LITE_OUTPUT_PER_TOKEN if is_lite else RATE_GEMINI_FLASH_OUTPUT_PER_TOKEN

        if met in ("tokens_input", "input_tokens", "prompt_tokens"):
            return round(metric_value * input_rate, 6)
        if met in ("tokens_output", "output_tokens", "completion_tokens"):
            return round(metric_value * output_rate, 6)
        return 0.0

    if prov in ("livekit", "telephony", "sip"):
        if met in ("audio_seconds", "duration"):
            return round(metric_value * RATE_LIVEKIT_TRANSPORT_PER_SEC, 6)
        return 0.0

    logger.warning("Unrecognized provider for cost calculation: provider=%s, model=%s", provider, model)
    return 0.0


@dataclass
class CostAccumulator:
    """Accumulates token, character, and duration metrics across a call session."""

    llm_cost: float = 0.0
    tts_cost: float = 0.0
    stt_cost: float = 0.0
    telephony_cost: float = 0.0
    records: list[dict[str, Any]] = field(default_factory=list)

    def add_llm_usage(self, model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
        """Record input and output tokens for an LLM generation.

        `cached_tokens` is the part of `input_tokens` the provider served from its prompt cache. It is stored as
        its own informational row (no cost) so the cache hit rate can be read back per call.
        """
        cost_in = calculate_cost("google", model, "tokens_input", float(input_tokens))
        cost_out = calculate_cost("google", model, "tokens_output", float(output_tokens))
        cost = round(cost_in + cost_out, 6)
        self.llm_cost = round(self.llm_cost + cost, 6)

        self.records.append(
            {
                "provider": "google",
                "model": model,
                "metric_type": "tokens_input",
                "metric_value": float(input_tokens),
                "estimated_cost_usd": cost_in,
            }
        )
        self.records.append(
            {
                "provider": "google",
                "model": model,
                "metric_type": "tokens_output",
                "metric_value": float(output_tokens),
                "estimated_cost_usd": cost_out,
            }
        )
        if cached_tokens > 0:
            self.records.append(
                {
                    "provider": "google",
                    "model": model,
                    "metric_type": "tokens_input_cached",
                    "metric_value": float(cached_tokens),
                    "estimated_cost_usd": 0.0,
                }
            )
        return cost

    def add_tts_usage(self, model: str, characters_count: int) -> float:
        """Record character count for TTS speech synthesis."""
        cost = calculate_cost("elevenlabs", model, "characters", float(characters_count))
        self.tts_cost = round(self.tts_cost + cost, 6)
        self.records.append(
            {
                "provider": "elevenlabs",
                "model": model,
                "metric_type": "characters",
                "metric_value": float(characters_count),
                "estimated_cost_usd": cost,
            }
        )
        return cost

    def add_stt_usage(self, model: str, audio_seconds: float) -> float:
        """Record transcribed audio duration in seconds."""
        cost = calculate_cost("deepgram", model, "audio_seconds", audio_seconds)
        self.stt_cost = round(self.stt_cost + cost, 6)
        self.records.append(
            {
                "provider": "deepgram",
                "model": model,
                "metric_type": "audio_seconds",
                "metric_value": round(audio_seconds, 2),
                "estimated_cost_usd": cost,
            }
        )
        return cost

    def add_telephony_usage(self, duration_seconds: float) -> float:
        """Record carrier / WebRTC transport duration in seconds."""
        cost = calculate_cost("livekit", "sip_webrtc", "audio_seconds", duration_seconds)
        self.telephony_cost = round(self.telephony_cost + cost, 6)
        self.records.append(
            {
                "provider": "livekit",
                "model": "sip_webrtc",
                "metric_type": "audio_seconds",
                "metric_value": round(duration_seconds, 2),
                "estimated_cost_usd": cost,
            }
        )
        return cost

    def get_charging_breakdown(self) -> dict[str, float]:
        """Format charging breakdown matching ElevenLabs emulation payload contract."""
        return {
            "call_charge": round(self.telephony_cost + self.tts_cost + self.stt_cost, 4),
            "llm_charge": round(self.llm_cost, 4),
            "tts_charge": round(self.tts_cost, 4),
            "stt_charge": round(self.stt_cost, 4),
            "transport_charge": round(self.telephony_cost, 4),
        }

    @property
    def total_cost(self) -> float:
        """Total accumulated cost in USD."""
        return round(self.llm_cost + self.tts_cost + self.stt_cost + self.telephony_cost, 4)
