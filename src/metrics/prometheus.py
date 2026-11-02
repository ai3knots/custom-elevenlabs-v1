"""Prometheus metrics collectors for Posivex Voice Agent operations."""

from __future__ import annotations

import prometheus_client
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram

# Metrics collectors
POSIVEX_CALLS_TOTAL = Counter(
    "posivex_calls_total",
    "Total phone calls initiated through native or emulated endpoints",
    ["persona", "language", "status"],
)

POSIVEX_CALL_OUTCOMES_TOTAL = Counter(
    "posivex_call_outcomes_total",
    "Total calls categorized by final outcome classification",
    ["persona", "outcome"],
)

POSIVEX_CALL_DURATION_SECONDS = Histogram(
    "posivex_call_duration_seconds",
    "Call duration in seconds",
    ["persona"],
    buckets=[5, 10, 20, 30, 60, 120, 180, 300, 600, 900],
)

POSIVEX_COST_DOLLARS_TOTAL = Counter(
    "posivex_cost_dollars_total",
    "Aggregated estimated provider costs in USD",
    ["provider", "metric_type"],
)

POSIVEX_WEBHOOK_DELIVERIES_TOTAL = Counter(
    "posivex_webhook_deliveries_total",
    "Total transactional webhook outbox delivery attempts",
    ["status"],
)

POSIVEX_ACTIVE_CALLS = Gauge(
    "posivex_active_calls",
    "Current number of active calls in progress",
)


def get_latest_metrics() -> tuple[bytes, str]:
    """Generate Prometheus exposition text and content type."""
    return prometheus_client.generate_latest(), CONTENT_TYPE_LATEST
