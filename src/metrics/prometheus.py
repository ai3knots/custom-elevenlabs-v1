"""Prometheus metrics collectors for Posivex Voice Agent operations."""

from __future__ import annotations

import prometheus_client
from prometheus_client import CONTENT_TYPE_LATEST, Counter

POSIVEX_CALLS_TOTAL = Counter(
    "posivex_calls_total",
    "Total phone calls initiated through the ElevenLabs-compatible dial endpoint",
    ["persona", "language", "status"],
)

POSIVEX_WEBHOOK_DELIVERIES_TOTAL = Counter(
    "posivex_webhook_deliveries_total",
    "Total transactional webhook outbox delivery attempts",
    ["status"],
)


def get_latest_metrics() -> tuple[bytes, str]:
    """Generate Prometheus exposition text and content type."""
    return prometheus_client.generate_latest(), CONTENT_TYPE_LATEST
