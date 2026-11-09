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
    default_model: str = "gemini-3.5-flash"
    # Must differ from the primary models, otherwise the fallback adds retries without resilience.
    fallback_model: str = "gemini-3.5-flash-lite"
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
    similarity_boost: float = 0.75
    expressive: bool = True


class VADConfig(BaseModel):
    """Voice Activity Detection configuration settings."""

    provider: Literal["silero", "mock"] = "silero"
    min_speech_duration: float = 0.05
    min_silence_duration: float = 0.55
    prefix_padding_duration: float = 0.5


class TurnDetectorConfig(BaseModel):
    """Turn detection mode configuration."""

    # "v1" / "v1-mini" use LiveKit's audio turn detector (v1 via LiveKit Inference,
    # falling back to the local v1-mini on errors or when the plan allowance runs out).
    mode: Literal["vad", "stt-only", "v1", "v1-mini", "multimodal", "eou"] = "vad"


class ConversationConfig(BaseModel):
    """Conversation guardrails, timeouts, and interruption ignore terms."""

    max_duration_seconds: int = 900
    # The call ends after this many seconds of caller silence (like the reference ElevenLabs agent).
    silence_end_call_seconds: float = 15.0
    # skip_turn ("hold on", "let me get the owner") suspends the silence timer for this long.
    hold_duration_seconds: float = 12.0
    # Turn-taking is the same for the whole call; 4 s lets people dictate emails and numbers.
    min_endpointing_delay: float = 0.5
    max_endpointing_delay: float = 4.0
    amd_enabled: bool = True
    ring_timeout_seconds: float = 75.0
    interruption_min_words: int = 1
    amd_no_speech_seconds: float = 6.0
    # Hard cap on answering-machine detection, measured from pickup. When it expires without a
    # verdict the callee is treated as a person and greeted, so a recording notice followed by
    # "Hello?" no longer leaves 10+ seconds of silence.
    amd_timeout_seconds: float = 3.0
    max_user_speech_seconds: float = 8.0
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
    file_path: str = "prompts/knowledge/Knowledge-base.md"
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
    sip_phone_number_id: str | None = None
    max_concurrent_calls: int = 50


class AgentEntry(BaseModel):
    """An agent id that lead-gen can dial with, and the persona (prompt) that serves it."""

    agent_id: str
    name: str
    persona: Literal["amelia", "michael"] = "amelia"


class PhoneNumberEntry(BaseModel):
    """A calling number lead-gen can pick. Every entry dials through the one configured SIP trunk."""

    phone_number_id: str
    phone_number: str = ""
    label: str = "Posivex SIP trunk"


class RecordingConfig(BaseModel):
    """Call recording: LiveKit egress writes audio to an S3-compatible bucket, the API streams it back."""

    enabled: bool = False
    bucket: str = ""
    region: str = "us-east-1"
    endpoint: str = ""  # blank for AWS S3; set for R2, MinIO, Wasabi and similar
    access_key: str = ""
    secret: str = ""
    force_path_style: bool = False
    prefix: str = "recordings"
    file_type: Literal["ogg", "mp3"] = "ogg"

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.bucket and self.access_key and self.secret)


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
    recording: RecordingConfig = Field(default_factory=RecordingConfig)

    # Lead-gen integration (the ElevenLabs-compatible API)
    agents: list[AgentEntry] = Field(
        default_factory=lambda: [
            AgentEntry(
                agent_id="agent_2701m6e2fw2eehd97r93d4v8x9yc",
                name="Posivex - 6 - Amelia - Single Master Prompt",
                persona="amelia",
            ),
            AgentEntry(agent_id="agent_posivex_michael", name="Posivex - Michael", persona="michael"),
        ]
    )
    phone_numbers: list[PhoneNumberEntry] = Field(
        default_factory=lambda: [PhoneNumberEntry(phone_number_id="phnum_posivex_trunk")]
    )
    # xi-api-key lead-gen must send. Empty: fall back to elevenlabs_api_key, then accept any non-empty key.
    voice_api_key: str = ""

    # Where a LiveKit Cloud-hosted agent reports the finished call (the API on your server). Empty: the
    # agent writes to the database directly, as when it runs on the same host as Postgres.
    results_callback_url: str = ""
    results_callback_secret: str = ""

    def webhook_destination(self) -> str:
        """Full URL of lead-gen's post-call webhook (a bare base URL gets the standard path appended)."""
        url = self.webhook.lead_gen_base_url
        if url and "/api/webhooks" not in url:
            url = f"{url.rstrip('/')}/api/webhooks/elevenlabs"
        return url

    def agent_for_id(self, agent_id: str | None) -> AgentEntry | None:
        return next((a for a in self.agents if a.agent_id == agent_id), None) if agent_id else None

    def persona_for_agent(self, agent_id: str | None, default: str = "amelia") -> str:
        """Persona that serves `agent_id`; unknown ids (for example an old ElevenLabs agent) use `default`."""
        entry = self.agent_for_id(agent_id)
        return entry.persona if entry else default

    def agent_for_persona(self, persona: str) -> AgentEntry:
        """The agent entry advertised for a persona (the first match, else the first entry)."""
        return next((a for a in self.agents if a.persona == persona), self.agents[0])

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

        # ElevenLabs names the calling number agent_phone_number_id
        if not values.get("phone_number_id") and values.get("agent_phone_number_id"):
            values["phone_number_id"] = values["agent_phone_number_id"]

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

    # Lead-gen integration and recording
    for env_var, field_name in (
        ("POSIVEX_API_KEY", "voice_api_key"),
        ("RESULTS_CALLBACK_URL", "results_callback_url"),
        ("RESULTS_CALLBACK_SECRET", "results_callback_secret"),
    ):
        env_val = os.getenv(env_var)
        if env_val:
            merged[field_name] = env_val
    for env_var, field_name in (
        ("RECORDING_BUCKET", "bucket"),
        ("RECORDING_REGION", "region"),
        ("RECORDING_ENDPOINT", "endpoint"),
        ("RECORDING_ACCESS_KEY", "access_key"),
        ("RECORDING_SECRET", "secret"),
        ("RECORDING_PREFIX", "prefix"),
    ):
        env_val = os.getenv(env_var)
        if env_val:
            merged.setdefault("recording", {})[field_name] = env_val
    if os.getenv("RECORDING_ENABLED"):
        merged.setdefault("recording", {})["enabled"] = os.getenv("RECORDING_ENABLED", "").lower() == "true"
    if os.getenv("RECORDING_FORCE_PATH_STYLE"):
        merged.setdefault("recording", {})["force_path_style"] = (
            os.getenv("RECORDING_FORCE_PATH_STYLE", "").lower() == "true"
        )
    phone_id, phone_number = os.getenv("VOICE_PHONE_NUMBER_ID"), os.getenv("VOICE_PHONE_NUMBER")
    if phone_id or phone_number:
        merged["phone_numbers"] = [
            {"phone_number_id": phone_id or "phnum_posivex_trunk", "phone_number": phone_number or ""}
        ]

    # LLM model overrides from env, so latency experiments need no rebuild
    for env_var, field_name in (
        ("LLM_DEFAULT_MODEL", "default_model"),
        ("LLM_FALLBACK_MODEL", "fallback_model"),
    ):
        model_override = os.getenv(env_var)
        if model_override:
            merged.setdefault("llm", {})[field_name] = model_override

    # Turn detection mode from env (vad | stt-only | v1 | v1-mini)
    turn_mode = os.getenv("TURN_DETECTOR_MODE")
    if turn_mode:
        merged.setdefault("turn_detector", {})["mode"] = turn_mode

    # Telephony configuration from env
    telephony_provider = os.getenv("TELEPHONY_PROVIDER")
    if telephony_provider:
        merged.setdefault("telephony", {})["provider"] = telephony_provider
    sip_trunk_id = os.getenv("SIP_OUTBOUND_TRUNK_ID")
    if sip_trunk_id:
        merged.setdefault("telephony", {})["sip_trunk_id"] = sip_trunk_id
    sip_phone_id = os.getenv("SIP_PHONE_NUMBER_ID")
    if sip_phone_id:
        merged.setdefault("telephony", {})["sip_phone_number_id"] = sip_phone_id

    # 4. Dispatch Overrides
    if overrides:
        merged = deep_merge(merged, overrides)

    return Settings.model_validate(merged)

