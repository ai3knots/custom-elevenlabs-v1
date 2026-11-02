"""Unit tests for cost calculations, usage accumulation, and CallRecorder persistence."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from src.db.base import Base
from src.db.models import Call, CallOutcome
from src.metrics.costs import CostAccumulator, calculate_cost
from src.persistence.recorder import CallRecorder


def test_calculate_cost():
    """Verify provider rate calculations for Deepgram, ElevenLabs, Gemini, and LiveKit."""
    # Deepgram: 60 seconds @ $0.0043/min = $0.0043
    cost_dg = calculate_cost("deepgram", "nova-3", "audio_seconds", 60.0)
    assert cost_dg == 0.0043

    # ElevenLabs: 1,000 characters @ $0.15/1k = $0.15
    cost_el = calculate_cost("elevenlabs", "eleven_v3_conversational", "characters", 1000.0)
    assert cost_el == 0.15

    # Gemini Flash Lite: 1,000,000 input tokens = $0.075, 1,000,000 output tokens = $0.30
    cost_gem_in = calculate_cost("google", "gemini-3.5-flash-lite", "tokens_input", 1_000_000.0)
    assert cost_gem_in == 0.075
    cost_gem_out = calculate_cost("google", "gemini-3.5-flash-lite", "tokens_output", 1_000_000.0)
    assert cost_gem_out == 0.30

    # Gemini Flash: 1,000,000 input tokens = $0.15, 1,000,000 output tokens = $0.60
    cost_gem_conv_in = calculate_cost("google", "gemini-3.5-flash", "tokens_input", 1_000_000.0)
    assert cost_gem_conv_in == 0.15
    cost_gem_conv_out = calculate_cost("google", "gemini-3.5-flash", "tokens_output", 1_000_000.0)
    assert cost_gem_conv_out == 0.60

    # Telephony: 60 seconds @ $0.004/min = $0.004
    cost_tel = calculate_cost("livekit", "sip_webrtc", "audio_seconds", 60.0)
    assert cost_tel == 0.004


def test_cost_accumulator():
    """Verify CostAccumulator aggregates multiple model events into breakdown and total cost."""
    acc = CostAccumulator()

    # Add 500 input and 200 output tokens on flash-lite
    acc.add_llm_usage("gemini-3.5-flash-lite", 500, 200)
    assert acc.llm_cost > 0.0

    # Add 250 characters on ElevenLabs
    acc.add_tts_usage("eleven_v3_conversational", 250)
    assert acc.tts_cost > 0.0

    # Add 30 seconds on Deepgram
    acc.add_stt_usage("nova-3", 30.0)
    assert acc.stt_cost > 0.0

    # Add 30 seconds transport
    acc.add_telephony_usage(30.0)
    assert acc.telephony_cost > 0.0

    breakdown = acc.get_charging_breakdown()
    assert "call_charge" in breakdown
    assert "llm_charge" in breakdown
    assert "tts_charge" in breakdown
    assert "stt_charge" in breakdown
    assert acc.total_cost > 0.0
    assert len(acc.records) == 5  # 2 LLM + 1 TTS + 1 STT + 1 Telephony


@pytest.fixture
async def async_session_factory():
    """Create in-memory SQLite async sessionmaker."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_call_recorder_end_to_end(async_session_factory):
    """Verify CallRecorder creates, updates, and finalizes call with outbox staging."""
    recorder = CallRecorder(async_session_factory)
    call_id = "call_rec_001"

    # 1. Create Call
    call = await recorder.create_call(
        call_id=call_id,
        phone="+14045551234",
        persona="amelia",
        language="en",
        lead_id="lead_999",
        metadata_json={"business_name": "Tony's Pizza"},
    )
    assert call.id == call_id
    assert call.lead_id == "lead_999"

    # 2. Record State Event
    event = await recorder.record_event(
        call_id=call_id,
        event_type="state_transition",
        state="conversation",
        payload={"reason": "Owner confirmed"},
    )
    assert event.state == "conversation"

    # 3. Record Transcripts
    t1 = await recorder.record_transcript(
        call_id=call_id,
        role="agent",
        message="Hi, this is Amelia from Clover Posivex.",
        time_in_call_secs=1.5,
    )
    assert t1.message == "Hi, this is Amelia from Clover Posivex."

    t2 = await recorder.record_transcript(
        call_id=call_id,
        role="user",
        message="Yes, speaking.",
        time_in_call_secs=4.2,
    )
    assert t2.role == "user"

    # 4. Record Usages
    acc = CostAccumulator()
    acc.add_tts_usage("eleven_v3_conversational", 100)
    acc.add_stt_usage("nova-3", 15.0)
    acc.add_llm_usage("gemini-3.5-flash-lite", 200, 80)
    usage_entities = await recorder.record_usage_batch(call_id, acc.records)
    assert len(usage_entities) == 4

    # 5. Finalize Call with Outbox Staging
    finalized_call, outbox_entry = await recorder.finalize_call(
        call_id=call_id,
        duration_secs=52.4,
        termination_reason="normal_completion",
        call_outcome=CallOutcome.CONSULTANT_CALL_SCHEDULED.value,
        outcome_rationale="Tony agreed to live demo Thursday at 2 PM.",
        call_successful="success",
        transcript_summary="Amelia spoke with Tony, demo confirmed.",
        charging_details=acc.get_charging_breakdown(),
        total_cost=acc.total_cost,
        destination_url="http://localhost:3000/api/webhooks/elevenlabs",
        create_outbox=True,
    )

    assert finalized_call.status == "completed"
    assert finalized_call.call_outcome == "consultant_call_scheduled"
    assert finalized_call.duration_secs == 52.4
    assert finalized_call.total_cost == acc.total_cost

    # Verify Outbox entry
    assert outbox_entry is not None
    assert outbox_entry.call_id == call_id
    assert outbox_entry.status == "pending"
    assert outbox_entry.payload["data"]["conversation_id"] == call_id
    assert (
        outbox_entry.payload["data"]["analysis"]["data_collection_results"]["call_outcome"]["value"]
        == "consultant_call_scheduled"
    )

    # 6. Query from DB with joined relationships
    async with async_session_factory() as session:
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
        res = await session.execute(stmt)
        queried = res.scalar_one()

        assert len(queried.events) == 1
        assert len(queried.transcripts) == 2
        assert len(queried.usage_costs) == 4
        assert len(queried.webhook_outbox) == 1
