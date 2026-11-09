"""Tests for the lead-gen integration: ElevenLabs-compatible API, full payloads, results callback, recording."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.app import create_app
from src.config import RecordingConfig, Settings
from src.db.base import Base
from src.db.models import Call, WebhookOutbox
from src.persistence.recorder import CallRecorder
from src.persistence.remote import RemoteRecorder
from src.telephony.mock import MockTelephonyProvider
from src.telephony.recording import recording_key, start_call_recording

MASTER_AGENT = "agent_2701m6e2fw2eehd97r93d4v8x9yc"
MICHAEL_AGENT = "agent_posivex_michael"
HEADERS = {"xi-api-key": "voice-key"}
CALLBACK = {"X-Posivex-Callback-Secret": "cb-secret"}


@pytest.fixture
async def env() -> AsyncGenerator[SimpleNamespace, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    telephony = MockTelephonyProvider()
    settings = Settings(
        environment="development",
        elevenlabs_api_key="real-elevenlabs-key",
        voice_api_key="voice-key",
        results_callback_secret="cb-secret",
        telephony={"provider": "mock", "max_concurrent_calls": 2},  # type: ignore[arg-type]
        webhook={"lead_gen_base_url": "https://leadgen.example.com", "webhook_secret": "whsec"},  # type: ignore[arg-type]
    )
    app = create_app(settings=settings, session_factory=session_factory, telephony_provider=telephony)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(
            client=client,
            telephony=telephony,
            sessions=session_factory,
            recorder=CallRecorder(session_factory),
            settings=settings,
        )
    await engine.dispose()


async def dial(env: SimpleNamespace, agent_id: str = MASTER_AGENT) -> str:
    res = await env.client.post(
        "/v1/convai/sip-trunk/outbound-call",
        headers=HEADERS,
        json={
            "agent_id": agent_id,
            "agent_phone_number_id": "phnum_posivex_trunk",
            "to_number": "+14045551234",
            "conversation_initiation_client_data": {"dynamic_variables": {"business_name": "Tony's Pizza"}},
        },
    )
    assert res.status_code == 200, res.text
    conversation_id = str(res.json()["conversation_id"])
    assert conversation_id.startswith("lk_")  # lead-gen routes by this prefix
    return conversation_id


async def finish_call(env: SimpleNamespace, call_id: str, outcome: str = "callback_set") -> None:
    await env.recorder.record_transcript_batch(
        call_id,
        [
            {"role": "agent", "message": "Hi, this is Amelia from Clover Posivex.", "time_in_call_secs": 1.0},
            {"role": "user", "message": "Yes, speaking.", "time_in_call_secs": 5.0},
        ],
    )
    await env.recorder.record_event(call_id, "end_call", payload={"reason": "conversation_complete"})
    await env.recorder.finalize_call(
        call_id=call_id,
        duration_secs=42.0,
        termination_reason="closing_line_detected",
        call_outcome=outcome,
        outcome_rationale="Callback Saturday 6 PM.",
        transcript_summary="Owner asked for a callback.",
        total_cost=0.12,
        destination_url=env.settings.webhook_destination(),
        agent_id=MASTER_AGENT,
        agent_name="Posivex - 6 - Amelia - Single Master Prompt",
    )


# ---- auth ----------------------------------------------------------------------------------------
async def test_dedicated_voice_key_is_required_and_the_elevenlabs_key_is_not_accepted(env: SimpleNamespace) -> None:
    assert (await env.client.get("/v1/convai/agents")).status_code == 401
    assert (await env.client.get("/v1/convai/agents", headers={"xi-api-key": "real-elevenlabs-key"})).status_code == 401
    assert (await env.client.get("/v1/convai/agents", headers=HEADERS)).status_code == 200


# ---- agents and phone numbers ----------------------------------------------------------------------
async def test_agents_and_phone_numbers_lists(env: SimpleNamespace) -> None:
    agents = (await env.client.get("/v1/convai/agents", headers=HEADERS)).json()
    assert [a["agent_id"] for a in agents["agents"]] == [MASTER_AGENT, MICHAEL_AGENT]
    numbers = (await env.client.get("/v1/convai/phone-numbers", headers=HEADERS)).json()
    assert numbers[0]["phone_number_id"] == "phnum_posivex_trunk"
    assert numbers[0]["provider"] == "sip_trunk"


# ---- dialing: agent id picks the persona ------------------------------------------------------------
async def test_agent_id_selects_the_persona(env: SimpleNamespace) -> None:
    await dial(env, MICHAEL_AGENT)
    await dial(env, "agent_some_old_elevenlabs_agent")
    personas = [req.persona for req, _ in env.telephony.dispatched_calls]
    assert personas == ["michael", "amelia"]


# ---- conversation details -----------------------------------------------------------------------------
async def test_conversation_in_progress_then_done(env: SimpleNamespace) -> None:
    call_id = await dial(env)
    running = (await env.client.get(f"/v1/convai/conversations/{call_id}", headers=HEADERS)).json()
    assert running["status"] == "in-progress"
    assert running["transcript"] == [] and running["analysis"] == {}
    assert running["agent_id"] == MASTER_AGENT
    assert running["conversation_initiation_client_data"]["dynamic_variables"]["business_name"] == "Tony's Pizza"

    await finish_call(env, call_id)
    done = (await env.client.get(f"/v1/convai/conversations/{call_id}", headers=HEADERS)).json()
    assert done["status"] == "done"
    assert done["analysis"]["data_collection_results"]["call_outcome"]["value"] == "callback_set"
    assert done["analysis"]["call_successful"] == "success"
    assert done["analysis"]["transcript_summary"] == "Owner asked for a callback."
    assert done["metadata"]["call_duration_secs"] == 42
    assert done["metadata"]["termination_reason"] == "closing_line_detected"
    assert done["metadata"]["phone_call"]["external_number"] == "+14045551234"
    assert done["metadata"]["phone_call"]["direction"] == "outbound"
    assert done["metadata"]["phone_call"]["call_sid"].startswith("sip_")
    messages = [(t["role"], t["message"]) for t in done["transcript"] if t["message"]]
    assert messages == [("agent", "Hi, this is Amelia from Clover Posivex."), ("user", "Yes, speaking.")]
    tool_names = [c["tool_name"] for t in done["transcript"] for c in t["tool_calls"]]
    assert tool_names == ["end_call"]

    assert (await env.client.get("/v1/convai/conversations/conv_missing", headers=HEADERS)).status_code == 404


async def test_conversation_list_filters_by_agent_and_start_time(env: SimpleNamespace) -> None:
    a = await dial(env, MASTER_AGENT)
    b = await dial(env, MICHAEL_AGENT)
    since = int((datetime.now(UTC) - timedelta(hours=1)).timestamp())

    everything = (await env.client.get(f"/v1/convai/conversations?call_start_after_unix={since}", headers=HEADERS)).json()
    assert {c["conversation_id"] for c in everything["conversations"]} == {a, b}
    only_michael = (
        await env.client.get(f"/v1/convai/conversations?agent_id={MICHAEL_AGENT}&call_start_after_unix={since}", headers=HEADERS)
    ).json()
    assert [c["conversation_id"] for c in only_michael["conversations"]] == [b]
    future = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
    assert (await env.client.get(f"/v1/convai/conversations?call_start_after_unix={future}", headers=HEADERS)).json()[
        "conversations"
    ] == []


# ---- post-call webhook payload -------------------------------------------------------------------------
async def test_post_call_webhook_payload_is_complete_and_signed_delivery_is_staged(env: SimpleNamespace) -> None:
    call_id = await dial(env)
    await finish_call(env, call_id, outcome="declined")

    async with env.sessions() as session:
        outbox = (await session.execute(select(WebhookOutbox))).scalar_one()
    assert outbox.destination_url == "https://leadgen.example.com/api/webhooks/elevenlabs"
    payload = outbox.payload
    assert payload["type"] == "post_call_transcription"
    data = payload["data"]
    assert data["conversation_id"] == call_id
    assert data["agent_id"] == MASTER_AGENT
    assert data["status"] == "done"
    assert data["analysis"]["call_successful"] == "failure"
    assert len(data["transcript"]) == 3  # two spoken turns plus the end_call tool turn
    assert data["metadata"]["start_time_unix_secs"] > 0


# ---- results callback (agent hosted on LiveKit Cloud) ----------------------------------------------------
RESULT_BODY: dict[str, Any] = {
    "call": {"phone": "+14045551234", "persona": "amelia", "language": "en"},
    "transcript": [
        {"role": "agent", "message": "Hello!", "time_in_call_secs": 1.0, "interrupted": False},
        {"role": "user", "message": "Hi.", "time_in_call_secs": 2.0, "interrupted": False},
    ],
    "usage": [
        {"provider": "google", "model": "gemini-3.5-flash", "metric_type": "tokens_input", "metric_value": 100,
         "estimated_cost_usd": 0.001}
    ],
    "events": [{"event_type": "voicemail_detected", "payload": {"reason": "x"}, "timestamp": 1.0}],
    "recording": {"egress_id": "EG_1", "bucket": "b", "key": "recordings/c.ogg", "file_type": "ogg"},
    "finalize": {"duration_secs": 12.5, "termination_reason": "closing_line_detected", "call_outcome": "voicemail",
                 "outcome_rationale": "AMD", "transcript_summary": "Voicemail.", "total_cost": 0.05},
}


async def test_results_callback_requires_the_secret(env: SimpleNamespace) -> None:
    call_id = await dial(env)
    url = f"/internal/calls/{call_id}/result"
    assert (await env.client.post(url, json=RESULT_BODY)).status_code == 401
    assert (await env.client.post(url, json=RESULT_BODY, headers={"X-Posivex-Callback-Secret": "nope"})).status_code == 401


async def test_results_callback_stores_the_call_and_is_idempotent(env: SimpleNamespace) -> None:
    call_id = await dial(env)
    url = f"/internal/calls/{call_id}/result"
    first = await env.client.post(url, json=RESULT_BODY, headers=CALLBACK)
    assert first.json() == {"ok": True, "duplicate": False}
    again = await env.client.post(url, json=RESULT_BODY, headers=CALLBACK)
    assert again.json() == {"ok": True, "duplicate": True}

    call = await env.recorder.get_call(call_id)
    assert call is not None and call.status == "completed" and call.call_outcome == "voicemail"
    assert (call.metadata_json or {})["recording"]["key"] == "recordings/c.ogg"
    assert len(await env.recorder.get_transcripts(call_id)) == 2
    async with env.sessions() as session:
        assert len((await session.execute(select(WebhookOutbox))).scalars().all()) == 1


async def test_results_callback_creates_the_call_when_the_api_never_saw_it(env: SimpleNamespace) -> None:
    res = await env.client.post("/internal/calls/call_browser_1/result", json=RESULT_BODY, headers=CALLBACK)
    assert res.status_code == 200
    assert (await env.recorder.get_call("call_browser_1")) is not None


async def test_remote_recorder_reports_everything_in_one_request(env: SimpleNamespace) -> None:
    call_id = await dial(env)
    remote = RemoteRecorder("http://test", "cb-secret", client=env.client, attempts=1)
    await remote.ensure_call(call_id, phone="+14045551234")
    await remote.record_event(call_id, "end_call", payload={"reason": "conversation_complete"})
    await remote.record_transcript_batch(call_id, RESULT_BODY["transcript"])
    await remote.record_usage_batch(call_id, RESULT_BODY["usage"])
    await remote.record_recording(call_id, RESULT_BODY["recording"])
    await remote.finalize_call(
        call_id=call_id,
        duration_secs=30.0,
        termination_reason="silence_timeout",
        call_outcome="other",
        outcome_rationale="r",
        transcript_summary="s",
        total_cost=0.01,
        destination_url="ignored",
        agent_id=MASTER_AGENT,
        agent_name="n",
    )
    call = await env.recorder.get_call(call_id)
    assert call is not None and call.call_outcome == "other" and call.termination_reason == "silence_timeout"
    done = (await env.client.get(f"/v1/convai/conversations/{call_id}", headers=HEADERS)).json()
    assert done["has_audio"] is True
    assert [c["tool_name"] for t in done["transcript"] for c in t["tool_calls"]] == ["end_call"]


async def test_remote_recorder_retries_server_errors_but_not_rejections() -> None:
    calls: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503 if len(calls) < 3 else 200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as client:
        await RemoteRecorder("http://api", "s", client=client, attempts=4, backoff_secs=0).finalize_call("c1")
    assert len(calls) == 3

    rejected: list[int] = []

    def reject(request: httpx.Request) -> httpx.Response:
        rejected.append(1)
        return httpx.Response(401, json={"detail": "no"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        with pytest.raises(RuntimeError, match="401"):
            await RemoteRecorder("http://api", "s", client=client, attempts=4, backoff_secs=0).finalize_call("c1")
    assert len(rejected) == 1


# ---- recording --------------------------------------------------------------------------------------------
def make_recording_config(**kw: Any) -> RecordingConfig:
    return RecordingConfig(enabled=True, bucket="calls", access_key="ak", secret="sk", region="auto",
                           endpoint="https://r2.example.com", **kw)


async def test_start_call_recording_requests_audio_only_egress_to_s3() -> None:
    sent: list[Any] = []

    class FakeEgress:
        async def start_room_composite_egress(self, request: Any) -> Any:
            sent.append(request)
            return SimpleNamespace(egress_id="EG_123")

    lkapi = SimpleNamespace(egress=FakeEgress())
    info = await start_call_recording(lkapi, "call_room", "conv_1", make_recording_config())
    assert info == {"egress_id": "EG_123", "bucket": "calls", "key": "recordings/conv_1.ogg", "file_type": "ogg"}
    request = sent[0]
    assert request.room_name == "call_room" and request.audio_only is True
    assert not request.layout and not request.custom_base_url  # keeps the audio-only billing rate
    out = request.file_outputs[0]
    assert out.filepath == "recordings/conv_1.ogg" and out.s3.bucket == "calls" and out.s3.endpoint == "https://r2.example.com"


async def test_recording_is_off_without_config_and_failures_do_not_raise() -> None:
    assert await start_call_recording(object(), "r", "c", RecordingConfig()) is None

    class Boom:
        async def start_room_composite_egress(self, request: Any) -> Any:
            raise RuntimeError("egress down")

    assert await start_call_recording(SimpleNamespace(egress=Boom()), "r", "c", make_recording_config()) is None


def test_recording_key_uses_prefix_and_file_type() -> None:
    assert recording_key(make_recording_config(prefix="/a/b/", file_type="mp3"), "c1") == "a/b/c1.mp3"
    assert recording_key(make_recording_config(prefix=""), "c1") == "c1.ogg"


async def test_audio_endpoint_404_without_recording_and_streams_when_present(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    call_id = await dial(env)
    assert (await env.client.get(f"/v1/convai/conversations/{call_id}/audio", headers=HEADERS)).status_code == 404

    env.settings.recording = make_recording_config()
    await finish_call(env, call_id)
    await env.recorder.record_recording(call_id, {"key": "recordings/x.ogg", "file_type": "ogg"})

    async def fake_open(cfg: Any, key: str, file_type: str = "ogg") -> Any:
        async def chunks() -> Any:
            yield b"OggS"
            yield b"data"

        assert key == "recordings/x.ogg"
        return SimpleNamespace(content_type="audio/ogg", content_length=8, chunks=chunks())

    monkeypatch.setattr("src.api.routes.convai_read.open_recording", fake_open)
    res = await env.client.get(f"/v1/convai/conversations/{call_id}/audio", headers=HEADERS)
    assert res.status_code == 200 and res.headers["content-type"] == "audio/ogg" and res.content == b"OggSdata"


# ---- concurrency ------------------------------------------------------------------------------------------
async def test_abandoned_calls_stop_counting_towards_the_concurrency_limit(env: SimpleNamespace) -> None:
    call_id = await dial(env)
    assert await env.recorder.get_active_calls_count() == 1
    async with env.sessions() as session:
        call = (await session.execute(select(Call).where(Call.id == call_id))).scalar_one()
        call.created_at = datetime.now(UTC) - timedelta(hours=2)
        await session.commit()
    assert await env.recorder.get_active_calls_count() == 0


async def test_naive_database_times_are_read_as_utc(env: SimpleNamespace) -> None:
    """SQLite returns naive datetimes; the payload must not shift them by the server's local offset."""
    call_id = await dial(env)
    async with env.sessions() as session:
        call = (await session.execute(select(Call).where(Call.id == call_id))).scalar_one()
        call.started_at = datetime(2026, 11, 9, 12, 0, 0)  # naive, meant as UTC
        await session.commit()
    data = (await env.client.get(f"/v1/convai/conversations/{call_id}", headers=HEADERS)).json()
    assert data["metadata"]["start_time_unix_secs"] == int(datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC).timestamp())
    listed = (await env.client.get("/v1/convai/conversations?call_start_after_unix=0", headers=HEADERS)).json()
    assert listed["conversations"][0]["start_time_unix_secs"] == data["metadata"]["start_time_unix_secs"]


async def test_calling_number_id_is_kept_with_the_call(env: SimpleNamespace) -> None:
    call_id = await dial(env)
    call = await env.recorder.get_call(call_id)
    assert call is not None and (call.metadata_json or {})["phone_number_id"] == "phnum_posivex_trunk"
    data = (await env.client.get(f"/v1/convai/conversations/{call_id}", headers=HEADERS)).json()
    assert data["metadata"]["phone_call"]["phone_number_id"] == "phnum_posivex_trunk"


async def audio_detail(env: SimpleNamespace, call_id: str) -> str:
    res = await env.client.get(f"/v1/convai/conversations/{call_id}/audio", headers=HEADERS)
    assert res.status_code == 404
    return str(res.json()["detail"])


async def test_audio_404_says_why_there_is_nothing_to_play(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    call_id = await dial(env)
    assert "turned off" in await audio_detail(env, call_id)

    env.settings.recording = RecordingConfig(enabled=True)
    assert "not fully configured" in await audio_detail(env, call_id)

    env.settings.recording = make_recording_config()
    assert "still in progress" in await audio_detail(env, call_id)

    await finish_call(env, call_id)
    assert "no recording" in await audio_detail(env, call_id)

    await env.recorder.record_recording(call_id, {"key": "recordings/x.ogg", "file_type": "ogg"})

    async def missing(cfg: Any, key: str, file_type: str = "ogg") -> None:
        return None

    monkeypatch.setattr("src.api.routes.convai_read.open_recording", missing)
    assert "still being processed" in await audio_detail(env, call_id)  # the call has only just ended

    async with env.sessions() as session:
        call = (await session.execute(select(Call).where(Call.id == call_id))).scalar_one()
        call.ended_at = datetime.now(UTC) - timedelta(hours=1)
        await session.commit()
    assert "missing from the bucket" in await audio_detail(env, call_id)


async def test_deploy_preflight_contract_rejects_bad_secret_and_stores_nothing(env: SimpleNamespace) -> None:
    """scripts/deploy_agent.py probes this endpoint: 401 for a wrong secret, 400 for a right one, no row created."""
    url = "/internal/calls/preflight-check/result"
    bad = await env.client.post(url, json={"transcript": "not-a-list"}, headers={"X-Posivex-Callback-Secret": "wrong"})
    assert bad.status_code == 401
    ok_secret = await env.client.post(url, json={"transcript": "not-a-list"}, headers=CALLBACK)
    assert ok_secret.status_code == 400
    assert await env.recorder.get_call("preflight-check") is None
