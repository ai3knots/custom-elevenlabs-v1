"""The request to place a call, as the API receives it and as the dispatch metadata carries it to the agent."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DynamicVariables(BaseModel):
    """Dynamic variables injected into prompt templates and FSM logic."""

    model_config = ConfigDict(extra="allow")

    business_name: str = ""
    business_context: str = ""
    current_datetime: str = ""
    is_followup: str = "false"
    followup_notes: str = ""
    contact_name: str = ""
    # A callback somebody agreed to. Lead-gen writes the opening question and the confirmation line (see its lib/callback-context).
    is_callback: str = "false"
    opening_question: str = ""
    callback_confirm_line: str = ""
    callback_summary: str = ""


# `config_overrides` on a call request may only tune the conversation. Anything else (recording credentials, the
# webhook target, telephony, a knowledge-base file path) would let whoever can dial change infrastructure settings.
ALLOWED_OVERRIDE_SECTIONS = frozenset({"stt", "llm", "tts", "vad", "turn_detector", "conversation"})


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

    @field_validator("config_overrides")
    @classmethod
    def only_conversation_overrides(cls, v: dict[str, Any]) -> dict[str, Any]:
        disallowed = sorted(set(v) - ALLOWED_OVERRIDE_SECTIONS)
        if disallowed:
            raise ValueError(
                f"config_overrides may only set {sorted(ALLOWED_OVERRIDE_SECTIONS)}, not {disallowed}"
            )
        return v

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
