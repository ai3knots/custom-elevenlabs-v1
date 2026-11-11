"""Response bodies of the routes whose shape is fixed by the ElevenLabs API that lead-gen speaks.

The names (`callSid`, `sip_call_id`, `has_more`) are ElevenLabs', not ours. `tests/test_route_contract.py` pins every body.
"""

from __future__ import annotations

from pydantic import BaseModel


class DialResponse(BaseModel):
    success: bool
    conversation_id: str
    callSid: str  # noqa: N815 - ElevenLabs' name
    sip_call_id: str


class AgentOut(BaseModel):
    agent_id: str
    name: str


class AgentList(BaseModel):
    agents: list[AgentOut]
    has_more: bool
    next_cursor: str | None


class PhoneNumberOut(BaseModel):
    phone_number_id: str
    phone_number: str
    label: str
    provider: str
    supports_outbound: bool
    supports_inbound: bool


class ConversationSummary(BaseModel):
    conversation_id: str
    agent_id: str
    agent_name: str
    start_time_unix_secs: int
    call_duration_secs: int
    status: str
    direction: str


class ConversationList(BaseModel):
    conversations: list[ConversationSummary]
    has_more: bool
    next_cursor: str | None


class ResultAck(BaseModel):
    ok: bool
    duplicate: bool
