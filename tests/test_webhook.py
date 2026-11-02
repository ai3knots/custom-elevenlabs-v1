"""Unit tests for ElevenLabs HMAC-SHA256 signature generator and transactional outbox worker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import Settings
from src.db.base import Base
from src.db.models import Call, CallStatus, WebhookOutbox, WebhookStatus
from src.webhook.signer import sign_payload, verify_signature
from src.webhook.worker import WebhookOutboxWorker, calculate_backoff_seconds


def test_signature_generation_and_verification() -> None:
    """Test HMAC-SHA256 signing and verification matching ElevenLabs contract."""
    secret = "test_webhook_secret_xyz"
    raw_payload = '{"event":"call_completed","outcome":"consultant_call_scheduled"}'
    fixed_ts = 1793208500

    # 1. Generate signature
    header = sign_payload(raw_payload, secret=secret, now_secs=fixed_ts)
    assert header.startswith(f"t={fixed_ts},v0=")

    # 2. Verify signature within tolerance
    assert verify_signature(
        header, raw_payload, secret=secret, now_secs=fixed_ts, tolerance_secs=300
    )

    # 3. Reject modified payload (tampering)
    tampered = '{"event":"call_completed","outcome":"do_not_call"}'
    assert not verify_signature(
        header, tampered, secret=secret, now_secs=fixed_ts, tolerance_secs=300
    )

    # 4. Reject wrong secret
    assert not verify_signature(
        header, raw_payload, secret="wrong_secret", now_secs=fixed_ts, tolerance_secs=300
    )

    # 5. Reject replay attack (timestamp outside tolerance)
    assert not verify_signature(
        header, raw_payload, secret=secret, now_secs=fixed_ts + 500, tolerance_secs=300
    )

    # 6. Reject malformed headers
    assert not verify_signature("invalid_header", raw_payload, secret=secret, now_secs=fixed_ts)
    assert not verify_signature(None, raw_payload, secret=secret, now_secs=fixed_ts)


def test_backoff_calculation() -> None:
    """Test exponential backoff calculations and upper bounds."""
    assert calculate_backoff_seconds(attempt=1, base=2.0, initial_delay_secs=2.0) == 2.0
    assert calculate_backoff_seconds(attempt=2, base=2.0, initial_delay_secs=2.0) == 4.0
    assert calculate_backoff_seconds(attempt=3, base=2.0, initial_delay_secs=2.0) == 8.0
    assert calculate_backoff_seconds(attempt=4, base=2.0, initial_delay_secs=2.0) == 16.0

    # Cap test
    assert (
        calculate_backoff_seconds(attempt=20, base=2.0, initial_delay_secs=2.0, max_delay_secs=3600.0)
        == 3600.0
    )


@pytest.fixture
async def db_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Provide an in-memory SQLite async session factory."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return session_factory


@pytest.mark.asyncio
async def test_worker_successful_delivery(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Test outbox worker successful delivery flow."""
    now = datetime.now(UTC)

    # Seed Call and WebhookOutbox
    async with db_sessionmaker() as session:
        call = Call(
            id="conv_test_123",
            phone="+14045551234",
            status=CallStatus.COMPLETED.value,
            started_at=now,
        )
        outbox = WebhookOutbox(
            id="outbox_123",
            call_id="conv_test_123",
            event_type="post_call_transcription",
            payload={"type": "post_call_transcription", "conversation_id": "conv_test_123"},
            destination_url="http://mock-lead-gen/api/webhooks/elevenlabs",
            status=WebhookStatus.PENDING.value,
            attempts=0,
            max_attempts=5,
            next_retry_at=now - timedelta(seconds=10),
            created_at=now,
        )
        session.add(call)
        session.add(outbox)
        await session.commit()

    recorded_requests: list[httpx.Request] = []

    def mock_transport(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        return httpx.Response(status_code=200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    settings = Settings(webhook={"webhook_secret": "my_secret_key"})  # type: ignore[arg-type]
    worker = WebhookOutboxWorker(
        session_factory=db_sessionmaker, settings=settings, client=client
    )

    processed_count = await worker.process_pending()
    assert processed_count == 1
    assert len(recorded_requests) == 1

    # Verify headers on outgoing request
    req = recorded_requests[0]
    assert "elevenlabs-signature" in req.headers
    assert req.headers["X-Delivery-Id"] == "outbox_123"
    sig = req.headers["elevenlabs-signature"]
    assert verify_signature(sig, req.content, secret="my_secret_key")

    # Verify DB record updated to DELIVERED
    async with db_sessionmaker() as session:
        stmt = select(WebhookOutbox).where(WebhookOutbox.id == "outbox_123")
        res = await session.execute(stmt)
        updated = res.scalar_one()
        assert updated.status == WebhookStatus.DELIVERED.value
        assert updated.delivered_at is not None
        assert updated.signature_header == sig
        assert updated.last_error is None


@pytest.mark.asyncio
async def test_worker_retry_on_failure(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Test outbox worker retry and backoff when endpoint returns 500 error."""
    now = datetime.now(UTC)

    async with db_sessionmaker() as session:
        call = Call(
            id="conv_test_456",
            phone="+14045551234",
            status=CallStatus.COMPLETED.value,
            started_at=now,
        )
        outbox = WebhookOutbox(
            id="outbox_456",
            call_id="conv_test_456",
            event_type="post_call_transcription",
            payload={"type": "post_call_transcription"},
            destination_url="http://mock-lead-gen/api/webhooks/elevenlabs",
            status=WebhookStatus.PENDING.value,
            attempts=1,
            max_attempts=3,
            next_retry_at=now - timedelta(seconds=10),
            created_at=now,
        )
        session.add(call)
        session.add(outbox)
        await session.commit()

    def mock_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=500, text="Internal Server Error")

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    settings = Settings(webhook={"webhook_secret": "my_secret_key", "retry_backoff_base": 2.0})  # type: ignore[arg-type]
    worker = WebhookOutboxWorker(
        session_factory=db_sessionmaker, settings=settings, client=client
    )

    success = await worker.deliver_entry("outbox_456")
    assert not success

    async with db_sessionmaker() as session:
        stmt = select(WebhookOutbox).where(WebhookOutbox.id == "outbox_456")
        res = await session.execute(stmt)
        updated = res.scalar_one()
        assert updated.attempts == 2
        assert updated.status == WebhookStatus.PENDING.value
        assert "HTTP 500" in (updated.last_error or "")
        # next_retry_at should be in the future (handle SQLite offset-naive return)
        retry_time = (
            updated.next_retry_at.replace(tzinfo=UTC)
            if updated.next_retry_at.tzinfo is None
            else updated.next_retry_at
        )
        assert retry_time > datetime.now(UTC)


@pytest.mark.asyncio
async def test_worker_dead_letter_transition(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Test that reaching max_attempts marks record as DEAD_LETTER."""
    now = datetime.now(UTC)

    async with db_sessionmaker() as session:
        call = Call(
            id="conv_test_789",
            phone="+14045551234",
            status=CallStatus.COMPLETED.value,
            started_at=now,
        )
        outbox = WebhookOutbox(
            id="outbox_789",
            call_id="conv_test_789",
            event_type="post_call_transcription",
            payload={"type": "post_call_transcription"},
            destination_url="http://mock-lead-gen/api/webhooks/elevenlabs",
            status=WebhookStatus.PENDING.value,
            attempts=2,
            max_attempts=3,
            next_retry_at=now - timedelta(seconds=10),
            created_at=now,
        )
        session.add(call)
        session.add(outbox)
        await session.commit()

    def mock_transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Network unreachable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    settings = Settings(webhook={"webhook_secret": "my_secret_key"})  # type: ignore[arg-type]
    worker = WebhookOutboxWorker(
        session_factory=db_sessionmaker, settings=settings, client=client
    )

    success = await worker.deliver_entry("outbox_789")
    assert not success

    async with db_sessionmaker() as session:
        stmt = select(WebhookOutbox).where(WebhookOutbox.id == "outbox_789")
        res = await session.execute(stmt)
        updated = res.scalar_one()
        assert updated.attempts == 3
        assert updated.status == WebhookStatus.DEAD_LETTER.value
        assert "ConnectError" in (updated.last_error or "")
