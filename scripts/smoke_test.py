"""Smoke test script verifying all subsystems in an end-to-end local dry run."""

from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import CallRequest, DynamicVariables, load_settings
from src.db.base import Base
from src.metrics.costs import CostAccumulator
from src.metrics.prometheus import get_latest_metrics
from src.persistence.recorder import CallRecorder
from src.prompts.manager import PromptManager
from src.telephony.mock import MockTelephonyProvider
from src.webhook.signer import sign_payload, verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("smoke_test")


async def run_smoke_test() -> bool:
    """Execute end-to-end subsystem verification."""
    print("=" * 60)
    print("POSIVEX VOICE AGENT — LOCAL DRY-RUN SMOKE TEST")
    print("=" * 60)

    success = True

    dyn = DynamicVariables(business_name="Smoke Test Pizza", contact_name="Tony")
    dispatch_res = None

    # 1. Configuration
    try:
        settings = load_settings(persona="amelia")
        print("  [PASS] 1. Layered Configuration Engine loaded (Persona: Amelia)")
    except Exception as e:
        print(f"  [FAIL] 1. Configuration Engine: {e}")
        return False

    # 2. Prompts & Knowledge Base
    try:
        prompt_mgr = PromptManager(persona="amelia", kb_config=settings.kb)
        first_msg = prompt_mgr.get_first_message(dyn)
        assert len(first_msg) > 10
        prompt_en = prompt_mgr.render_prompt(dyn)
        assert "Interchange" in prompt_en or "interchange" in prompt_en.lower()
        print("  [PASS] 2. Prompt & Knowledge Base Engine (Single prompt + Pricing)")
    except Exception as e:
        print(f"  [FAIL] 2. Prompts & KB: {e}")
        success = False

    # 3. Single system prompt
    try:
        assert "Step F: Closing the call" in prompt_en
        assert "{{" not in prompt_en
        print("  [PASS] 3. Single system prompt (Steps A-F, tools, knowledge base)")
    except Exception as e:
        print(f"  [FAIL] 3. System prompt: {e}")
        success = False

    # 4. Telephony Provider
    try:
        telephony = MockTelephonyProvider()
        call_req = CallRequest(
            call_id="conv_smoke_test_1",
            phone="+14045551234",
            dynamic_variables=dyn,
        )
        dispatch_res = await telephony.dial(call_req, room_name="smoke_test_room")
        assert dispatch_res.call_id == "conv_smoke_test_1"
        print("  [PASS] 4. Telephony Abstraction (Mock & Dispatch Interface)")
    except Exception as e:
        print(f"  [FAIL] 4. Telephony: {e}")
        success = False

    # 5. Database Persistence & Recorder
    try:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        recorder = CallRecorder(session_factory)

        call_id = dispatch_res.call_id if dispatch_res else "conv_smoke_test_1"
        first_text = first_msg if "first_msg" in locals() else "Hello"
        await recorder.create_call(call_id=call_id, phone="+14045551234")
        await recorder.record_transcript_batch(
            call_id,
            [
                {"role": "agent", "message": first_text, "time_in_call_secs": 1.0},
                {"role": "user", "message": "Yes, speaking.", "time_in_call_secs": 3.0},
            ],
        )

        accumulator = CostAccumulator()
        accumulator.add_stt_usage("nova-3", audio_seconds=12.5)
        accumulator.add_tts_usage("eleven_v3_conversational", characters_count=95)
        accumulator.add_llm_usage("gemini-3.5-flash-lite", input_tokens=420, output_tokens=35)
        accumulator.add_telephony_usage(duration_seconds=15.0)

        usage_records = accumulator.records
        await recorder.record_usage_batch(call_id, usage_records)
        await recorder.finalize_call(
            call_id=call_id,
            duration_secs=15.0,
            call_outcome="consultant_call_scheduled",
            total_cost=accumulator.total_cost,
            destination_url="http://localhost:3000/api/webhooks/elevenlabs",
        )
        print("  [PASS] 5. Database Persistence & Cost Tracking (Outbox Staged)")
    except Exception as e:
        print(f"  [FAIL] 5. Database & Cost Tracking: {e}")
        success = False

    # 6. HMAC-SHA256 Webhook Signing
    try:
        secret = "test_webhook_secret_key"
        payload = '{"type":"post_call_transcription","call_outcome":"consultant_call_scheduled"}'
        sig = sign_payload(payload, secret=secret)
        assert verify_signature(sig, payload, secret=secret)
        print("  [PASS] 6. HMAC-SHA256 Webhook Signer (ElevenLabs Contract Compatible)")
    except Exception as e:
        print(f"  [FAIL] 6. Webhook Signer: {e}")
        success = False

    # 7. Prometheus Metrics
    try:
        data, content_type = get_latest_metrics()
        assert len(data) > 0
        assert "text/plain" in content_type
        print("  [PASS] 7. Prometheus Metrics Exposition (/metrics Endpoint)")
    except Exception as e:
        print(f"  [FAIL] 7. Prometheus Metrics: {e}")
        success = False

    print("=" * 60)
    if success:
        print("ALL SUBSYSTEMS VERIFIED OPERATIONAL.")
    else:
        print("SOME SUBSYSTEMS FAILED.")
    print("=" * 60)
    return success


def main() -> None:
    ok = asyncio.run(run_smoke_test())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
