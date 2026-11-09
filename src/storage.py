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


@dataclass
class RecordingStream:
    content_type: str
    content_length: int | None
    chunks: AsyncIterator[bytes]


def _client(cfg: RecordingConfig) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        region_name=cfg.region or None,
        endpoint_url=cfg.endpoint or None,
        aws_access_key_id=cfg.access_key,
        aws_secret_access_key=cfg.secret,
        config=Config(s3={"addressing_style": "path" if cfg.force_path_style else "auto"}),
    )


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
