"""Security and robustness behaviour added after the audit: key check, override limits, log privacy, outbox lease."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.agent import session_handler
from src.api.app import create_app
from src.config import CallRequest, Settings, load_settings
from src.db.base import Base
from src.db.models import Call, CallStatus, WebhookOutbox, WebhookStatus
from src.logging import StructuredJsonFormatter, mask_phone, redact_sensitive_data
from src.telephony.mock import MockTelephonyProvider
from src.webhook.worker import DELIVERY_LEASE_SECS, WebhookOutboxWorker

DIAL = {"to_number": "+14045551234", "agent_id": "agent_posivex_amelia"}


# ---- the dial route's key check ----------------------------------------------------------------------------
async def _post_dial(settings: Settings, headers: dict[str, str]) -> httpx.Response:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    app = create_app(settings=settings, session_factory=sessions, telephony_provider=MockTelephonyProvider())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/v1/convai/sip-trunk/outbound-call", headers=headers, json=DIAL)
    await engine.dispose()
    return res


async def test_no_configured_key_refuses_every_request_instead_of_accepting_any() -> None:
    settings = Settings(voice_api_key="", elevenlabs_api_key="", telephony={"provider": "mock"})  # type: ignore[arg-type]
    res = await _post_dial(settings, {"xi-api-key": "anything"})
    assert (res.status_code, res.json()) == (503, {"detail": "The API key is not configured (POSIVEX_API_KEY)"})


async def test_a_missing_header_is_still_401_when_no_key_is_configured() -> None:
    settings = Settings(voice_api_key="", elevenlabs_api_key="", telephony={"provider": "mock"})  # type: ignore[arg-type]
    assert (await _post_dial(settings, {})).status_code == 401


async def test_the_elevenlabs_key_still_works_as_the_shared_key_when_there_is_no_dedicated_one() -> None:
    settings = Settings(voice_api_key="", elevenlabs_api_key="el-key", telephony={"provider": "mock"})  # type: ignore[arg-type]
    assert (await _post_dial(settings, {"xi-api-key": "el-key"})).status_code == 200
    assert (await _post_dial(settings, {"xi-api-key": "other"})).status_code == 401


# ---- per-call overrides ------------------------------------------------------------------------------------
def test_overrides_may_tune_the_conversation() -> None:
    request = CallRequest(
        phone="+14045551234", config_overrides={"llm": {"temperature": 0.2}, "conversation": {"silence_end_call_seconds": 20}}
    )
    assert request.config_overrides["llm"] == {"temperature": 0.2}


@pytest.mark.parametrize("section", ["recording", "webhook", "telephony", "kb", "results_callback_url", "gemini_api_key"])
def test_overrides_may_not_touch_infrastructure_settings(section: str) -> None:
    with pytest.raises(ValidationError, match="config_overrides may only set"):
        CallRequest(phone="+14045551234", config_overrides={section: {"x": "y"}})


async def test_the_dial_route_answers_400_for_a_forbidden_override() -> None:
    settings = Settings(voice_api_key="k", telephony={"provider": "mock"})  # type: ignore[arg-type]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    app = create_app(settings=settings, session_factory=sessions, telephony_provider=MockTelephonyProvider())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/v1/convai/sip-trunk/outbound-call",
            headers={"xi-api-key": "k"},
            json={**DIAL, "config_overrides": {"recording": {"bucket": "elsewhere"}}},
        )
    await engine.dispose()
    assert res.status_code == 400 and res.json()["success"] is False


# ---- configuration -----------------------------------------------------------------------------------------
def test_telephony_defaults_to_the_real_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEPHONY_PROVIDER", raising=False)
    assert load_settings().telephony.provider == "livekit_sip"


def test_generic_environment_names_cannot_change_how_settings_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA", "michael")
    monkeypatch.setenv("LANGUAGE", "en_US:en")
    assert load_settings().persona == "amelia"


# ---- logs without phone numbers ----------------------------------------------------------------------------
def test_mask_phone_keeps_only_the_last_four_digits() -> None:
    assert mask_phone("+14045551234") == "***1234"
    assert mask_phone("") == "***" and mask_phone(None) == "***"


def test_json_log_lines_mask_phone_numbers_in_messages_and_extras() -> None:
    assert redact_sensitive_data("Dialing +14045551234 now") == "Dialing ***1234 now"
    record = logging.LogRecord("t", logging.INFO, "f.py", 1, "call to %s", ("+14045551234",), None)
    record.number = "+14045551234"
    parsed = json.loads(StructuredJsonFormatter().format(record))
    assert parsed["message"] == "call to ***1234" and parsed["extra"]["number"] == "***1234"


def test_a_metadata_parse_error_is_logged_without_the_phone_number(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_DEFAULT_CALL_REQUEST", raising=False)
    with caplog.at_level(logging.ERROR, logger="posivex.session"), pytest.raises(ValueError):
        session_handler.parse_call_request_from_room('{"phone": "+14045551234", "persona": "nobody"}')
    logged = caplog.text
    assert "4045551234" not in logged
    assert "persona" in logged  # which field was wrong is still there


# ---- the outbox lease --------------------------------------------------------------------------------------
@pytest.fixture
async def outbox() -> AsyncGenerator[SimpleNamespace, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    posted: list[httpx.Request] = []

    async def seed(status: str, next_retry_in: float) -> None:
        now = datetime.now(UTC)
        async with sessions() as session:
            session.add(Call(id="c1", phone="+14045551234", status=CallStatus.COMPLETED.value, started_at=now))
            session.add(
                WebhookOutbox(
                    id="o1", call_id="c1", payload={"type": "post_call_transcription"}, destination_url="http://leadgen/hook",
                    status=status, attempts=0, max_attempts=5, next_retry_at=now + timedelta(seconds=next_retry_in), created_at=now,
                )
            )
            await session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    worker = WebhookOutboxWorker(sessions, Settings(webhook={"webhook_secret": "s"}), client=client)  # type: ignore[arg-type]
    yield SimpleNamespace(sessions=sessions, posted=posted, seed=seed, worker=worker)
    await client.aclose()
    await engine.dispose()


async def test_an_entry_another_worker_is_delivering_is_not_delivered_twice(outbox: SimpleNamespace) -> None:
    await outbox.seed(WebhookStatus.PROCESSING.value, next_retry_in=30)  # claimed, lease still running
    assert await outbox.worker.deliver_entry("o1") is False
    assert outbox.posted == []


async def test_an_entry_whose_worker_died_is_delivered_after_the_lease(outbox: SimpleNamespace) -> None:
    await outbox.seed(WebhookStatus.PROCESSING.value, next_retry_in=-1)  # lease expired
    assert await outbox.worker.deliver_entry("o1") is True
    assert len(outbox.posted) == 1


async def test_claiming_an_entry_sets_a_lease_while_it_is_being_delivered(outbox: SimpleNamespace) -> None:
    await outbox.seed(WebhookStatus.PENDING.value, next_retry_in=-1)
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200)

    outbox.worker._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def peek() -> None:
        async with outbox.sessions() as session:
            row = (await session.execute(select(WebhookOutbox))).scalar_one()
            seen["status"], seen["lease"] = row.status, row.next_retry_at

    original = outbox.worker._client.post

    async def post_after_peek(*args: Any, **kwargs: Any) -> httpx.Response:
        await peek()
        return await original(*args, **kwargs)

    outbox.worker._client.post = post_after_peek  # type: ignore[method-assign]
    assert await outbox.worker.deliver_entry("o1") is True
    assert seen["status"] == WebhookStatus.PROCESSING.value
    lease = seen["lease"] if seen["lease"].tzinfo else seen["lease"].replace(tzinfo=UTC)
    assert lease > datetime.now(UTC) + timedelta(seconds=DELIVERY_LEASE_SECS - 5)
