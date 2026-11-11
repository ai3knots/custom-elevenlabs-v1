"""Configuration: settings schemas, layered loading, and call request validation.

Import from here (`from src.config import Settings, load_settings, CallRequest`); the modules are an implementation detail.
"""

from src.config.call_request import ALLOWED_OVERRIDE_SECTIONS, CallRequest, DynamicVariables
from src.config.loader import ENV_RULES, EnvRule, deep_merge, load_settings
from src.config.models import (
    AgentEntry,
    ConversationConfig,
    KBConfig,
    LLMConfig,
    PhoneNumberEntry,
    RecordingConfig,
    Settings,
    STTConfig,
    TelephonyConfig,
    TTSConfig,
    TurnDetectorConfig,
    VADConfig,
    WebhookConfig,
)

__all__ = [
    "ALLOWED_OVERRIDE_SECTIONS",
    "ENV_RULES",
    "AgentEntry",
    "CallRequest",
    "ConversationConfig",
    "DynamicVariables",
    "EnvRule",
    "KBConfig",
    "LLMConfig",
    "PhoneNumberEntry",
    "RecordingConfig",
    "STTConfig",
    "Settings",
    "TTSConfig",
    "TelephonyConfig",
    "TurnDetectorConfig",
    "VADConfig",
    "WebhookConfig",
    "deep_merge",
    "load_settings",
]
