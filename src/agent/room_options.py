"""Room options for a call: hang up when the session closes, and optionally clean up the callee's audio.

Noise cancellation runs on the audio that goes to speech-to-text, the voice-activity detector and the turn detector, so
background noise (a kitchen, traffic, a TV) is less likely to be read as speech or to cut the agent off. It does not
change the recording, which is taken from the room itself.

Krisp NC is the background-noise model that LiveKit Cloud includes at no extra charge. The voice-isolation models
(including the telephony one) are metered. The setting is `conversation.noise_cancellation`.
"""

import logging

from livekit import rtc
from livekit.agents import room_io

logger = logging.getLogger("posivex.session")

KRISP_NC = "krisp_nc"
NO_NOISE_CANCELLATION = "none"


def build_noise_cancellation(mode: str) -> rtc.NoiseCancellationOptions | None:
    """The noise-cancellation filter for `mode`, or None when it is off or cannot be loaded.

    An enhancement must never be the reason a call fails, so a missing or broken plugin logs a warning and the call
    goes ahead with the raw audio.
    """
    if mode == NO_NOISE_CANCELLATION:
        return None
    if mode != KRISP_NC:
        logger.warning("Unknown noise cancellation mode %r; continuing without it.", mode)
        return None
    try:
        from livekit.plugins import noise_cancellation

        return noise_cancellation.NC()
    except Exception:
        logger.warning("Krisp noise cancellation could not be loaded; continuing without it.", exc_info=True)
        return None


def build_room_options(mode: str) -> tuple[room_io.RoomOptions, bool]:
    """The room options for a call and whether noise cancellation is on.

    Deleting the room on close hangs up the SIP leg; otherwise the callee is left on a silent line.
    """
    filter_ = build_noise_cancellation(mode)
    if filter_ is None:
        return room_io.RoomOptions(delete_room_on_close=True), False
    return (
        room_io.RoomOptions(
            delete_room_on_close=True,
            audio_input=room_io.AudioInputOptions(noise_cancellation=filter_),
        ),
        True,
    )
