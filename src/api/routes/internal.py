"""Internal endpoints used by the voice agent worker, not by lead-gen.

A LiveKit Cloud-hosted agent cannot reach the Postgres on your server, so it reports the finished call
here over HTTPS (`X-Posivex-Callback-Secret`). The API stores everything and stages the signed post-call
webhook for lead-gen, exactly as when the agent writes to the database itself.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from src.api.deps import get_call_recorder, get_settings, verify_callback_secret
from src.api.schemas import ResultAck
from src.config import Settings
from src.db.models import CallStatus
from src.persistence.recorder import CallRecorder

logger = logging.getLogger("posivex.api.internal")
router = APIRouter(prefix="/internal", tags=["Internal"], dependencies=[Depends(verify_callback_secret)])


class CallInfo(BaseModel):
    """What is needed to create the call row when the API did not (browser and console test calls)."""

    phone: str = ""
    persona: str = "amelia"
    language: str = "en"
    lead_id: str | None = None
    campaign_id: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class FinalizeInfo(BaseModel):
    duration_secs: float = 0.0
    termination_reason: str = "normal_completion"
    call_outcome: str = "other"
    outcome_rationale: str = ""
    transcript_summary: str | None = ""
    total_cost: float = 0.0
    charging_details: dict[str, Any] | None = None
    agent_id: str | None = None
    agent_name: str | None = None


class CallResult(BaseModel):
    call: CallInfo = Field(default_factory=CallInfo)
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    usage: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    recording: dict[str, Any] | None = None
    finalize: FinalizeInfo = Field(default_factory=FinalizeInfo)


@router.post("/calls/{call_id}/result", status_code=status.HTTP_200_OK, response_model=ResultAck)
async def report_call_result(
    call_id: str,
    body: CallResult,
    settings: Settings = Depends(get_settings),
    recorder: CallRecorder = Depends(get_call_recorder),
) -> ResultAck:
    """Store a finished call reported by the agent. Safe to retry: a finalized call is not stored twice."""
    existing = await recorder.get_call(call_id)
    if existing is not None and existing.status in (CallStatus.COMPLETED.value, CallStatus.FAILED.value):
        logger.info("Result for %s already stored; ignoring the repeat.", call_id)
        return ResultAck(ok=True, duplicate=True)

    info = body.call
    await recorder.ensure_call(
        call_id=call_id,
        phone=info.phone,
        persona=info.persona,
        language=info.language,
        lead_id=info.lead_id,
        campaign_id=info.campaign_id,
        metadata_json=info.metadata_json,
    )
    for event in body.events:
        timestamp = None
        if event.get("timestamp"):
            timestamp = datetime.fromtimestamp(float(event["timestamp"]), tz=UTC)
        await recorder.record_event(
            call_id=call_id,
            event_type=str(event.get("event_type", "event")),
            state=event.get("state"),
            payload=event.get("payload") or {},
            timestamp=timestamp,
        )
    await recorder.record_transcript_batch(call_id, body.transcript)
    await recorder.record_usage_batch(call_id, body.usage)
    if body.recording:
        await recorder.record_recording(call_id, body.recording)

    fin = body.finalize
    await recorder.finalize_call(
        call_id=call_id,
        duration_secs=fin.duration_secs,
        termination_reason=fin.termination_reason,
        call_outcome=fin.call_outcome,
        outcome_rationale=fin.outcome_rationale,
        transcript_summary=fin.transcript_summary,
        total_cost=fin.total_cost,
        charging_details=fin.charging_details,
        destination_url=settings.webhook_destination(),
        agent_id=fin.agent_id,
        agent_name=fin.agent_name,
    )
    return ResultAck(ok=True, duplicate=False)
