"""Checks run when a service starts, so a bad setting is reported at boot instead of on the first call."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from src.config import Settings

logger = logging.getLogger("posivex.startup")

DEFAULT_WEBHOOK_SECRET = "dev_webhook_secret"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


class StartupCheckError(RuntimeError):
    """A setting is wrong in a way that would silently break production."""


def check_webhook_config(settings: Settings) -> None:
    """Refuse to start when post-call webhooks to a real lead-gen would be signed with the built-in dev secret.

    `ELEVENLABS_WEBHOOK_SECRET` unset falls back to a public placeholder. Lead-gen then rejects every result
    with `401 Invalid signature` and the call pages stay empty. A local lead-gen (development) is left alone.
    """
    if settings.webhook.webhook_secret != DEFAULT_WEBHOOK_SECRET:
        return
    host = urlparse(settings.webhook_destination()).hostname or ""
    if host in _LOCAL_HOSTS:
        return
    raise StartupCheckError(
        f"ELEVENLABS_WEBHOOK_SECRET is not set, but post-call webhooks go to {settings.webhook_destination()}. "
        "Set it to the same value as VOICE_WEBHOOK_SECRET on lead-gen, then recreate the container "
        "(docker compose up -d --force-recreate)."
    )


def check_recording_storage(settings: Settings) -> str | None:
    """Verify the recording bucket can be written. Logs the result; returns the error, or None when fine/off.

    Never raises: a recording problem must not stop calls from being placed. The check writes and deletes one
    tiny object, which is what LiveKit's egress needs to do with the same credentials.
    """
    cfg = settings.recording
    if not cfg.enabled:
        return None
    if not cfg.ready:
        error = "RECORDING_ENABLED is true but RECORDING_BUCKET, RECORDING_ACCESS_KEY or RECORDING_SECRET is missing"
        logger.error("Recording is on but cannot work: %s", error)
        return error
    from src.storage import verify_bucket_access

    error = verify_bucket_access(cfg)
    if error:
        logger.error("Recording bucket '%s' is not usable, calls will have no audio: %s", cfg.bucket, error)
    else:
        logger.info("Recording bucket '%s' is reachable and writable (region %s).", cfg.bucket, cfg.region)
    return error
