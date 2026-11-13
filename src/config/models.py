"""Configuration schemas: the settings of every part of the system, and the `Settings` that holds them."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class STTConfig(BaseModel):
    """Speech-to-Text configuration settings."""

    provider: Literal["deepgram"] = "deepgram"
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

    provider: Literal["google"] = "google"
    default_model: str = "gemini-3.5-flash"
    # Must differ from the primary models, otherwise the fallback adds retries without resilience.
    fallback_model: str = "gemini-3.5-flash-lite"
    temperature: float = 0.0
    thinking_level: Literal["minimal", "low", "medium", "high"] = "minimal"
    cascade_timeout: float = 10.0
    max_retries: int = 0


class TTSConfig(BaseModel):
    """Text-to-Speech configuration settings."""

    provider: Literal["elevenlabs"] = "elevenlabs"
    model: str = "eleven_v3_conversational"
    voice_id: str = "eXpIbVcVbLo8ZJQDlDnl"  # Default Amelia voice
    stability: float = 0.5
    similarity_boost: float = 0.75


class VADConfig(BaseModel):
    """Voice Activity Detection configuration settings."""

    provider: Literal["silero"] = "silero"
    min_speech_duration: float = 0.05
    min_silence_duration: float = 0.55
    prefix_padding_duration: float = 0.5


class TurnDetectorConfig(BaseModel):
    """Turn detection mode configuration."""

    # "v1" / "v1-mini" use LiveKit's audio turn detector (v1 via LiveKit Inference,
    # falling back to the local v1-mini on errors or when the plan allowance runs out).
    mode: Literal["vad", "stt-only", "v1", "v1-mini"] = "vad"


class ConversationConfig(BaseModel):
    """Conversation guardrails, timeouts, and interruption ignore terms."""

    max_duration_seconds: int = 900
    # The call ends after this many seconds of caller silence (like the reference ElevenLabs agent).
    silence_end_call_seconds: float = 15.0
    # skip_turn ("hold on", "let me get the owner") suspends the silence timer for this long.
    hold_duration_seconds: float = 30.0
    # Ends the call when nothing has been heard from the callee (no transcribed words) for this long, even
    # if the line is not silent (hold music, a carrier recording looping). 0 disables it.
    no_speech_watchdog_seconds: float = 40.0
    # Turn-taking is the same for the whole call. The maximum is how long the agent waits for a reply it is
    # unsure has ended (short answers like "Yes." and "Hello." often are). It was 4 s, which left 4 s of dead
    # air on 4 of 11 turns in a real call. Someone dictating an email with long pauses can now be cut off.
    min_endpointing_delay: float = 0.5
    max_endpointing_delay: float = 1.5
    ring_timeout_seconds: float = 75.0
    interruption_min_words: int = 1
    # The scripted opening line starts this long after pickup. If the callee is mid-sentence at that moment
    # it waits up to `opener_quiet_wait_seconds` more for them to finish, then speaks anyway.
    opener_delay_seconds: float = 1.0
    opener_quiet_wait_seconds: float = 1.0
    # Voicemail / phone-menu detection runs beside the opener: a small, fast Gemini model reads what the
    # callee has said so far (streaming transcript, no wait for the end of their turn) and hangs up on a
    # machine as soon as it is confident. It replaces the 3 s LiveKit AMD gate, which never classified.
    machine_detection_enabled: bool = True
    machine_detection_model: str = "gemini-3.5-flash-lite"
    machine_detection_window_seconds: float = 25.0
    machine_detection_confidence: float = 0.8
    max_user_speech_seconds: float = 8.0
    # Cleans background noise out of the callee's audio before speech-to-text and turn detection. "krisp_nc" is the
    # background-noise model LiveKit Cloud includes at no charge; "none" turns it off (a call can then be compared).
    noise_cancellation: Literal["krisp_nc", "none"] = "krisp_nc"
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
    retry_backoff_base: float = 2.0


class TelephonyConfig(BaseModel):
    """Telephony provider configuration."""

    provider: Literal["livekit_sip", "mock", "console"] = "livekit_sip"
    sip_trunk_id: str | None = None
    max_concurrent_calls: int = 20


class AgentEntry(BaseModel):
    """An agent id that lead-gen can dial with, and the persona (prompt) that serves it."""

    agent_id: str
    name: str
    persona: Literal["amelia", "michael"] = "amelia"


class PhoneNumberEntry(BaseModel):
    """A calling number lead-gen can pick. Every entry dials through the one configured SIP trunk."""

    phone_number_id: str
    phone_number: str = ""
    label: str = "Posivex LiveKit number"


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

    log_level: str = "INFO"
    persona: Literal["amelia", "michael"] = "amelia"

    # Core Infrastructure Secrets
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    gemini_api_key: str = ""
    deepgram_api_key: str = ""
    elevenlabs_api_key: str = ""
    database_url: str = "postgresql+asyncpg://posivex:posivex@localhost:5432/posivex_calls"

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
            AgentEntry(agent_id="agent_posivex_amelia", name="Posivex LiveKit - Amelia", persona="amelia"),
            AgentEntry(agent_id="agent_posivex_michael", name="Posivex LiveKit - Michael", persona="michael"),
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
