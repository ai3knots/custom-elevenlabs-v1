"""Database persistence service for call lifecycle, events, transcripts, costs, and outbox."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.models import (
    Call,
    CallEvent,
    CallStatus,
    Transcript,
    UsageCost,
    WebhookOutbox,
    WebhookStatus,
)

logger = logging.getLogger("posivex.persistence.recorder")


class CallRecorder:
    """Handles async persistence of call records, event audits, transcripts, costs, and outbox."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def create_call(
        self,
        call_id: str,
        phone: str,
        persona: str = "amelia",
        language: str = "en",
        lead_id: str | None = None,
        campaign_id: str | None = None,
        status: str = CallStatus.IN_PROGRESS.value,
        metadata_json: dict[str, Any] | None = None,
    ) -> Call:
        """Create and persist an initial Call record."""
        async with self.session_factory() as session:
            call = Call(
                id=call_id,
                lead_id=lead_id,
                campaign_id=campaign_id,
                phone=phone,
                persona=persona,
                language=language,
                status=status,
                started_at=datetime.now(UTC),
                metadata_json=metadata_json or {},
            )
            session.add(call)
            await session.commit()
            logger.info("Persisted initial call record: id=%s, phone=%s", call_id, phone)
            return call

    async def record_event(
        self,
        call_id: str,
        event_type: str,
        state: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> CallEvent:
        """Record a state machine transition or operational event."""
        async with self.session_factory() as session:
            event = CallEvent(
                call_id=call_id,
                event_type=event_type,
                state=state,
                payload=payload or {},
                timestamp=datetime.now(UTC),
            )
            session.add(event)
            await session.commit()
            return event

    async def record_transcript(
        self,
        call_id: str,
        role: str,
        message: str,
        time_in_call_secs: float = 0.0,
        interrupted: bool = False,
    ) -> Transcript:
        """Record an individual spoken utterance in the conversation history."""
        async with self.session_factory() as session:
            entry = Transcript(
                call_id=call_id,
                role=role,
                message=message,
                time_in_call_secs=round(time_in_call_secs, 2),
                interrupted=interrupted,
                created_at=datetime.now(UTC),
            )
            session.add(entry)
            await session.commit()
            return entry

    async def record_usage_batch(
        self,
        call_id: str,
        usage_records: list[dict[str, Any]],
    ) -> list[UsageCost]:
        """Batch persist provider cost records."""
        if not usage_records:
            return []

        async with self.session_factory() as session:
            entities: list[UsageCost] = []
            for item in usage_records:
                entity = UsageCost(
                    call_id=call_id,
                    provider=item["provider"],
                    model=item["model"],
                    metric_type=item["metric_type"],
                    metric_value=item["metric_value"],
                    estimated_cost_usd=item["estimated_cost_usd"],
                    created_at=datetime.now(UTC),
                )
                session.add(entity)
                entities.append(entity)

            await session.commit()
            return entities

    async def finalize_call(
        self,
        call_id: str,
        status: str = CallStatus.COMPLETED.value,
        duration_secs: float = 0.0,
        termination_reason: str = "normal_completion",
        call_outcome: str = "other",
        outcome_rationale: str = "",
        call_successful: str = "unclear",
        transcript_summary: str = "",
        charging_details: dict[str, Any] | None = None,
        total_cost: float = 0.0,
        destination_url: str | None = None,
        create_outbox: bool = True,
    ) -> tuple[Call, WebhookOutbox | None]:
        """Finalize call metadata and transactionally stage webhook outbox payload."""
        async with self.session_factory() as session:
            stmt = select(Call).where(Call.id == call_id)
            res = await session.execute(stmt)
            call = res.scalar_one_or_none()

            now = datetime.now(UTC)
            if not call:
                raise ValueError(f"Call with id '{call_id}' not found for finalization.")

            call.status = status
            call.ended_at = now
            call.duration_secs = round(duration_secs, 2)
            call.termination_reason = termination_reason
            call.call_outcome = call_outcome
            call.outcome_rationale = outcome_rationale
            call.call_successful = call_successful
            call.transcript_summary = transcript_summary
            call.charging_details = charging_details or {}
            call.total_cost = round(total_cost, 4)
            call.updated_at = now

            outbox_entry: WebhookOutbox | None = None
            if create_outbox and destination_url:
                outbox_payload = {
                    "type": "post_call_transcription",
                    "event_timestamp": int(now.timestamp()),
                    "data": {
                        "conversation_id": call.id,
                        "status": "done",
                        "agent_name": f"Posivex - 5 - {call.persona.capitalize()}",
                        "analysis": {
                            "data_collection_results": {
                                "call_outcome": {
                                    "value": call_outcome,
                                    "rationale": outcome_rationale,
                                }
                            },
                            "transcript_summary": transcript_summary,
                            "call_successful": call_successful,
                        },
                        "metadata": {
                            "call_duration_secs": int(duration_secs),
                            "termination_reason": termination_reason,
                            "cost": round(total_cost, 4),
                            "charging": charging_details or {},
                        },
                    },
                }

                outbox_entry = WebhookOutbox(
                    id=f"outbox_{uuid.uuid4().hex[:16]}",
                    call_id=call.id,
                    event_type="post_call_transcription",
                    payload=outbox_payload,
                    destination_url=destination_url,
                    status=WebhookStatus.PENDING.value,
                    attempts=0,
                    created_at=now,
                    next_retry_at=now,
                )
                session.add(outbox_entry)

            await session.commit()
            logger.info("Finalized call id=%s: outcome=%s, cost=%.4f", call_id, call_outcome, total_cost)
            return call, outbox_entry

    async def get_call(self, call_id: str) -> Call | None:
        """Fetch a call record by primary ID."""
        async with self.session_factory() as session:
            stmt = select(Call).where(Call.id == call_id)
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    async def get_active_calls_count(self) -> int:
        """Count currently active calls (queued or in progress)."""
        async with self.session_factory() as session:
            stmt = select(Call).where(
                Call.status.in_([CallStatus.QUEUED.value, CallStatus.IN_PROGRESS.value])
            )
            res = await session.execute(stmt)
            return len(res.scalars().all())

    async def update_call_status(
        self,
        call_id: str,
        status: str,
        termination_reason: str | None = None,
    ) -> Call | None:
        """Update call lifecycle status and optional termination reason."""
        async with self.session_factory() as session:
            stmt = select(Call).where(Call.id == call_id)
            res = await session.execute(stmt)
            call = res.scalar_one_or_none()
            if not call:
                return None
            call.status = status
            if termination_reason:
                call.termination_reason = termination_reason
            call.updated_at = datetime.now(UTC)
            await session.commit()
            return call

    async def get_recent_call_by_phone(self, phone: str) -> Call | None:
        """Fetch the most recent call record for a given phone number."""
        async with self.session_factory() as session:
            stmt = (
                select(Call)
                .where(Call.phone == phone)
                .order_by(Call.created_at.desc())
                .limit(1)
            )
            res = await session.execute(stmt)
            return res.scalar_one_or_none()


