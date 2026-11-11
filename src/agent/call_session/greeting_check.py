"""Is it a person, a voicemail or a phone menu? Runs beside the opener on the streaming transcript.

On a confident machine verdict the agent stops talking and hangs up, and the call gets the matching outcome
instead of a guess after the fact.
"""

from __future__ import annotations

import logging
from typing import Any

from src.agent.call_session.lifecycle import CallLifecycle
from src.agent.call_session.tasks import BackgroundTasks
from src.agent.call_session.timeline import CallTimeline
from src.agent.greeting_detector import GreetingDetector, GreetingVerdict, machine_outcome
from src.agent.persona_agent import EARLY_CALL_USER_TURNS, CallState
from src.analysis import CallSignals
from src.config import ConversationConfig

logger = logging.getLogger("posivex.session")

# How much of the callee's words is kept in a rationale.
RATIONALE_TEXT_CHARS = 200


class GreetingCheck:
    def __init__(
        self,
        *,
        classifier: Any | None,
        session: Any,
        call_state: CallState,
        lifecycle: CallLifecycle,
        timeline: CallTimeline,
        signals: CallSignals,
        tasks: BackgroundTasks,
        config: ConversationConfig,
    ) -> None:
        self._classifier = classifier
        self._session = session
        self._call_state = call_state
        self._lifecycle = lifecycle
        self._timeline = timeline
        self._signals = signals
        self._tasks = tasks
        self._config = config
        self._detector: GreetingDetector | None = None
        if classifier is not None:
            self._detector = GreetingDetector(
                classifier.classify,
                self._handle_machine,
                confidence=config.machine_detection_confidence,
                window_seconds=config.machine_detection_window_seconds,
                on_verdict=self._note_verdict,
            )

    @property
    def enabled(self) -> bool:
        return self._detector is not None

    def warm(self) -> None:
        """Build the Gemini client now, while the phone is still ringing, so the first check is not delayed."""
        if self._classifier is not None:
            self._tasks.spawn(self._classifier.warm())

    def start(self) -> None:
        if self._detector is not None:
            self._detector.start()

    def stop(self) -> None:
        if self._detector is not None:
            self._detector.stop()

    def on_transcript(self, text: str, is_final: bool) -> None:
        if self._detector is None:
            return
        if self._call_state.user_turns >= EARLY_CALL_USER_TURNS:
            self._detector.stop()  # a real conversation is under way
        else:
            self._detector.feed(text, is_final)

    def allows_reply_to_overlap(self) -> bool:
        """A reply to words said over the opener needs a confirmed person (a voicemail greeting never gets one)."""
        return self._detector is None or self._call_state.greeting_verdict == "human"

    # ---- verdicts ------------------------------------------------------------------------------
    def _handle_machine(self, verdict: GreetingVerdict, text: str) -> None:
        if self._lifecycle.reason or self._call_state.voicemail_detected:
            return
        outcome, reason = machine_outcome(verdict.category)
        self._signals.forced_outcome = self._signals.forced_outcome or outcome
        self._signals.forced_rationale = self._signals.forced_rationale or (
            f"Greeting check: {verdict.category} ({verdict.confidence:.2f}). {text[:RATIONALE_TEXT_CHARS]}"
        )
        self._call_state.greeting_verdict = verdict.category
        self._call_state.end_reason = self._call_state.end_reason or "machine_detected"
        self._timeline.mark(
            "machine_detected",
            category=verdict.category,
            confidence=round(verdict.confidence, 2),
            model_secs=round(verdict.seconds, 2),
            words=len(text.split()),
        )
        try:
            self._session.interrupt(force=True)  # the opener is uninterruptible, so force it
        except Exception:
            logger.debug("Could not interrupt speech before hanging up on a machine.", exc_info=True)
        self._lifecycle.end(reason, drain=False)

    def _note_verdict(self, verdict: GreetingVerdict, text: str) -> None:
        self._timeline.mark(
            "greeting_check",
            category=verdict.category,
            confidence=round(verdict.confidence, 2),
            model_secs=round(verdict.seconds, 2),
            words=len(text.split()),
        )
        if verdict.category == "human" and verdict.confidence >= self._config.machine_detection_confidence:
            self._call_state.greeting_verdict = "human"  # lets the voicemail tool refuse a false "machine" call
