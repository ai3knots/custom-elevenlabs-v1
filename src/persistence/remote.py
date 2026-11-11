"""Recorder for an agent that cannot reach the database (for example one hosted on LiveKit Cloud).

It exposes the same methods the session handler calls on `CallRecorder`, but only keeps the data in
memory during the call. `finalize_call` sends everything to the API in one request
(`POST /internal/calls/{id}/result`), which stores it and stages the webhook for lead-gen.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("posivex.persistence.remote")

POST_TIMEOUT_SECS = 10.0
DEFAULT_ATTEMPTS = 4
DEFAULT_BACKOFF_SECS = 1.0  # doubles after each failed attempt
ERROR_EXCERPT_CHARS = 200


class RemoteRecorder:
    def __init__(
        self,
        base_url: str,
        secret: str,
        client: httpx.AsyncClient | None = None,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff_secs: float = DEFAULT_BACKOFF_SECS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._secret = secret
        self._client = client
        self._attempts = attempts
        self._backoff_secs = backoff_secs
        self._call_info: dict[str, Any] = {}
        self._events: list[dict[str, Any]] = []
        self._transcript: list[dict[str, Any]] = []
        self._usage: list[dict[str, Any]] = []
        self._recording: dict[str, Any] | None = None

    # ---- same surface as CallRecorder (only what the session handler uses) -------------------
    async def ensure_call(
        self,
        call_id: str,
        phone: str,
        persona: str = "amelia",
        language: str = "en",
        lead_id: str | None = None,
        campaign_id: str | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> None:
        self._call_info = {
            "phone": phone,
            "persona": persona,
            "language": language,
            "lead_id": lead_id,
            "campaign_id": campaign_id,
            "metadata_json": metadata_json or {},
        }

    async def record_event(
        self,
        call_id: str,
        event_type: str,
        state: str | None = None,
        payload: dict[str, Any] | None = None,
        timestamp: Any = None,
    ) -> None:
        self._events.append(
            {"event_type": event_type, "state": state, "payload": payload or {}, "timestamp": time.time()}
        )

    async def record_transcript_batch(self, call_id: str, entries: list[dict[str, Any]]) -> int:
        self._transcript = list(entries)
        return len(entries)

    async def record_usage_batch(self, call_id: str, usage_records: list[dict[str, Any]]) -> list[Any]:
        self._usage = list(usage_records)
        return []

    async def record_recording(self, call_id: str, info: dict[str, Any]) -> None:
        self._recording = dict(info)

    async def finalize_call(self, call_id: str, **kwargs: Any) -> None:
        """Send the whole call to the API. Raises when every attempt fails."""
        kwargs.pop("destination_url", None)  # the API knows lead-gen's URL
        kwargs.pop("create_outbox", None)
        body = {
            "call": self._call_info,
            "transcript": self._transcript,
            "usage": self._usage,
            "events": self._events,
            "recording": self._recording,
            "finalize": kwargs,
        }
        url = f"{self._base_url}/internal/calls/{call_id}/result"
        headers = {"X-Posivex-Callback-Secret": self._secret}
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=POST_TIMEOUT_SECS)
        try:
            last_error = "unknown error"
            for attempt in range(1, self._attempts + 1):
                try:
                    res = await client.post(url, json=body, headers=headers)
                    if res.status_code < 300:
                        logger.info("Reported call %s to the API (attempt %d).", call_id, attempt)
                        return
                    last_error = f"HTTP {res.status_code}: {res.text[:ERROR_EXCERPT_CHARS]}"
                    if res.status_code < 500 and res.status_code != 429:
                        break  # a rejected request will not succeed on retry
                except httpx.HTTPError as e:
                    last_error = repr(e)
                if attempt < self._attempts:
                    await asyncio.sleep(self._backoff_secs * (2 ** (attempt - 1)))
            raise RuntimeError(f"Could not report call {call_id} to the API: {last_error}")
        finally:
            if owns_client:
                await client.aclose()
