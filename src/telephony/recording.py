"""Start a LiveKit egress that records the call to an S3-compatible bucket (audio only)."""

from __future__ import annotations

import logging
from typing import Any

from livekit import api

from src.config import RecordingConfig

logger = logging.getLogger("posivex.telephony.recording")


def recording_key(cfg: RecordingConfig, call_id: str) -> str:
    return f"{cfg.prefix.strip('/')}/{call_id}.{cfg.file_type}" if cfg.prefix.strip("/") else f"{call_id}.{cfg.file_type}"


async def start_call_recording(
    lkapi: Any, room_name: str, call_id: str, cfg: RecordingConfig
) -> dict[str, Any] | None:
    """Begin an audio-only room-composite egress. Returns where the file will appear, or None if off/failed.

    The egress stops by itself when the room closes, and the file is complete shortly after. `layout` and
    `custom_base_url` stay unset so LiveKit bills it as audio only.
    """
    if not cfg.ready:
        return None
    key = recording_key(cfg, call_id)
    file_type = api.EncodedFileType.MP3 if cfg.file_type == "mp3" else api.EncodedFileType.OGG
    request = api.RoomCompositeEgressRequest(
        room_name=room_name,
        audio_only=True,
        file_outputs=[
            api.EncodedFileOutput(
                file_type=file_type,
                filepath=key,
                s3=api.S3Upload(
                    access_key=cfg.access_key,
                    secret=cfg.secret,
                    region=cfg.region,
                    endpoint=cfg.endpoint,
                    bucket=cfg.bucket,
                    force_path_style=cfg.force_path_style,
                ),
            )
        ],
    )
    try:
        info = await lkapi.egress.start_room_composite_egress(request)
    except Exception:
        # A recording problem must never affect the call itself.
        logger.exception("Could not start the recording for call %s", call_id)
        return None
    logger.info("Recording call %s to s3://%s/%s (egress %s)", call_id, cfg.bucket, key, info.egress_id)
    return {"egress_id": info.egress_id, "bucket": cfg.bucket, "key": key, "file_type": cfg.file_type}
