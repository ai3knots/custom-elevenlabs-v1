"""Start-up checks, the dev lifespan (telephony client leak) and the recording-bucket check."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi.testclient import TestClient

from src.analysis.outcome import CallSignals, classify_transcript
from src.config import RecordingConfig, Settings
from src.main import build_dev_lifespan, run_startup_checks
from src.startup_checks import StartupCheckError, check_recording_storage, check_webhook_config
from src.storage import verify_bucket_access
from src.telephony.mock import MockTelephonyProvider


def settings_with(url: str, secret: str, recording: RecordingConfig | None = None) -> Settings:
    return Settings(
        webhook={"lead_gen_base_url": url, "webhook_secret": secret},  # type: ignore[arg-type]
        recording=recording or RecordingConfig(),
    )


def recording(**overrides: Any) -> RecordingConfig:
    base: dict[str, Any] = {"enabled": True, "bucket": "calls", "access_key": "k", "secret": "s", "region": "us-east-1"}
    return RecordingConfig(**{**base, **overrides})


# ---- webhook secret -------------------------------------------------------------------------------------------
def test_default_secret_towards_a_real_lead_gen_is_refused() -> None:
    with pytest.raises(StartupCheckError, match="ELEVENLABS_WEBHOOK_SECRET"):
        check_webhook_config(settings_with("https://xyz.example.com/api/webhooks/elevenlabs", "dev_webhook_secret"))


def test_default_secret_is_fine_for_a_local_lead_gen_and_a_real_secret_is_fine_anywhere() -> None:
    check_webhook_config(settings_with("http://localhost:3000", "dev_webhook_secret"))
    check_webhook_config(settings_with("http://127.0.0.1:3005/api/webhooks/elevenlabs", "dev_webhook_secret"))
    check_webhook_config(settings_with("https://xyz.example.com", "b6f197-real"))


def test_run_startup_checks_exits_on_a_bad_secret_only_when_asked() -> None:
    bad = settings_with("https://xyz.example.com", "dev_webhook_secret")
    with pytest.raises(SystemExit):
        run_startup_checks(bad, webhook=True)
    run_startup_checks(bad, webhook=False)  # the agent never sends webhooks


# ---- recording bucket -----------------------------------------------------------------------------------------
class FakeS3:
    def __init__(self, error: Exception | None = None) -> None:
        self.error, self.calls = error, []

    def put_object(self, **kw: Any) -> None:
        self.calls.append(("put", kw["Key"]))
        if self.error:
            raise self.error

    def delete_object(self, **kw: Any) -> None:
        self.calls.append(("delete", kw["Key"]))


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "nope"}}, "PutObject")


def test_bucket_check_writes_and_removes_one_object(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeS3()
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    assert verify_bucket_access(recording()) is None
    assert fake.calls == [("put", "recordings/.startup-check"), ("delete", "recordings/.startup-check")]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (client_error("NoSuchBucket"), "does not exist"),
        (client_error("AccessDenied"), "not allowed"),
        (client_error("InvalidAccessKeyId"), "RECORDING_ACCESS_KEY"),
        (client_error("SignatureDoesNotMatch"), "RECORDING_SECRET"),
        (client_error("PermanentRedirect"), "RECORDING_REGION"),
        (EndpointConnectionError(endpoint_url="https://nowhere"), "could not reach"),
    ],
)
def test_bucket_check_names_the_setting_to_fix(monkeypatch: pytest.MonkeyPatch, error: Exception, expected: str) -> None:
    monkeypatch.setattr("boto3.client", lambda *a, **k: FakeS3(error))
    assert expected in (verify_bucket_access(recording()) or "")


def test_recording_check_is_silent_when_off_and_flags_missing_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Any] = []
    monkeypatch.setattr("src.storage.verify_bucket_access", lambda cfg: called.append(cfg))
    assert check_recording_storage(settings_with("http://localhost", "x", RecordingConfig())) is None
    assert "missing" in (check_recording_storage(settings_with("http://localhost", "x", recording(secret=""))) or "")
    assert called == []  # nothing to test against until it is configured
    monkeypatch.setattr("src.storage.verify_bucket_access", lambda cfg: "AccessDenied: no")
    assert check_recording_storage(settings_with("http://localhost", "x", recording())) == "AccessDenied: no"


# ---- dev lifespan keeps the shared telephony provider ---------------------------------------------------------
def test_dev_lifespan_creates_and_closes_the_shared_telephony_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from src.api.app import create_app

    closed: list[bool] = []

    class Provider(MockTelephonyProvider):
        async def close(self) -> None:
            closed.append(True)

    ran: list[str] = []

    class Worker:
        async def run(self, poll_interval_secs: float, stop_event: asyncio.Event) -> None:
            ran.append("started")
            await stop_event.wait()
            ran.append("stopped")

    class Engine:
        async def dispose(self) -> None:
            ran.append("disposed")

    # `src.api.app` is shadowed by the FastAPI instance of the same name, so patch it through sys.modules.
    monkeypatch.setattr(sys.modules["src.api.app"], "get_telephony_provider", lambda settings: Provider())
    app = create_app(settings=Settings(), session_factory=object())  # type: ignore[arg-type]
    app.router.lifespan_context = build_dev_lifespan(app, Worker(), Engine(), 1.0)  # type: ignore[arg-type]
    with TestClient(app):
        assert isinstance(app.state.telephony_provider, Provider)  # the app's own lifespan still ran
    assert closed == [True]
    assert ran == ["started", "stopped", "disposed"]


async def test_telephony_dependency_keeps_one_provider_when_none_exists() -> None:
    from fastapi import FastAPI

    from src.api.deps import get_telephony

    app = FastAPI()
    request = SimpleNamespace(app=app)
    settings = Settings(telephony={"provider": "mock"})  # type: ignore[arg-type]
    first = get_telephony(request, settings)  # type: ignore[arg-type]
    assert get_telephony(request, settings) is first  # type: ignore[arg-type]


# ---- classifier client is built off the event loop -------------------------------------------------------------
async def test_classifier_builds_its_client_in_a_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    seen: list[str] = []
    main = threading.current_thread().name

    class Models:
        async def generate_content(self, **kw: Any) -> Any:
            return SimpleNamespace(text='{"call_outcome": "declined", "rationale": "r", "summary": "s"}')

    def fake_client(api_key: str) -> Any:
        seen.append(threading.current_thread().name)
        return SimpleNamespace(aio=SimpleNamespace(models=Models()))

    monkeypatch.setattr("src.analysis.outcome._make_client", fake_client)
    result = await classify_transcript(
        [{"role": "user", "message": "no thanks"}], CallSignals(), api_key="k", model="m"
    )
    assert result.outcome == "declined"
    assert seen and seen[0] != main
