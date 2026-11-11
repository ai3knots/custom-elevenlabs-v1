"""S3-compatible access to call recordings (written by LiveKit egress, streamed by the API)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from src.config import RecordingConfig

logger = logging.getLogger("posivex.storage")

CONTENT_TYPES = {"ogg": "audio/ogg", "mp3": "audio/mpeg"}
CHUNK_BYTES = 64 * 1024
PROBE_CONNECT_TIMEOUT_SECS = 5
PROBE_READ_TIMEOUT_SECS = 10


@dataclass
class RecordingStream:
    content_type: str
    content_length: int | None
    chunks: AsyncIterator[bytes]


def _client(cfg: RecordingConfig, *, probe: bool = False) -> Any:
    """The S3 client for recordings. `probe=True` adds short timeouts and no retries, for the start-up check."""
    import boto3
    from botocore.config import Config

    options: dict[str, Any] = {"s3": {"addressing_style": "path" if cfg.force_path_style else "auto"}}
    if probe:
        options.update(
            connect_timeout=PROBE_CONNECT_TIMEOUT_SECS,
            read_timeout=PROBE_READ_TIMEOUT_SECS,
            retries={"max_attempts": 1},
        )
    return boto3.client(
        "s3",
        region_name=cfg.region or None,
        endpoint_url=cfg.endpoint or None,
        aws_access_key_id=cfg.access_key,
        aws_secret_access_key=cfg.secret,
        config=Config(**options),
    )


def verify_bucket_access(cfg: RecordingConfig) -> str | None:
    """Write and delete one tiny object. Returns a short reason when the bucket cannot be used, else None.

    Blocking, with short timeouts; meant for service start-up, not for a request path.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    client = _client(cfg, probe=True)
    key = f"{cfg.prefix.strip('/')}/.startup-check" if cfg.prefix.strip("/") else ".startup-check"
    try:
        client.put_object(Bucket=cfg.bucket, Key=key, Body=b"ok")
        client.delete_object(Bucket=cfg.bucket, Key=key)
    except ClientError as e:
        error = e.response.get("Error", {})
        code = error.get("Code", "")
        hints = {
            "NoSuchBucket": "the bucket does not exist (check RECORDING_BUCKET)",
            "AccessDenied": "the key is not allowed to write to this bucket",
            "InvalidAccessKeyId": "RECORDING_ACCESS_KEY is wrong",
            "SignatureDoesNotMatch": "RECORDING_SECRET is wrong",
            "PermanentRedirect": "RECORDING_REGION does not match the bucket's region",
            "AuthorizationHeaderMalformed": "RECORDING_REGION does not match the bucket's region",
        }
        return f"{code}: {hints.get(code, error.get('Message', 'request failed'))}"
    except BotoCoreError as e:
        return f"could not reach the storage endpoint ({e})"
    return None


async def open_recording(cfg: RecordingConfig, key: str, file_type: str = "ogg") -> RecordingStream | None:
    """Open a recording for streaming, or None when it does not exist (yet)."""
    from botocore.exceptions import ClientError

    client = _client(cfg)
    try:
        obj = await asyncio.to_thread(client.get_object, Bucket=cfg.bucket, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    body = obj["Body"]

    async def chunks() -> AsyncIterator[bytes]:
        try:
            while True:
                data = await asyncio.to_thread(body.read, CHUNK_BYTES)
                if not data:
                    break
                yield data
        finally:
            body.close()

    return RecordingStream(
        content_type=CONTENT_TYPES.get(file_type, "application/octet-stream"),
        content_length=obj.get("ContentLength"),
        chunks=chunks(),
    )
