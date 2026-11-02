"""Configuration schemas, layered configuration loading, and call request validation."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("posivex.config")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override dictionary into base dictionary."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class STTConfig(BaseModel):
    """Speech-to-Text configuration settings."""

    provider: Literal["deepgram", "mock", "assemblyai"] = "deepgram"
    model: str = "nova-3"
    language_mode: Literal["en", "es", "multi"] = "en"
    language: str = "en-US"
    language_hint: list[str] = Field(default_factory=lambda: ["en", "es"])
    endpointing_ms: int = 25
    interim_results: bool = True
    punctuate: bool = True
    smart_format: bool = False


class LLMConfig(BaseModel):
    """Large Language Model configuration settings."""

    provider: Literal["google", "mock"] = "google"
    default_model: str = "gemini-3.5-flash-lite"
    conversation_model: str = "gemini-3.5-flash"
    fallback_model: str = "gemini-3.5-flash"
    temperature: float = 0.0
    thinking_level: Literal["minimal", "low", "medium", "high"] = "minimal"
    cascade_timeout: float = 10.0
    max_retries: int = 0


class TTSConfig(BaseModel):
    """Text-to-Speech configuration settings."""

    provider: Literal["elevenlabs", "mock"] = "elevenlabs"
    model: str = "eleven_v3_conversational"
    voice_id: str = "eXpIbVcVbLo8ZJQDlDnl"  # Default Amelia voice
    encoding: Literal["pcm_24000", "pcm_16000", "ulaw_8000"] = "pcm_24000"
    stability: float = 0.5
    similarity_boost: float = 0.8
    expressive: bool = True


class VADConfig(BaseModel):
    """Voice Activity Detection configuration settings."""

    provider: Literal["silero", "mock"] = "silero"
    min_speech_duration: float = 0.05
    min_silence_duration: float = 0.55
    prefix_padding_duration: float = 0.5


class TurnDetectorConfig(BaseModel):
    """Turn detection mode configuration."""

    mode: Literal["stt-only", "multimodal", "eou"] = "stt-only"


class ConversationConfig(BaseModel):
    """Conversation guardrails, timeouts, and interruption ignore terms."""

    max_duration_seconds: int = 900
    silence_timeout_seconds: float = 15.0
    max_silence_count: int = 3
    hold_duration_seconds: float = 12.0
    interruption_ignore_terms: list[str] = Field(
        default_factory=lambda: [
            "mmhmm",
            "uhhuh",
            "uh huh",
            "uh",
            "mm",
            "hmm",
            "okay",
            "ok",
            "okey",
            "i see",
            "go on",
            "go ahead",
            "right",
            "gotcha",
            "mmm",
            "mhm",
            "ajá",
            "vale",
            "ya",
            "sí",
            "claro",
            "entiendo",
            "dígame",
        ]
    )


class KBConfig(BaseModel):
    """Knowledge base configuration settings."""

    mode: Literal["inline", "rag", "off"] = "inline"
    file_path: str = "reference/elevenlabs-agent-config/agent-v5-Amelia/Knowledge-base.md"
    max_token_budget: int = 4000


class WebhookConfig(BaseModel):
    """Outbound webhook dispatcher settings."""

    lead_gen_base_url: str = "http://localhost:3000"
    webhook_secret: str = "dev_webhook_secret"
    max_retries: int = 10
    retry_backoff_base: float = 2.0


class TelephonyConfig(BaseModel):
    """Telephony provider configuration."""

    provider: Literal["livekit_sip", "mock", "console"] = "mock"
    sip_trunk_id: str | None = None
    max_concurrent_calls: int = 50


class Settings(BaseSettings):
    """Global application settings with layered resolution."""

    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    persona: Literal["amelia", "michael"] = "amelia"
    display_name: str = "Amelia"
    gender: str = "female"
    language: Literal["en", "es"] = "en"

    # Core Infrastructure Secrets
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    gemini_api_key: str = ""
    deepgram_api_key: str = ""
    elevenlabs_api_key: str = ""
    database_url: str = "postgresql+asyncpg://posivex:posivex@localhost:5432/posivex_calls"
    redis_url: str = "redis://localhost:6379/0"

    # Sub-component Configurations
    stt: STTConfig = Field(default_factory=STTConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    vad: VADConfig = Field(default_factory=VADConfig)
    turn_detector: TurnDetectorConfig = Field(default_factory=TurnDetectorConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    kb: KBConfig = Field(default_factory=KBConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    telephony: TelephonyConfig = Field(default_factory=TelephonyConfig)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )


class DynamicVariables(BaseModel):
    """Dynamic variables injected into prompt templates and FSM logic."""

    model_config = ConfigDict(extra="allow")

    business_name: str = ""
    business_context: str = ""
    current_datetime: str = ""
    is_followup: str = "false"
    followup_notes: str = ""
    contact_name: str = ""


class CallRequest(BaseModel):
    """Validated call request schema for both native and emulated dial endpoints."""

    model_config = ConfigDict(extra="allow")

    call_id: str | None = None
    lead_id: str | None = None
    campaign_id: str | None = None
    phone: str = ""
    to_number: str | None = None
    agent_id: str | None = None
    persona: Literal["amelia", "michael"] = "amelia"
    language: Literal["en", "es"] = "en"
    phone_number_id: str | None = None
    dynamic_variables: DynamicVariables = Field(default_factory=DynamicVariables)
    conversation_initiation_client_data: dict[str, Any] | None = None
    config_overrides: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def populate_and_normalize_fields(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values

        # Handle ElevenLabs to_number alias
        if not values.get("phone") and values.get("to_number"):
            values["phone"] = values["to_number"]

        # Handle ElevenLabs nested conversation_initiation_client_data.dynamic_variables
        if values.get("conversation_initiation_client_data") and not values.get("dynamic_variables"):
            client_data = values["conversation_initiation_client_data"]
            if isinstance(client_data, dict) and "dynamic_variables" in client_data:
                values["dynamic_variables"] = client_data["dynamic_variables"]

        return values

    @field_validator("phone")
    @classmethod
    def validate_and_normalize_phone(cls, v: str) -> str:
        if not v:
            raise ValueError("Phone number must not be empty.")
        # Strip spaces, dashes, parentheses
        digits_and_plus = re.sub(r"[^\d+]", "", v)
        if not digits_and_plus:
            raise ValueError(f"Invalid phone number format: {v}")

        # Check standard 10-digit US number without prefix
        if re.fullmatch(r"\d{10}", digits_and_plus):
            return f"+1{digits_and_plus}"
        # 11-digit US number starting with 1
        if re.fullmatch(r"1\d{10}", digits_and_plus):
            return f"+{digits_and_plus}"
        # Standard E.164
        if re.fullmatch(r"\+[1-9]\d{7,14}", digits_and_plus):
            return digits_and_plus

        raise ValueError(f"Invalid E.164 phone number: {v}")


def load_settings(
    persona: str = "amelia",
    overrides: dict[str, Any] | None = None,
    config_dir: str | Path | None = None,
) -> Settings:
    """Load settings by layering defaults.yaml, persona profile, env vars, and overrides."""
    base_dir = Path(config_dir) if config_dir else Path(__file__).resolve().parent.parent / "config"
    merged: dict[str, Any] = {}

    # 1. Defaults YAML
    defaults_file = base_dir / "defaults.yaml"
    if defaults_file.exists():
        with open(defaults_file, encoding="utf-8") as f:
            content = yaml.safe_load(f)
            if isinstance(content, dict):
                merged = deep_merge(merged, content)

    # 2. Persona Profile YAML
    persona_file = base_dir / "profiles" / f"{persona.lower()}.yaml"
    if persona_file.exists():
        with open(persona_file, encoding="utf-8") as f:
            content = yaml.safe_load(f)
            if isinstance(content, dict):
                merged = deep_merge(merged, content)

    # 3. Environment Variables (.env via Pydantic BaseSettings default behavior)
    env_keys = {
        "LIVEKIT_URL": "livekit_url",
        "LIVEKIT_API_KEY": "livekit_api_key",
        "LIVEKIT_API_SECRET": "livekit_api_secret",
        "GEMINI_API_KEY": "gemini_api_key",
        "DEEPGRAM_API_KEY": "deepgram_api_key",
        "ELEVENLABS_API_KEY": "elevenlabs_api_key",
        "DATABASE_URL": "database_url",
        "REDIS_URL": "redis_url",
        "ENVIRONMENT": "environment",
        "LOG_LEVEL": "log_level",
    }
    for env_var, field_name in env_keys.items():
        val = os.getenv(env_var)
        if val is not None:
            merged[field_name] = val

    # Webhook integrations from env
    lead_gen_url = os.getenv("LEAD_GEN_WEBHOOK_URL")
    if lead_gen_url:
        merged.setdefault("webhook", {})["lead_gen_base_url"] = lead_gen_url
    webhook_secret = os.getenv("ELEVENLABS_WEBHOOK_SECRET")
    if webhook_secret:
        merged.setdefault("webhook", {})["webhook_secret"] = webhook_secret

    # 4. Dispatch Overrides
    if overrides:
        merged = deep_merge(merged, overrides)

    return Settings.model_validate(merged)

