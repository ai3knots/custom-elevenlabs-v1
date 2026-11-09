"""LLM usage as recorded per call: cached tokens are kept so the prompt-cache hit rate can be read back."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from livekit.agents.metrics.usage import LLMModelUsage

from src.agent.session_handler import _usage_into_costs
from src.metrics.costs import CostAccumulator


def llm_usage(**overrides: int) -> LLMModelUsage:
    base = {"provider": "google", "model": "gemini-3.5-flash", "input_tokens": 1000, "output_tokens": 50}
    return LLMModelUsage(**{**base, **overrides})  # type: ignore[arg-type]


def test_cached_tokens_get_their_own_row_without_changing_the_cost() -> None:
    plain, cached = CostAccumulator(), CostAccumulator()
    plain.add_llm_usage("gemini-3.5-flash", 1000, 50)
    cached.add_llm_usage("gemini-3.5-flash", 1000, 50, cached_tokens=600)
    assert cached.llm_cost == plain.llm_cost
    row = [r for r in cached.records if r["metric_type"] == "tokens_input_cached"]
    assert row and row[0]["metric_value"] == 600.0 and row[0]["estimated_cost_usd"] == 0.0
    assert not [r for r in plain.records if r["metric_type"] == "tokens_input_cached"]


def test_session_usage_logs_the_cache_hit_rate(caplog) -> None:  # type: ignore[no-untyped-def]
    session = SimpleNamespace(usage=SimpleNamespace(model_usage=[llm_usage(input_cached_tokens=600)]))
    costs = CostAccumulator()
    with caplog.at_level(logging.INFO, logger="posivex.session"):
        _usage_into_costs(session, costs, duration_secs=30)  # type: ignore[arg-type]
    assert any("cached=600 (60%)" in m for m in caplog.messages)
    assert any(r["metric_type"] == "tokens_input_cached" for r in costs.records)
