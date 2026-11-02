"""Transactional outbox worker for resilient ElevenLabs-compatible webhook delivery."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import Settings
from src.db.models import WebhookOutbox, WebhookStatus
from src.webhook.signer import sign_payload

logger = logging.getLogger("posivex.webhook.worker")


def calculate_backoff_seconds(
    attempt: int,
    base: float = 2.0,
    initial_delay_secs: float = 2.0,
    max_delay_secs: float = 3600.0,
) -> float:
    """Calculate exponential backoff interval in seconds."""
    delay = initial_delay_secs * (base ** max(0, attempt - 1))
    return min(delay, max_delay_secs)


class WebhookOutboxWorker:
    """Processes pending transactional webhook outbox records with signing and exponential backoff."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self._client = client

    async def deliver_entry(self, entry_id: str) -> bool:
        """Attempt delivery of a single outbox record by ID."""
        async with self.session_factory() as session:
            stmt = select(WebhookOutbox).where(WebhookOutbox.id == entry_id)
            res = await session.execute(stmt)
            entry = res.scalar_one_or_none()

            if not entry or entry.status in (
                WebhookStatus.DELIVERED.value,
                WebhookStatus.DEAD_LETTER.value,
            ):
                return False

            entry.status = WebhookStatus.PROCESSING.value
            await session.commit()

        # Build payload and compute signature
        raw_body = json.dumps(entry.payload, separators=(",", ":"))
        sig_header = sign_payload(raw_body, secret=self.settings.webhook.webhook_secret)

        headers = {
            "Content-Type": "application/json",
            "elevenlabs-signature": sig_header,
            "X-Delivery-Id": entry.id,
        }

        should_close_client = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=10.0)
            should_close_client = True

        success = False
        error_msg: str | None = None

        try:
            response = await client.post(entry.destination_url, content=raw_body, headers=headers)
            if 200 <= response.status_code < 300:
                success = True
            else:
                error_msg = f"HTTP {response.status_code}: {response.text[:200]}"
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            if should_close_client:
                await client.aclose()

        # Update entry status in a fresh transaction
        async with self.session_factory() as session:
            stmt = select(WebhookOutbox).where(WebhookOutbox.id == entry_id)
            res = await session.execute(stmt)
            updated_entry = res.scalar_one_or_none()
            if not updated_entry:
                return False

            now = datetime.now(UTC)
            if success:
                updated_entry.status = WebhookStatus.DELIVERED.value
                updated_entry.delivered_at = now
                updated_entry.signature_header = sig_header
                updated_entry.last_error = None
                logger.info("Successfully delivered webhook outbox id=%s", entry_id)
            else:
                updated_entry.attempts += 1
                updated_entry.last_error = error_msg
                if updated_entry.attempts >= updated_entry.max_attempts:
                    updated_entry.status = WebhookStatus.DEAD_LETTER.value
                    logger.warning(
                        "Webhook outbox id=%s reached max attempts (%d); marked DEAD_LETTER: %s",
                        entry_id,
                        updated_entry.max_attempts,
                        error_msg,
                    )
                else:
                    updated_entry.status = WebhookStatus.PENDING.value
                    delay = calculate_backoff_seconds(
                        updated_entry.attempts,
                        base=self.settings.webhook.retry_backoff_base,
                    )
                    updated_entry.next_retry_at = now + timedelta(seconds=delay)
                    logger.warning(
                        "Webhook outbox id=%s attempt %d failed: %s; retrying in %.1fs",
                        entry_id,
                        updated_entry.attempts,
                        error_msg,
                        delay,
                    )

            await session.commit()
            return success

    async def process_pending(self, limit: int = 50) -> int:
        """Fetch and process a batch of pending webhook entries."""
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            stmt = (
                select(WebhookOutbox.id)
                .where(
                    WebhookOutbox.status.in_([
                        WebhookStatus.PENDING.value,
                        WebhookStatus.PROCESSING.value,
                    ]),
                    WebhookOutbox.next_retry_at <= now,
                )
                .order_by(WebhookOutbox.created_at.asc())
                .limit(limit)
            )
            res = await session.execute(stmt)
            entry_ids = list(res.scalars().all())

        if not entry_ids:
            return 0

        logger.debug("Processing batch of %d pending webhook outbox entries", len(entry_ids))
        for entry_id in entry_ids:
            await self.deliver_entry(entry_id)

        return len(entry_ids)

    async def run(
        self,
        poll_interval_secs: float = 2.0,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Run continuous background polling loop until stopped."""
        logger.info("Starting Webhook Outbox Worker poll loop (interval=%.1fs)", poll_interval_secs)
        while stop_event is None or not stop_event.is_set():
            try:
                count = await self.process_pending()
                if count == 0 and stop_event is not None:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_secs)
                        break
                    except TimeoutError:
                        pass
                elif count == 0:
                    await asyncio.sleep(poll_interval_secs)
            except Exception as e:
                logger.exception("Error in webhook outbox polling loop: %s", e)
                await asyncio.sleep(poll_interval_secs)

        logger.info("Webhook Outbox Worker poll loop stopped.")
