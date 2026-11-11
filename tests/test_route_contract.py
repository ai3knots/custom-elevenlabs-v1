"""Pins the HTTP contract that lead-gen and the agent depend on: routes, status codes and body shapes.

These tests record what the API does today so that a cleanup or refactor cannot change a route by accident.
Behavioural detail of each route is covered in test_api.py and test_leadgen_integration.py; this file only
fixes the *shape*: which routes exist, which status code each case returns, and which keys each body has.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.app import create_app
from src.config import Settings
from src.db.base import Base
from src.db.models import Call, CallStatus, WebhookOutbox
from src.persistence.recorder import CallRecorder
from src.telephony.mock import MockTelephonyProvider

KEY = {"xi-api-key": "voice-key"}
SECRET = {"X-Posivex-Callback-Secret": "cb-secret"}
DIAL_BODY = {
    "agent_id": "agent_posivex_amelia",
    "agent_phone_number_id": "phnum_posivex_trunk",
    "to_number": "+14045551234",
    "conversation_initiation_client_data": {"dynamic_variables": {"business_name": "Tony's Pizza"}},
}


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "voice_api_key": "voice-key",
        "results_callback_secret": "cb-secret",
        "telephony": {"provider": "mock", "max_concurrent_calls": 2},
        "webhook": {"lead_gen_base_url": "https://leadgen.example.com", "webhook_secret": "whsec"},
        "recording": {"enabled": False},
    }
    return Settings(**{**base, **overrides})


@pytest.fixture
async def env() -> AsyncGenerator[SimpleNamespace, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    telephony = MockTelephonyProvider()
    settings = make_settings()
    app = create_app(settings=settings, session_factory=sessions, telephony_provider=telephony)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(
            app=app, client=client, telephony=telephony, sessions=sessions, settings=settings, recorder=CallRecorder(sessions)
        )
    await engine.dispose()


async def dial(env: SimpleNamespace, path: str = "/v1/convai/sip-trunk/outbound-call", **body: Any) -> httpx.Response:
    return await env.client.post(path, headers=KEY, json={**DIAL_BODY, **body})


async def finished_call(env: SimpleNamespace, outcome: str = "callback_set") -> str:
    call_id = (await dial(env)).json()["conversation_id"]
    await env.recorder.record_transcript_batch(
        call_id,
        [
            {"role": "agent", "message": "Hi, this is Amelia.", "time_in_call_secs": 1.0},
            {"role": "user", "message": "Yes, speaking.", "time_in_call_secs": 4.0, "interrupted": True},
        ],
    )
    await env.recorder.record_event(call_id, "end_call", payload={"reason": "conversation_complete"})
    await env.recorder.finalize_call(
        call_id=call_id,
        duration_secs=42.0,
        termination_reason="closing_line_detected",
        call_outcome=outcome,
        outcome_rationale="Callback Saturday.",
        transcript_summary="Owner wants a callback.",
        total_cost=0.12,
        charging_details={"call_charge": 0.1},
        destination_url=env.settings.webhook_destination(),
        agent_id="agent_posivex_amelia",
        agent_name="Posivex LiveKit - Amelia",
    )
    return str(call_id)


# ---- the route table ---------------------------------------------------------------------------------
def test_route_table(env: SimpleNamespace) -> None:
    """Every route the service exposes. Adding or removing one must be a deliberate edit of this list."""
    paths = env.app.openapi()["paths"]
    actual = sorted((p, m.upper()) for p, ops in paths.items() for m in ops)
    assert actual == [
        ("/api/v1/calls", "POST"),
        ("/api/v1/calls/{call_id}", "GET"),
        ("/api/webhooks/elevenlabs/initiation", "POST"),
        ("/health", "GET"),
        ("/health/", "GET"),
        ("/health/live", "GET"),
        ("/health/ready", "GET"),
        ("/internal/calls/{call_id}/result", "POST"),
        ("/metrics", "GET"),
        ("/v1/convai/agents", "GET"),
        ("/v1/convai/conversations", "GET"),
        ("/v1/convai/conversations/{conversation_id}", "GET"),
        ("/v1/convai/conversations/{conversation_id}/audio", "GET"),
        ("/v1/convai/phone-numbers", "GET"),
        ("/v1/convai/sip-trunk/outbound-call", "POST"),
        ("/v1/convai/twilio/outbound-call", "POST"),
    ]


# ---- health and metrics ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/health", {"status": "healthy"}),
        ("/health/", {"status": "healthy"}),
        ("/health/live", {"status": "alive"}),
        ("/health/ready", {"status": "ready", "database": "connected"}),
    ],
)
async def test_health_bodies(env: SimpleNamespace, path: str, body: dict[str, str]) -> None:
    res = await env.client.get(path)
    assert (res.status_code, res.json()) == (200, body)


async def test_readiness_failure_is_503_with_the_error_text() -> None:
    class BrokenSession:
        async def __aenter__(self) -> Any:
            raise RuntimeError("database is down")

        async def __aexit__(self, *exc: Any) -> None:
            return None

    app = create_app(settings=make_settings(), session_factory=lambda: BrokenSession())  # type: ignore[arg-type,return-value]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/health/ready")
    assert res.status_code == 503
    assert res.json() == {"detail": {"status": "unhealthy", "database": "disconnected", "error": "database is down"}}


async def test_metrics_are_prometheus_text(env: SimpleNamespace) -> None:
    res = await env.client.get("/metrics")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    assert "posivex_calls_total" in res.text


# ---- dialing -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/v1/convai/sip-trunk/outbound-call", "/v1/convai/twilio/outbound-call"])
async def test_dial_success_shape(env: SimpleNamespace, path: str) -> None:
    res = await dial(env, path)
    assert res.status_code == 200
    body = res.json()
    assert body.keys() == {"success", "conversation_id", "callSid", "sip_call_id"}
    assert body["success"] is True
    assert body["conversation_id"].startswith("lk_")
    assert body["callSid"].startswith("CA") and body["sip_call_id"].startswith("sip_")


async def test_dial_records_the_call_and_its_events(env: SimpleNamespace) -> None:
    call_id = (await dial(env, agent_id="agent_posivex_michael")).json()["conversation_id"]
    call = await env.recorder.get_call(call_id)
    assert call is not None
    assert (call.status, call.persona, call.phone) == (CallStatus.IN_PROGRESS.value, "michael", "+14045551234")
    assert (call.metadata_json or {}).keys() >= {"agent_id", "phone_number_id", "dynamic_variables", "config_overrides"}
    assert [e.event_type for e in await env.recorder.get_events(call_id)] == ["call_requested", "call_dispatched"]
    assert env.telephony.dispatched_calls[0][1] == f"call_{call_id}"


@pytest.mark.parametrize("path", ["/v1/convai/sip-trunk/outbound-call", "/v1/convai/twilio/outbound-call"])
async def test_dial_requires_the_api_key(env: SimpleNamespace, path: str) -> None:
    missing = await env.client.post(path, json=DIAL_BODY)
    wrong = await env.client.post(path, headers={"xi-api-key": "nope"}, json=DIAL_BODY)
    assert (missing.status_code, missing.json()) == (401, {"detail": "Invalid or missing xi-api-key"})
    assert (wrong.status_code, wrong.json()) == (401, {"detail": "Invalid or missing xi-api-key"})


async def test_dial_validation_errors_are_400_not_422(env: SimpleNamespace) -> None:
    res = await env.client.post("/v1/convai/sip-trunk/outbound-call", headers=KEY, json={"to_number": "12"})
    assert res.status_code == 400
    body = res.json()
    assert body.keys() == {"success", "detail", "errors"}
    assert body["success"] is False and isinstance(body["detail"], str) and isinstance(body["errors"], list)


async def test_dial_over_the_concurrency_limit_is_429(env: SimpleNamespace) -> None:
    assert (await dial(env)).status_code == 200
    assert (await dial(env)).status_code == 200
    third = await dial(env)
    assert (third.status_code, third.json()) == (429, {"detail": "Maximum concurrent calls limit reached"})


async def test_dial_failure_is_500_and_marks_the_call_failed(env: SimpleNamespace) -> None:
    env.telephony.should_fail = True
    env.telephony.failure_message = "trunk rejected"
    res = await dial(env)
    assert (res.status_code, res.json()) == (500, {"detail": "Outbound dial failure: trunk rejected"})
    async with env.sessions() as session:
        call = (await session.execute(select(Call))).scalar_one()
    assert (call.status, call.termination_reason) == (CallStatus.FAILED.value, "dial_failed")
    assert [e.event_type for e in await env.recorder.get_events(call.id)] == ["call_requested", "dial_failed"]


# ---- lead-gen reads ----------------------------------------------------------------------------------
async def test_read_routes_require_the_api_key(env: SimpleNamespace) -> None:
    call_id = await finished_call(env)
    for path in (
        "/v1/convai/agents",
        "/v1/convai/phone-numbers",
        "/v1/convai/conversations",
        f"/v1/convai/conversations/{call_id}",
        f"/v1/convai/conversations/{call_id}/audio",
    ):
        res = await env.client.get(path)
        assert (res.status_code, res.json()) == (401, {"detail": "Invalid or missing xi-api-key"}), path


async def test_agents_and_phone_numbers_shapes(env: SimpleNamespace) -> None:
    agents = (await env.client.get("/v1/convai/agents", headers=KEY)).json()
    assert agents == {
        "agents": [
            {"agent_id": "agent_posivex_amelia", "name": "Posivex LiveKit - Amelia"},
            {"agent_id": "agent_posivex_michael", "name": "Posivex LiveKit - Michael"},
        ],
        "has_more": False,
        "next_cursor": None,
    }
    numbers = (await env.client.get("/v1/convai/phone-numbers", headers=KEY)).json()
    assert numbers == [
        {
            "phone_number_id": "phnum_posivex_trunk",
            "phone_number": "",
            "label": "Posivex LiveKit number",
            "provider": "sip_trunk",
            "supports_outbound": True,
            "supports_inbound": False,
        }
    ]


async def test_conversation_list_shape(env: SimpleNamespace) -> None:
    call_id = await finished_call(env)
    res = await env.client.get("/v1/convai/conversations", headers=KEY)
    assert res.status_code == 200
    body = res.json()
    assert body.keys() == {"conversations", "has_more", "next_cursor"}
    assert (body["has_more"], body["next_cursor"]) == (False, None)
    item = body["conversations"][0]
    assert item.keys() == {
        "conversation_id", "agent_id", "agent_name", "start_time_unix_secs", "call_duration_secs", "status", "direction"
    }
    assert (item["conversation_id"], item["status"], item["direction"], item["call_duration_secs"]) == (
        call_id, "done", "outbound", 42
    )
    assert (await env.client.get("/v1/convai/conversations?page_size=0", headers=KEY)).status_code == 400


async def test_conversation_detail_shape_for_a_finished_call(env: SimpleNamespace) -> None:
    call_id = await finished_call(env)
    res = await env.client.get(f"/v1/convai/conversations/{call_id}", headers=KEY)
    assert res.status_code == 200
    data = res.json()
    assert data.keys() == {
        "agent_id", "agent_name", "conversation_id", "status", "transcript", "metadata", "analysis",
        "conversation_initiation_client_data", "has_audio", "has_user_audio", "has_response_audio",
    }
    assert (data["conversation_id"], data["status"], data["agent_id"]) == (call_id, "done", "agent_posivex_amelia")
    assert data["metadata"].keys() == {
        "start_time_unix_secs", "call_duration_secs", "cost", "termination_reason", "charging", "phone_call"
    }
    assert data["metadata"]["phone_call"].keys() == {"direction", "phone_number_id", "type", "external_number", "call_sid"}
    assert data["analysis"].keys() == {
        "call_successful", "transcript_summary", "data_collection_results", "evaluation_criteria_results"
    }
    assert data["analysis"]["call_successful"] == "success"
    assert data["analysis"]["data_collection_results"]["call_outcome"] == {
        "data_collection_id": "call_outcome", "value": "callback_set", "rationale": "Callback Saturday."
    }
    assert data["conversation_initiation_client_data"] == {
        "dynamic_variables": {
            "business_name": "Tony's Pizza",
            "business_context": "",
            "current_datetime": "",
            "is_followup": "false",
            "followup_notes": "",
            "contact_name": "",
        }
    }
    turns = data["transcript"]
    assert sorted(t["role"] for t in turns) == ["agent", "agent", "user"]  # spoken turns plus the end_call tool turn
    spoken = next(t for t in turns if t["message"] == "Hi, this is Amelia.")
    tool = next(t for t in turns if t["tool_calls"])
    assert spoken.keys() == {"role", "message", "time_in_call_secs", "interrupted", "tool_calls", "tool_results"}
    assert tool["tool_calls"][0].keys() == {"request_id", "tool_name", "params_as_json", "tool_has_been_called"}
    assert tool["tool_calls"][0]["tool_name"] == "end_call"
    assert tool["tool_results"][0].keys() == {"request_id", "tool_name", "result_value", "is_error"}
    assert data["has_audio"] is False


async def test_conversation_in_progress_has_no_analysis(env: SimpleNamespace) -> None:
    call_id = (await dial(env)).json()["conversation_id"]
    data = (await env.client.get(f"/v1/convai/conversations/{call_id}", headers=KEY)).json()
    assert (data["status"], data["analysis"]) == ("in-progress", {})


async def test_unknown_conversation_is_404(env: SimpleNamespace) -> None:
    res = await env.client.get("/v1/convai/conversations/lk_missing", headers=KEY)
    assert (res.status_code, res.json()) == (404, {"detail": "Conversation 'lk_missing' not found"})
    audio = await env.client.get("/v1/convai/conversations/lk_missing/audio", headers=KEY)
    assert audio.status_code == 404


async def test_audio_404_explains_why_there_is_nothing_to_play(env: SimpleNamespace) -> None:
    call_id = await finished_call(env)
    res = await env.client.get(f"/v1/convai/conversations/{call_id}/audio", headers=KEY)
    assert res.status_code == 404
    assert res.json() == {"detail": "Recording is turned off on the voice service (RECORDING_ENABLED is not true)."}


async def test_inbound_initiation_lookup_shape(env: SimpleNamespace) -> None:
    res = await env.client.post("/api/webhooks/elevenlabs/initiation", json={"caller_id": "+14045559999"})
    assert res.status_code == 200
    body = res.json()
    assert body.keys() == {"type", "dynamic_variables"}
    assert body["type"] == "conversation_initiation_client_data"
    assert body["dynamic_variables"].keys() == {
        "call_direction", "caller_status", "caller_phone", "business_name", "business_context",
        "current_datetime", "is_followup", "followup_notes", "contact_name",
    }
    assert (await env.client.post("/api/webhooks/elevenlabs/initiation", json={})).status_code == 400


# ---- the agent's report ------------------------------------------------------------------------------
RESULT = {
    "call": {"phone": "+14045551234", "persona": "amelia", "language": "en"},
    "transcript": [{"role": "agent", "message": "Hi.", "time_in_call_secs": 1.0, "interrupted": False}],
    "usage": [
        {"provider": "livekit", "model": "sip_webrtc", "metric_type": "audio_seconds", "metric_value": 5.0, "estimated_cost_usd": 0.0}
    ],
    "events": [{"event_type": "timing_pickup", "state": None, "payload": {"since_pickup_secs": 0.0}, "timestamp": 1_700_000_000.0}],
    "recording": None,
    "finalize": {
        "duration_secs": 5.0, "termination_reason": "session_closed", "call_outcome": "hangup_no_speak",
        "outcome_rationale": "x", "transcript_summary": "", "total_cost": 0.01, "charging_details": {}, "agent_id": None,
        "agent_name": None,
    },
}


async def test_results_callback_authentication(env: SimpleNamespace) -> None:
    url = "/internal/calls/lk_r1/result"
    assert (await env.client.post(url, json=RESULT)).status_code == 401
    wrong = await env.client.post(url, headers={"X-Posivex-Callback-Secret": "nope"}, json=RESULT)
    assert (wrong.status_code, wrong.json()) == (401, {"detail": "Invalid callback secret"})

    app = create_app(settings=make_settings(results_callback_secret=""), session_factory=env.sessions)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(url, headers=SECRET, json=RESULT)
    assert res.status_code == 503
    assert res.json() == {"detail": "Results callback is not configured (RESULTS_CALLBACK_SECRET)"}


async def test_results_callback_stores_once_and_stages_the_webhook(env: SimpleNamespace) -> None:
    url = "/internal/calls/lk_r2/result"
    first = await env.client.post(url, headers=SECRET, json=RESULT)
    assert (first.status_code, first.json()) == (200, {"ok": True, "duplicate": False})
    again = await env.client.post(url, headers=SECRET, json=RESULT)
    assert (again.status_code, again.json()) == (200, {"ok": True, "duplicate": True})

    call = await env.recorder.get_call("lk_r2")
    assert call is not None and call.status == CallStatus.COMPLETED.value and call.call_outcome == "hangup_no_speak"
    async with env.sessions() as session:
        outbox = (await session.execute(select(WebhookOutbox))).scalars().all()
    assert len(outbox) == 1
    row = outbox[0]
    assert (row.status, row.event_type, row.destination_url) == (
        "pending", "post_call_transcription", "https://leadgen.example.com/api/webhooks/elevenlabs"
    )
    assert row.payload.keys() == {"type", "event_timestamp", "data"} and row.payload["type"] == "post_call_transcription"
    assert row.payload["data"]["conversation_id"] == "lk_r2"


async def test_results_callback_rejects_a_malformed_body_with_400(env: SimpleNamespace) -> None:
    res = await env.client.post("/internal/calls/lk_r3/result", headers=SECRET, json={"transcript": "not a list"})
    assert res.status_code == 400
    assert res.json().keys() == {"success", "detail", "errors"}
