"""Database persistence service for call lifecycle, events, transcripts, costs, and outbox."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from src.db.models import (
    Call,
    CallEvent,
    CallStatus,
    Transcript,
    UsageCost,
    WebhookOutbox,
    WebhookStatus,
)
from src.webhook.payload import build_post_call_payload, call_successful_for

logger = logging.getLogger("posivex.persistence.recorder")

# A call that has been "active" for longer than this is treated as abandoned (the worker died without
# finalizing it), so it cannot block the concurrency limit forever.
STALE_ACTIVE_CALL_SECS = 30 * 60


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

    async def ensure_call(
        self,
        call_id: str,
        phone: str,
        persona: str = "amelia",
        language: str = "en",
        lead_id: str | None = None,
        campaign_id: str | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> Call:
        """Return the existing Call row, or create one when the API did not (e.g. browser test calls)."""
        existing = await self.get_call(call_id)
        if existing is not None:
            return existing
        return await self.create_call(
            call_id=call_id,
            phone=phone,
            persona=persona,
            language=language,
            lead_id=lead_id,
            campaign_id=campaign_id,
            metadata_json=metadata_json,
        )

    async def record_event(
        self,
        call_id: str,
        event_type: str,
        state: str | None = None,
        payload: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> CallEvent:
        """Record a state machine transition or operational event."""
        async with self.session_factory() as session:
            event = CallEvent(
                call_id=call_id,
                event_type=event_type,
                state=state,
                payload=payload or {},
                timestamp=timestamp or datetime.now(UTC),
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

    async def record_transcript_batch(
        self,
        call_id: str,
        entries: list[dict[str, Any]],
    ) -> int:
        """Persist buffered utterances (role, message, time_in_call_secs, interrupted) in one transaction."""
        if not entries:
            return 0

        async with self.session_factory() as session:
            now = datetime.now(UTC)
            for item in entries:
                session.add(
                    Transcript(
                        call_id=call_id,
                        role=item["role"],
                        message=item["message"],
                        time_in_call_secs=round(item.get("time_in_call_secs", 0.0), 2),
                        interrupted=bool(item.get("interrupted", False)),
                        created_at=now,
                    )
                )
            await session.commit()
            return len(entries)

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
        call_successful: str | None = None,
        transcript_summary: str | None = "",
        charging_details: dict[str, Any] | None = None,
        total_cost: float = 0.0,
        destination_url: str | None = None,
        create_outbox: bool = True,
        agent_id: str | None = None,
        agent_name: str | None = None,
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
            call.call_successful = call_successful or call_successful_for(call_outcome)
            call.transcript_summary = transcript_summary or ""
            call.charging_details = charging_details or {}
            call.total_cost = round(total_cost, 4)
            call.updated_at = now

            outbox_entry: WebhookOutbox | None = None
            if create_outbox and destination_url:
                transcripts = (
                    await session.execute(
                        select(Transcript).where(Transcript.call_id == call.id).order_by(Transcript.id)
                    )
                ).scalars().all()
                events = (
                    await session.execute(
                        select(CallEvent).where(CallEvent.call_id == call.id).order_by(CallEvent.id)
                    )
                ).scalars().all()
                outbox_payload = build_post_call_payload(
                    call,
                    transcripts,
                    events,
                    event_timestamp=int(now.timestamp()),
                    agent_id=agent_id,
                    agent_name=agent_name,
                )

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
        """Count calls that are queued or in progress and not abandoned (started within the stale window)."""
        cutoff = datetime.now(UTC) - timedelta(seconds=STALE_ACTIVE_CALL_SECS)
        async with self.session_factory() as session:
            stmt = select(func.count()).select_from(Call).where(
                Call.status.in_([CallStatus.QUEUED.value, CallStatus.IN_PROGRESS.value]),
                Call.created_at >= cutoff,
            )
            return int((await session.execute(stmt)).scalar_one())

    async def get_transcripts(self, call_id: str) -> list[Transcript]:
        async with self.session_factory() as session:
            res = await session.execute(
                select(Transcript).where(Transcript.call_id == call_id).order_by(Transcript.id)
            )
            return list(res.scalars().all())

    async def get_events(self, call_id: str) -> list[CallEvent]:
        async with self.session_factory() as session:
            res = await session.execute(
                select(CallEvent).where(CallEvent.call_id == call_id).order_by(CallEvent.id)
            )
            return list(res.scalars().all())

    async def list_calls_since(self, since: datetime, limit: int = 100) -> list[Call]:
        """Calls that started at or after `since`, newest first."""
        async with self.session_factory() as session:
            res = await session.execute(
                select(Call).where(Call.started_at >= since).order_by(Call.started_at.desc()).limit(limit)
            )
            return list(res.scalars().all())

    async def merge_call_metadata(self, call_id: str, updates: dict[str, Any]) -> None:
        """Merge keys into the call's metadata_json (e.g. the recording location)."""
        async with self.session_factory() as session:
            call = (await session.execute(select(Call).where(Call.id == call_id))).scalar_one_or_none()
            if call is None:
                return
            call.metadata_json = {**(call.metadata_json or {}), **updates}
            flag_modified(call, "metadata_json")
            await session.commit()

    async def record_recording(self, call_id: str, info: dict[str, Any]) -> None:
        """Remember where the call's audio was written (bucket, key, file type)."""
        await self.merge_call_metadata(call_id, {"recording": info})

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
