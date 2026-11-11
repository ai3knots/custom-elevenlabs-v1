"""Pins the helpers that replaced duplicated code, so the unified version keeps the behaviour of the copies."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.config import LLMConfig, RecordingConfig
from src.db.models import as_utc
from src.providers.llm import (
    create_fallback_llm,
    get_llm,
    share_thought_signatures,
    uses_thinking_budget,
)
from src.storage import PROBE_CONNECT_TIMEOUT_SECS, PROBE_READ_TIMEOUT_SECS, _client


# ---- one S3 client builder -------------------------------------------------------------------------------
def _cfg(**kw: object) -> RecordingConfig:
    base = {"enabled": True, "bucket": "calls", "access_key": "key", "secret": "secret", "region": "us-east-1"}
    return RecordingConfig(**{**base, **kw})  # type: ignore[arg-type]


def test_s3_client_for_recordings_uses_the_configured_endpoint_and_style() -> None:
    client = _client(_cfg(endpoint="https://r2.example.com", force_path_style=True))
    assert client.meta.endpoint_url == "https://r2.example.com"
    assert client.meta.config.s3 == {"addressing_style": "path"}
    assert _client(_cfg()).meta.config.s3 == {"addressing_style": "auto"}


def test_s3_probe_client_has_short_timeouts_and_one_retry() -> None:
    probe = _client(_cfg(), probe=True).meta.config
    assert (probe.connect_timeout, probe.read_timeout) == (PROBE_CONNECT_TIMEOUT_SECS, PROBE_READ_TIMEOUT_SECS) == (5, 10)
    assert probe.retries["total_max_attempts"] == 2  # botocore counts max_attempts=1 as one retry, so two tries
    normal = _client(_cfg()).meta.config
    assert (normal.connect_timeout, normal.read_timeout) != (5, 10)  # botocore defaults (60 s)


def test_the_s3_client_is_built_once_per_credentials_and_the_probe_client_is_never_reused() -> None:
    assert _client(_cfg()) is _client(_cfg())
    assert _client(_cfg(access_key="other")) is not _client(_cfg())
    assert _client(_cfg(), probe=True) is not _client(_cfg(), probe=True)
    assert _client(_cfg(), probe=True) is not _client(_cfg())


# ---- one _utc ---------------------------------------------------------------------------------------------
def test_as_utc_treats_naive_times_as_utc_and_keeps_aware_ones() -> None:
    naive = datetime(2026, 11, 10, 12, 0, 0)
    assert as_utc(naive) == datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)
    aware = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone(timedelta(hours=5)))
    assert as_utc(aware) is aware


# ---- one thought_signatures setup --------------------------------------------------------------------------
def test_the_real_gemini_clients_end_up_sharing_one_signature_dict() -> None:
    """Also proves the private attribute still exists in the pinned LiveKit Google plugin."""
    cfg = LLMConfig()
    primary = get_llm("gemini-3.5-flash", cfg, api_key="test-key")
    secondary = get_llm("gemini-3.5-flash-lite", cfg, api_key="test-key")
    assert primary._thought_signatures is not secondary._thought_signatures  # type: ignore[attr-defined]
    primary._thought_signatures["call-1"] = b"sig"  # type: ignore[attr-defined]

    create_fallback_llm(primary, secondary, cfg)

    assert primary._thought_signatures is secondary._thought_signatures  # type: ignore[attr-defined]
    assert secondary._thought_signatures == {"call-1": b"sig"}  # type: ignore[attr-defined]


def test_share_thought_signatures_merges_both_sides_and_ignores_llms_without_them() -> None:
    a = SimpleNamespace(_thought_signatures={"a": b"1"})
    b = SimpleNamespace(_thought_signatures={"b": b"2"})
    share_thought_signatures(a, b)  # type: ignore[arg-type]
    assert a._thought_signatures is b._thought_signatures
    assert a._thought_signatures == {"a": b"1", "b": b"2"}

    plain = SimpleNamespace()
    share_thought_signatures(a, plain)  # type: ignore[arg-type]
    assert not hasattr(plain, "_thought_signatures")


# ---- model-family markers ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("model", "budget"),
    [("gemini-2.5-flash", True), ("gemini-2.0-flash", True), ("gemini-3.5-flash", False), ("gemini-3.5-flash-lite", False)],
)
def test_gemini_2x_models_use_a_thinking_budget(model: str, budget: bool) -> None:
    assert uses_thinking_budget(model) is budget


def test_greeting_check_thinking_config_follows_the_same_rule() -> None:
    from src.agent.greeting_detector import _thinking_config

    assert _thinking_config("gemini-2.5-flash-lite").thinking_budget == 0
    assert str(_thinking_config("gemini-3.5-flash-lite").thinking_level).endswith("MINIMAL")
