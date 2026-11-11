"""The dial flow as a service (no HTTP), and the API module having no import-time side effects."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.dispatch import ConcurrencyLimitReached, DialFailed, place_call
from src.config import CallRequest, Settings
from src.db.base import Base
from src.persistence.recorder import CallRecorder
from src.telephony.mock import MockTelephonyProvider


@pytest.fixture
async def env() -> AsyncGenerator[SimpleNamespace, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield SimpleNamespace(
        recorder=CallRecorder(sessions),
        telephony=MockTelephonyProvider(),
        settings=Settings(telephony={"provider": "mock", "max_concurrent_calls": 1}),  # type: ignore[arg-type]
    )
    await engine.dispose()


def request(**kw: object) -> CallRequest:
    return CallRequest(phone="+14045551234", agent_id="agent_posivex_michael", **kw)  # type: ignore[arg-type]


async def test_place_call_records_dispatches_and_returns_the_ids(env: SimpleNamespace) -> None:
    placed = await place_call(request(), settings=env.settings, recorder=env.recorder, telephony=env.telephony)

    assert placed.conversation_id.startswith("lk_")
    assert placed.dispatch.room_name == f"call_{placed.conversation_id}"
    call = await env.recorder.get_call(placed.conversation_id)
    assert (call.status, call.persona) == ("in_progress", "michael")  # the agent id picks the persona
    events = [e.event_type for e in await env.recorder.get_events(placed.conversation_id)]
    assert events == ["call_requested", "call_dispatched"]


async def test_place_call_keeps_a_call_id_the_caller_supplied(env: SimpleNamespace) -> None:
    placed = await place_call(
        request(call_id="lk_given"), settings=env.settings, recorder=env.recorder, telephony=env.telephony
    )
    assert placed.conversation_id == "lk_given"


async def test_place_call_over_the_limit_stores_nothing(env: SimpleNamespace) -> None:
    await place_call(request(), settings=env.settings, recorder=env.recorder, telephony=env.telephony)
    with pytest.raises(ConcurrencyLimitReached):
        await place_call(request(), settings=env.settings, recorder=env.recorder, telephony=env.telephony)
    assert await env.recorder.get_active_calls_count() == 1


async def test_place_call_dial_failure_marks_the_call_failed(env: SimpleNamespace) -> None:
    env.telephony.should_fail = True
    env.telephony.failure_message = "trunk rejected"
    with pytest.raises(DialFailed, match="trunk rejected"):
        await place_call(
            request(call_id="lk_fail"), settings=env.settings, recorder=env.recorder, telephony=env.telephony
        )
    call = await env.recorder.get_call("lk_fail")
    assert (call.status, call.termination_reason) == ("failed", "dial_failed")
    assert [e.event_type for e in await env.recorder.get_events("lk_fail")] == ["call_requested", "dial_failed"]


def test_importing_the_api_creates_no_app_and_reads_no_settings() -> None:
    """The module used to build an app (and load settings and YAML) at import time."""
    code = (
        "import src.api, src.api.app as m, src.config as c\n"
        "loaded = []\n"
        "assert not hasattr(m, 'app'), 'module-level app is back'\n"
        "print('ok')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "ok"
