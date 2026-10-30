"""Unit tests for async database models, relationships, and outbox status transitions."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from src.db.base import Base
from src.db.models import (
    Call,
    CallEvent,
    CallOutcome,
    CallStatus,
    Transcript,
    UsageCost,
    WebhookOutbox,
    WebhookStatus,
)


@pytest.fixture
async def async_session():
    """Create an in-memory SQLite async engine and session for testing."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        yield session

    await engine.dispose()


async def test_create_and_query_call(async_session: AsyncSession):
    """Verify call creation, dynamic metadata persistence, and querying."""
    call = Call(
        id="call_test_001",
        lead_id="lead_abc_123",
        phone="+14045551234",
        persona="amelia",
        language="en",
        status=CallStatus.IN_PROGRESS.value,
        metadata_json={"business_name": "Tony's Pizza & Grill", "is_followup": False},
    )
    async_session.add(call)
    await async_session.commit()

    stmt = select(Call).where(Call.id == "call_test_001")
    result = await async_session.execute(stmt)
    saved_call = result.scalar_one()

    assert saved_call.id == "call_test_001"
    assert saved_call.lead_id == "lead_abc_123"
    assert saved_call.phone == "+14045551234"
    assert saved_call.status == CallStatus.IN_PROGRESS.value
    assert saved_call.metadata_json is not None
    assert saved_call.metadata_json["business_name"] == "Tony's Pizza & Grill"


async def test_call_lifecycle_and_outcome_update(async_session: AsyncSession):
    """Verify call completion updates duration, outcome enum, and rationale."""
    call = Call(
        id="call_test_002",
        lead_id="lead_xyz_789",
        phone="+14045559876",
        persona="amelia",
        status=CallStatus.IN_PROGRESS.value,
    )
    async_session.add(call)
    await async_session.commit()

    # Transition to completed with outcome
    call.status = CallStatus.COMPLETED.value
    call.ended_at = datetime.now(UTC)
    call.duration_secs = 85.5
    call.termination_reason = "normal_completion"
    call.call_outcome = CallOutcome.CONSULTANT_CALL_SCHEDULED.value
    call.outcome_rationale = "Tony agreed to a 10-minute demo on Thursday at 2 PM."
    call.call_successful = "success"
    await async_session.commit()

    stmt = select(Call).where(Call.id == "call_test_002")
    res = await async_session.execute(stmt)
    updated = res.scalar_one()

    assert updated.status == CallStatus.COMPLETED.value
    assert updated.call_outcome == "consultant_call_scheduled"
    assert updated.duration_secs == 85.5
    assert updated.call_successful == "success"


async def test_relationships_and_cascade_delete(async_session: AsyncSession):
    """Verify child events, transcripts, costs, and webhook records link properly and cascade delete."""
    call_id = "call_test_cascade"
    call = Call(
        id=call_id,
        phone="+14045551122",
        status=CallStatus.IN_PROGRESS.value,
    )
    async_session.add(call)

    # Add CallEvent
    event = CallEvent(
        call_id=call_id,
        event_type="state_transition",
        state="reach_owner",
        payload={"from": "opener", "to": "reach_owner"},
    )
    async_session.add(event)

    # Add Transcript
    transcript = Transcript(
        call_id=call_id,
        role="agent",
        message="Hi, this is Amelia from Clover Posivex.",
        time_in_call_secs=1.2,
        interrupted=False,
    )
    async_session.add(transcript)

    # Add UsageCost
    cost = UsageCost(
        call_id=call_id,
        provider="elevenlabs",
        model="eleven_v3_conversational",
        metric_type="characters",
        metric_value=42.0,
        estimated_cost_usd=0.003,
    )
    async_session.add(cost)

    # Add WebhookOutbox
    outbox = WebhookOutbox(
        id="outbox_001",
        call_id=call_id,
        event_type="post_call_transcription",
        payload={"call_id": call_id, "status": "completed"},
        destination_url="http://localhost:3000/api/webhooks/elevenlabs",
        status=WebhookStatus.PENDING.value,
    )
    async_session.add(outbox)
    await async_session.commit()

    # Query with relationships loaded
    stmt = (
        select(Call)
        .options(
            selectinload(Call.events),
            selectinload(Call.transcripts),
            selectinload(Call.usage_costs),
            selectinload(Call.webhook_outbox),
        )
        .where(Call.id == call_id)
    )
    res = await async_session.execute(stmt)
    loaded_call = res.scalar_one()

    assert len(loaded_call.events) == 1
    assert loaded_call.events[0].state == "reach_owner"
    assert len(loaded_call.transcripts) == 1
    assert loaded_call.transcripts[0].message == "Hi, this is Amelia from Clover Posivex."
    assert len(loaded_call.usage_costs) == 1
    assert loaded_call.usage_costs[0].metric_value == 42.0
    assert len(loaded_call.webhook_outbox) == 1
    assert loaded_call.webhook_outbox[0].status == WebhookStatus.PENDING.value

    # Delete parent call and verify cascade
    await async_session.delete(loaded_call)
    await async_session.commit()

    events_res = await async_session.execute(select(CallEvent).where(CallEvent.call_id == call_id))
    assert events_res.all() == []

    transcripts_res = await async_session.execute(
        select(Transcript).where(Transcript.call_id == call_id)
    )
    assert transcripts_res.all() == []

    costs_res = await async_session.execute(select(UsageCost).where(UsageCost.call_id == call_id))
    assert costs_res.all() == []

    outbox_res = await async_session.execute(
        select(WebhookOutbox).where(WebhookOutbox.call_id == call_id)
    )
    assert outbox_res.all() == []


async def test_webhook_outbox_retry_lifecycle(async_session: AsyncSession):
    """Verify outbox entry progression through pending, failed, and delivered states."""
    call_id = "call_outbox_test"
    call = Call(id=call_id, phone="+14045553344", status=CallStatus.COMPLETED.value)
    async_session.add(call)

    outbox = WebhookOutbox(
        id="outbox_retry_001",
        call_id=call_id,
        payload={"event": "transcription"},
        destination_url="http://localhost:3000/api/webhooks/elevenlabs",
        status=WebhookStatus.PENDING.value,
        attempts=0,
        max_attempts=5,
    )
    async_session.add(outbox)
    await async_session.commit()

    # Simulate 1st failed attempt
    outbox.status = WebhookStatus.FAILED.value
    outbox.attempts = 1
    outbox.last_error = "Connection refused (HTTP 503)"
    await async_session.commit()

    stmt = select(WebhookOutbox).where(WebhookOutbox.id == "outbox_retry_001")
    r1 = await async_session.execute(stmt)
    o1 = r1.scalar_one()
    assert o1.status == WebhookStatus.FAILED.value
    assert o1.attempts == 1
    assert o1.last_error == "Connection refused (HTTP 503)"

    # Simulate success on 2nd attempt
    o1.status = WebhookStatus.DELIVERED.value
    o1.attempts = 2
    o1.delivered_at = datetime.now(UTC)
    await async_session.commit()

    r2 = await async_session.execute(stmt)
    o2 = r2.scalar_one()
    assert o2.status == WebhookStatus.DELIVERED.value
    assert o2.attempts == 2
    assert o2.delivered_at is not None
