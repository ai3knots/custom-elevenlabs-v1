"""Live transfer to the team line: the tool, its guards, the failure paths, the briefing, and the whole call."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from livekit import api
from livekit.agents.llm import ToolError

from src.agent import transfer as transfer_module
from src.agent.persona_agent import CallState
from src.agent.transfer import CallTransferrer, TransferBrief, classify_failure
from src.config import CallRequest, DynamicVariables, Settings, TransferConfig
from src.webhook.payload import build_conversation_data
from tests.support.fake_call import FakeLiveKitApi, FakeSpeechHandle, settle, sip_error

IDENTITY = "phone_+14045550000"
CONFIG = TransferConfig(enabled=True, briefing_url="https://leadgen.test/briefing", briefing_secret="s3cret")


class Rig:
    """A CallTransferrer with fakes around it."""

    def __init__(
        self, *outcomes: Any, config: TransferConfig = CONFIG, phone: bool = True, identity: str | None = IDENTITY
    ) -> None:
        self.api = FakeLiveKitApi(*outcomes)
        self.state = CallState()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.ended: list[tuple[str, bool]] = []
        self.ending = False
        self.said: list[str] = []
        self.context = SimpleNamespace(session=SimpleNamespace(say=self._say))
        request = CallRequest(
            call_id="lk_test",
            phone="+14045550000",
            dynamic_variables=DynamicVariables(business_name="Apex Pizza", contact_name="Tony"),
        )
        self.transferrer = CallTransferrer(
            ctx=SimpleNamespace(room=SimpleNamespace(name="call_lk_test"), api=self.api),
            call_request=request,
            config=config,
            call_state=self.state,
            is_phone_call=lambda: phone,
            callee_identity=lambda: identity,
            call_ending=lambda: self.ending,
            end_call=lambda reason, drain: self.ended.append((reason, drain)),
            emit=self._emit,
        )

    def _say(self, text: str, **_: Any) -> FakeSpeechHandle:
        self.said.append(text)
        return FakeSpeechHandle()

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))

    async def transfer(self, **brief: str) -> None:
        await self.transferrer.transfer(self.context, TransferBrief(**brief))  # type: ignore[arg-type]

    def names(self) -> list[str]:
        return [n for n, _ in self.events]


@pytest.fixture
def briefings(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Lead-gen's team-briefing webhook, answered with 200 and recorded."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        transfer_module.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    return seen


# ---- a transfer that connects -------------------------------------------------------------------------
async def test_a_connected_transfer_refers_the_caller_to_the_team_line_and_ends_the_call(
    briefings: list[httpx.Request],
) -> None:
    rig = Rig(None)
    await rig.transfer(summary="Uses Square, wants lower rates.", prospect_name="Tony", prospect_email="tony@apex.com")

    request = rig.api.sip.transfers[0]
    assert request.room_name == "call_lk_test" and request.participant_identity == IDENTITY
    assert request.transfer_to == "tel:+12292028500"
    assert request.play_dialtone is False
    assert request.ringing_timeout.seconds == 30
    assert rig.said == [transfer_module.HANDOVER_LINE]
    assert rig.state.transferred is True and rig.state.transferring is False and rig.state.end_reason == "transferred"
    assert rig.names() == ["call_transferred"]
    assert rig.events[0][1]["result"] == "Call was transferred to number"
    assert rig.ended == [("transferred", False)]


async def test_a_demo_now_transfer_says_the_demo_line_and_a_person_request_says_the_person_line() -> None:
    demo = Rig(None)
    await demo.transfer(summary="Said yes to the demo now.", demo=True)  # type: ignore[arg-type]
    assert demo.said == [transfer_module.DEMO_HANDOVER_LINE]
    assert demo.events[0][1]["demo"] is True

    person = Rig(None)
    await person.transfer(summary="Asked for a human.")
    assert person.said == [transfer_module.HANDOVER_LINE]
    assert person.events[0][1]["demo"] is False
    assert transfer_module.DEMO_HANDOVER_LINE != transfer_module.HANDOVER_LINE


async def test_the_team_is_briefed_before_the_caller_is_connected(briefings: list[httpx.Request]) -> None:
    rig = Rig(None)
    await rig.transfer(
        summary="Uses Square.", prospect_name="Tony", prospect_email="tony@apex.com", callback_number="+14045559999"
    )

    assert len(briefings) == 1
    request = briefings[0]
    assert str(request.url) == "https://leadgen.test/briefing"
    assert request.headers["x-agent-tool-secret"] == "s3cret"
    body = json.loads(request.content)
    assert body["intent"] == "transfer_now" and body["conversation_id"] == "lk_test"
    assert body["business_name"] == "Apex Pizza" and body["prospect_phone"] == "+14045550000"
    assert body["prospect_name"] == "Tony" and body["prospect_email"] == "tony@apex.com"
    assert body["callback_number"] == "+14045559999" and body["summary"] == "Uses Square."


async def test_the_briefing_never_blocks_or_fails_a_transfer(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**_: Any) -> Any:
        raise httpx.ConnectError("lead-gen is down")

    monkeypatch.setattr(transfer_module.httpx, "AsyncClient", boom)
    rig = Rig(None)
    await rig.transfer(summary="x")
    assert rig.state.transferred is True and rig.ended == [("transferred", False)]


async def test_a_refused_briefing_is_logged_and_the_transfer_still_connects(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        transfer_module.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(401)), **kw),
    )
    rig = Rig(None)
    with caplog.at_level(logging.WARNING, logger="posivex.session"):
        await rig.transfer()
    assert rig.state.transferred is True
    assert "refused (HTTP 401)" in caplog.text and "s3cret" not in caplog.text


async def test_without_a_briefing_address_the_transfer_goes_ahead() -> None:
    rig = Rig(None, config=TransferConfig(enabled=True))
    await rig.transfer()
    assert rig.state.transferred is True


# ---- a transfer that does not connect -----------------------------------------------------------------
@pytest.mark.parametrize(
    ("error", "kind", "text"),
    [
        (sip_error("STR_RINGING_TIMEOUT"), "no_answer", "Nobody on our team line answered"),
        (sip_error("STR_REJECTED", status_code=486), "rejected", "busy or declined"),
        (sip_error("STR_CALL_ENDED"), "call_ended", "ended during the transfer"),
        (api.ServerError("canceled", "request timed out", status=408, metadata={}), "no_answer", "Nobody on our team"),
        (TimeoutError(), "no_answer", "Nobody on our team line answered"),
        (RuntimeError("boom"), "failed", "could not be completed"),
    ],
)
async def test_a_transfer_that_does_not_connect_tells_the_model_why_and_the_call_goes_on(
    error: BaseException, kind: str, text: str
) -> None:
    rig = Rig(error)
    with pytest.raises(ToolError) as caught:
        await rig.transfer()
    assert text in str(caught.value) and "give our team line or offer to have someone call them back" in str(caught.value)
    assert classify_failure(error).kind == kind
    assert rig.state.transferred is False and rig.state.transferring is False
    assert rig.state.is_holding is False  # the silence timers run again
    assert rig.ended == []  # the call continues with the caller
    assert rig.names() == ["transfer_failed"]
    assert rig.events[0][1]["reason"] == kind and rig.events[0][1]["error"] is True


async def test_a_second_attempt_is_allowed_after_a_failure_and_a_third_is_refused() -> None:
    rig = Rig(sip_error("STR_RINGING_TIMEOUT"), sip_error("STR_RINGING_TIMEOUT"))
    for _ in range(2):
        with pytest.raises(ToolError):
            await rig.transfer()
    with pytest.raises(ToolError, match="already tried 2 times"):
        await rig.transfer()
    assert len(rig.api.sip.transfers) == 2


async def test_a_retry_that_connects_after_a_failure_ends_the_call_transferred() -> None:
    rig = Rig(sip_error("STR_RINGING_TIMEOUT"), None)
    with pytest.raises(ToolError):
        await rig.transfer()
    await rig.transfer()
    assert rig.state.transferred is True and rig.names() == ["transfer_failed", "call_transferred"]


# ---- guards -------------------------------------------------------------------------------------------
async def test_a_browser_test_call_cannot_be_transferred() -> None:
    with pytest.raises(ToolError, match="not a phone call"):
        await Rig(phone=False).transfer()


@pytest.mark.parametrize("verdict", ["voicemail", "menu"])
async def test_a_machine_is_never_transferred(verdict: str) -> None:
    rig = Rig()
    rig.state.greeting_verdict = verdict
    with pytest.raises(ToolError, match="machine"):
        await rig.transfer()
    assert rig.api.sip.transfers == []


async def test_a_call_that_is_ending_or_already_transferred_is_not_transferred_again() -> None:
    rig = Rig()
    rig.ending = True
    with pytest.raises(ToolError, match="already ending"):
        await rig.transfer()
    rig.ending = False
    rig.state.transferred = True
    with pytest.raises(ToolError, match="already in progress or done"):
        await rig.transfer()
    assert rig.api.sip.transfers == []


async def test_an_unknown_caller_identity_is_refused() -> None:
    with pytest.raises(ToolError, match="cannot be found"):
        await Rig(identity=None).transfer()


async def test_an_unclear_greeting_verdict_does_not_stop_a_transfer() -> None:
    rig = Rig(None)
    rig.state.greeting_verdict = "unclear"  # the check has not made up its mind; the owner asked for a person
    await rig.transfer()
    assert rig.state.transferred is True


# ---- the caller's session closing while the transfer is in flight -------------------------------------
async def test_a_successful_transfer_is_still_recorded_when_the_tool_is_cancelled_by_the_session_closing() -> None:
    """LiveKit closes the caller's session as the REFER succeeds; the tool can be cancelled, the transfer still counts."""
    release = asyncio.Event()
    rig = Rig(release)
    tool = asyncio.ensure_future(rig.transfer())
    await settle()
    assert rig.state.transferring is True and rig.state.is_holding is True  # on hold while the line rings
    tool.cancel()
    await settle()
    release.set()
    await rig.transferrer.settle()  # what the finalizer does before it reports the call
    assert rig.state.transferred is True and rig.names() == ["call_transferred"]


# ---- the settings -------------------------------------------------------------------------------------
def test_transfers_are_off_by_default_and_point_at_the_team_line() -> None:
    config = Settings().transfer
    assert config.enabled is False and config.destination == "+12292028500"


def test_an_enabled_transfer_needs_a_real_phone_number_and_a_media_timeout_that_outlasts_the_ringing() -> None:
    with pytest.raises(ValueError, match="E.164"):
        TransferConfig(enabled=True, destination="229-202-8500")
    with pytest.raises(ValueError, match="media_timeout"):
        TransferConfig(ringing_timeout_seconds=30, media_timeout_seconds=30)


def test_the_destination_cannot_be_changed_by_a_dial_request() -> None:
    from src.config.call_request import ALLOWED_OVERRIDE_SECTIONS

    assert "transfer" not in ALLOWED_OVERRIDE_SECTIONS
    with pytest.raises(ValueError):
        CallRequest(phone="+14045550000", config_overrides={"transfer": {"destination": "+19995550000"}})


def test_the_environment_switches_the_transfer_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.config import load_settings

    monkeypatch.setenv("TRANSFER_ENABLED", "true")
    monkeypatch.setenv("TEAM_BRIEFING_URL", "https://leadgen.test/briefing")
    monkeypatch.setenv("TEAM_BRIEFING_SECRET", "abc")
    config = load_settings().transfer
    assert config.enabled is True and config.briefing_url.endswith("/briefing") and config.briefing_secret == "abc"


# ---- how the call is reported -------------------------------------------------------------------------
def test_a_transfer_appears_to_lead_gen_as_a_transfer_to_number_tool_call() -> None:
    """Lead-gen's `isTransferTool` matches `transfer_to_*`; its result carries the text lead-gen reads."""
    call = SimpleNamespace(
        started_at=None,
        status="completed",
        transcript_summary="",
        call_outcome="consultant_call_scheduled",
        termination_reason="transferred",
        phone="+14045550000",
        metadata_json={},
        duration_secs=60,
        total_cost=0.1,
        charging_details={},
        id="lk_test",
        summary=None,
        outcome_rationale=None,
        ended_at=None,
        language="en",
        persona="amelia",
    )
    events = [
        SimpleNamespace(
            id=1, event_type="transfer_failed", timestamp=None, payload={"result": "Transfer failed: nobody answered", "error": True}
        ),
        SimpleNamespace(id=2, event_type="call_transferred", timestamp=None, payload={"result": "Call was transferred to number"}),
    ]
    data = build_conversation_data(call, [], events)  # type: ignore[arg-type]
    tools = [(t["tool_calls"][0]["tool_name"], t["tool_results"][0]) for t in data["transcript"] if t["tool_calls"]]
    assert [name for name, _ in tools] == ["transfer_to_number", "transfer_to_number"]
    assert tools[0][1]["is_error"] is True and tools[0][1]["result_value"].startswith("Transfer failed")
    assert tools[1][1]["is_error"] is False and tools[1][1]["result_value"] == "Call was transferred to number"
    assert data["metadata"]["termination_reason"] == "transferred"
