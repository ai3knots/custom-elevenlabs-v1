"""Builds ElevenLabs-shaped conversation payloads for Posivex-lead-gen.

Lead-gen parses the post-call webhook and the conversation-details API with one parser
(`src/lib/elevenlabs-payload.ts`): `transcript[]` turns (with tool calls), `metadata.phone_call`,
`metadata.termination_reason`, `analysis.data_collection_results.call_outcome` and so on. The same
`data` object is used for both, so what the webhook delivers and what `GET /v1/convai/conversations/{id}`
returns never differ.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from src.db.models import Call, CallEvent, CallStatus, Transcript, as_utc

# Agent events that lead-gen sees as tool calls (name as the ElevenLabs agent would call it).
TOOL_EVENTS = {
    "end_call": "end_call",
    "voicemail_detected": "voicemail_detection",
    "hold_requested": "skip_turn",
}

SUCCESS_OUTCOMES = frozenset(
    {"consultant_call_scheduled", "email_comparison", "info_email", "owner_details_captured", "callback_set"}
)
FAILURE_OUTCOMES = frozenset({"declined", "do_not_call"})


def call_successful_for(outcome: str | None) -> str:
    if outcome in SUCCESS_OUTCOMES:
        return "success"
    if outcome in FAILURE_OUTCOMES:
        return "failure"
    return "unknown"


def conversation_status(call_status: str) -> str:
    """ElevenLabs conversation status for one of our call statuses."""
    if call_status == CallStatus.COMPLETED.value:
        return "done"
    if call_status == CallStatus.FAILED.value:
        return "failed"
    return "in-progress"


def _secs_since(start: datetime | None, moment: datetime | None) -> float:
    if start is None or moment is None:
        return 0.0
    return max(0.0, round((as_utc(moment) - as_utc(start)).total_seconds(), 2))


def _tool_turns(call: Call, events: Sequence[CallEvent]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for ev in events:
        tool = TOOL_EVENTS.get(ev.event_type)
        if tool is None:
            continue
        at = _secs_since(call.started_at, ev.timestamp)
        request_id = f"tool_{ev.id}" if ev.id is not None else f"tool_{len(turns)}"
        turns.append(
            {
                "role": "agent",
                "message": "",
                "time_in_call_secs": at,
                "tool_calls": [
                    {
                        "request_id": request_id,
                        "tool_name": tool,
                        "params_as_json": "{}",
                        "tool_has_been_called": True,
                    }
                ],
                "tool_results": [
                    {"request_id": request_id, "tool_name": tool, "result_value": "", "is_error": False}
                ],
            }
        )
    return turns


def _sip_call_id(events: Sequence[CallEvent]) -> str | None:
    for ev in events:
        if ev.event_type == "call_dispatched":
            return (ev.payload or {}).get("sip_call_id")
    return None


def build_conversation_data(
    call: Call,
    transcripts: Sequence[Transcript],
    events: Sequence[CallEvent],
    *,
    agent_id: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """The `data` object of a conversation (webhook `data`, and the conversation-details response)."""
    meta = call.metadata_json or {}
    outcome = call.call_outcome
    turns: list[dict[str, Any]] = [
        {
            "role": "agent" if t.role == "agent" else "user",
            "message": t.message,
            "time_in_call_secs": t.time_in_call_secs,
            "interrupted": bool(t.interrupted),
            "tool_calls": [],
            "tool_results": [],
        }
        for t in transcripts
        if t.role in ("agent", "user")
    ]
    turns.extend(_tool_turns(call, events))
    turns.sort(key=lambda t: t["time_in_call_secs"])

    finished = call.status in (CallStatus.COMPLETED.value, CallStatus.FAILED.value)
    metadata: dict[str, Any] = {
        "start_time_unix_secs": int(as_utc(call.started_at).timestamp()) if call.started_at else 0,
        "call_duration_secs": int(call.duration_secs or 0),
        "cost": round(call.total_cost or 0.0, 4),
        "termination_reason": call.termination_reason or "",
        "charging": call.charging_details or {},
        "phone_call": {
            "direction": meta.get("direction", "outbound"),
            "phone_number_id": meta.get("phone_number_id"),
            "type": "sip_trunking",
            "external_number": call.phone,
            "call_sid": _sip_call_id(events),
        },
    }
    if call.status == CallStatus.FAILED.value:
        metadata["error"] = {"code": call.termination_reason or "failed", "reason": call.termination_reason or "failed"}

    analysis: dict[str, Any] = {}
    if finished and outcome:
        analysis = {
            "call_successful": call_successful_for(outcome),
            "transcript_summary": call.transcript_summary or "",
            "data_collection_results": {
                "call_outcome": {
                    "data_collection_id": "call_outcome",
                    "value": outcome,
                    "rationale": call.outcome_rationale or "",
                }
            },
            "evaluation_criteria_results": {},
        }

    recording = meta.get("recording") or {}
    return {
        "agent_id": agent_id or meta.get("agent_id") or "",
        "agent_name": agent_name or "",
        "conversation_id": call.id,
        "status": conversation_status(call.status),
        "transcript": turns,
        "metadata": metadata,
        "analysis": analysis,
        "conversation_initiation_client_data": {"dynamic_variables": meta.get("dynamic_variables") or {}},
        "has_audio": bool(recording.get("key")),
        "has_user_audio": bool(recording.get("key")),
        "has_response_audio": bool(recording.get("key")),
    }


def build_post_call_payload(
    call: Call,
    transcripts: Sequence[Transcript],
    events: Sequence[CallEvent],
    *,
    event_timestamp: int,
    agent_id: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """The `post_call_transcription` webhook body."""
    return {
        "type": "post_call_transcription",
        "event_timestamp": event_timestamp,
        "data": build_conversation_data(call, transcripts, events, agent_id=agent_id, agent_name=agent_name),
    }
