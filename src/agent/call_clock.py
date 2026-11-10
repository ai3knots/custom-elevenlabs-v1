"""Call time is counted from pickup, not from the moment the agent job started."""

from __future__ import annotations

import time
from collections.abc import Callable


class CallClock:
    """Seconds since the callee picked up (0 until they do), plus how long the phone rang.

    The agent job starts when the call is dialed, so job time includes 5 to 75 s of ringing. Lead-gen's
    durations and the transcript times should match what ElevenLabs reported: time on the line.
    """

    def __init__(self, started_at: float | None = None, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._started_at = self._now() if started_at is None else started_at
        self._pickup_at: float | None = None

    def pick_up(self) -> float:
        """Mark the moment the callee answered; returns how long it rang."""
        if self._pickup_at is None:
            self._pickup_at = self._now()
        return self.ring_secs

    @property
    def picked_up(self) -> bool:
        return self._pickup_at is not None

    @property
    def ring_secs(self) -> float:
        """Dial to pickup, or dial to now while it is still ringing."""
        return max(0.0, (self._pickup_at if self._pickup_at is not None else self._now()) - self._started_at)

    def since_pickup(self) -> float:
        return 0.0 if self._pickup_at is None else max(0.0, self._now() - self._pickup_at)
