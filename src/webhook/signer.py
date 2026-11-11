"""HMAC-SHA256 signature generator and verifier matching ElevenLabs webhook contract."""

from __future__ import annotations

import hashlib
import hmac
import time

# How far a signature's timestamp may be from now before it is rejected (replay protection).
SIGNATURE_TOLERANCE_SECS = 1800


def sign_payload(
    raw_body: str | bytes,
    secret: str,
    now_secs: int | None = None,
) -> str:
    """Generate an ElevenLabs-compatible HMAC-SHA256 signature header.

    Format: `t=<unix_secs>,v0=<hex_hmac>` where
    hmac = HMAC-SHA256(secret, `${unix_secs}.${raw_body}`).
    """
    ts = now_secs if now_secs is not None else int(time.time())
    body_bytes = raw_body.encode() if isinstance(raw_body, str) else raw_body
    prefix = f"{ts}.".encode()
    message = prefix + body_bytes
    signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"t={ts},v0={signature}"


def verify_signature(
    header: str | None,
    raw_body: str | bytes,
    secret: str,
    now_secs: int | None = None,
    tolerance_secs: int = SIGNATURE_TOLERANCE_SECS,
) -> bool:
    """Verify an incoming ElevenLabs webhook signature header.

    Matches Posivex-lead-gen verifyElevenLabsSignature specification with replay tolerance.
    """
    if not secret or not header:
        return False

    timestamp_str = ""
    hashes: list[str] = []

    for part in header.split(","):
        subparts = part.strip().split("=", 1)
        if len(subparts) == 2:
            key, val = subparts
            if key == "t":
                timestamp_str = val
            elif key in ("v0", "v1"):
                hashes.append(val)

    if not timestamp_str or not hashes:
        return False

    try:
        ts = int(timestamp_str)
    except ValueError:
        return False

    current_time = now_secs if now_secs is not None else int(time.time())
    if abs(current_time - ts) > tolerance_secs:
        return False

    body_bytes = raw_body.encode() if isinstance(raw_body, str) else raw_body
    prefix = f"{ts}.".encode()
    expected_hash = hmac.new(secret.encode(), prefix + body_bytes, hashlib.sha256).hexdigest()

    return any(hmac.compare_digest(h, expected_hash) for h in hashes)
