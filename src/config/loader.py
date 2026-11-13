"""Layered settings: defaults.yaml, then the persona profile, then environment variables, then per-call overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.config.models import Settings

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override dictionary into base dictionary."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


@dataclass(frozen=True)
class EnvRule:
    """One environment variable and where its value lands in the settings."""

    env: str
    path: tuple[str, ...]  # e.g. ("recording", "bucket")
    accept_empty: bool = False  # True: an empty value still overrides; False: an empty value is ignored
    boolean: bool = False  # the value is "true" (any case) or anything else (false)


ENV_RULES: tuple[EnvRule, ...] = (
    # Infrastructure and provider keys. An empty value overrides too, so a blank key in .env blanks the default.
    EnvRule("LIVEKIT_URL", ("livekit_url",), accept_empty=True),
    EnvRule("LIVEKIT_API_KEY", ("livekit_api_key",), accept_empty=True),
    EnvRule("LIVEKIT_API_SECRET", ("livekit_api_secret",), accept_empty=True),
    EnvRule("GEMINI_API_KEY", ("gemini_api_key",), accept_empty=True),
    EnvRule("DEEPGRAM_API_KEY", ("deepgram_api_key",), accept_empty=True),
    EnvRule("ELEVENLABS_API_KEY", ("elevenlabs_api_key",), accept_empty=True),
    EnvRule("DATABASE_URL", ("database_url",), accept_empty=True),
    EnvRule("LOG_LEVEL", ("log_level",), accept_empty=True),
    # Lead-gen integration
    EnvRule("LEAD_GEN_WEBHOOK_URL", ("webhook", "lead_gen_base_url")),
    EnvRule("ELEVENLABS_WEBHOOK_SECRET", ("webhook", "webhook_secret")),
    EnvRule("POSIVEX_API_KEY", ("voice_api_key",)),
    EnvRule("RESULTS_CALLBACK_URL", ("results_callback_url",)),
    EnvRule("RESULTS_CALLBACK_SECRET", ("results_callback_secret",)),
    # Call recording
    EnvRule("RECORDING_ENABLED", ("recording", "enabled"), boolean=True),
    EnvRule("RECORDING_BUCKET", ("recording", "bucket")),
    EnvRule("RECORDING_REGION", ("recording", "region")),
    EnvRule("RECORDING_ENDPOINT", ("recording", "endpoint")),
    EnvRule("RECORDING_ACCESS_KEY", ("recording", "access_key")),
    EnvRule("RECORDING_SECRET", ("recording", "secret")),
    EnvRule("RECORDING_PREFIX", ("recording", "prefix")),
    EnvRule("RECORDING_FORCE_PATH_STYLE", ("recording", "force_path_style"), boolean=True),
    # Latency experiments need no rebuild
    EnvRule("LLM_DEFAULT_MODEL", ("llm", "default_model")),
    EnvRule("LLM_FALLBACK_MODEL", ("llm", "fallback_model")),
    EnvRule("TURN_DETECTOR_MODE", ("turn_detector", "mode")),  # vad | stt-only | v1 | v1-mini
    EnvRule("NOISE_CANCELLATION", ("conversation", "noise_cancellation")),  # krisp_nc | none
    # Telephony
    EnvRule("TELEPHONY_PROVIDER", ("telephony", "provider")),
    EnvRule("SIP_OUTBOUND_TRUNK_ID", ("telephony", "sip_trunk_id")),
)


def _apply_env(merged: dict[str, Any]) -> None:
    for rule in ENV_RULES:
        raw = os.getenv(rule.env)
        if raw is None or (not raw and not rule.accept_empty):
            continue
        target = merged
        for key in rule.path[:-1]:
            target = target.setdefault(key, {})
        target[rule.path[-1]] = (raw.lower() == "true") if rule.boolean else raw

    # The calling number lead-gen can pick: one entry from these two variables replaces the YAML list.
    phone_id, phone_number = os.getenv("VOICE_PHONE_NUMBER_ID"), os.getenv("VOICE_PHONE_NUMBER")
    if phone_id or phone_number:
        merged["phone_numbers"] = [
            {"phone_number_id": phone_id or "phnum_posivex_trunk", "phone_number": phone_number or ""}
        ]


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        content = yaml.safe_load(f)
    return content if isinstance(content, dict) else {}


def load_settings(
    persona: str = "amelia",
    overrides: dict[str, Any] | None = None,
    config_dir: str | Path | None = None,
) -> Settings:
    """Load settings by layering defaults.yaml, persona profile, env vars, and overrides."""
    base_dir = Path(config_dir) if config_dir else CONFIG_DIR
    merged = deep_merge({}, _read_yaml(base_dir / "defaults.yaml"))  # 1. defaults
    merged = deep_merge(merged, _read_yaml(base_dir / "profiles" / f"{persona.lower()}.yaml"))  # 2. persona profile
    _apply_env(merged)  # 3. environment variables
    if overrides:  # 4. dispatch overrides
        merged = deep_merge(merged, overrides)
    return Settings.model_validate(merged)
