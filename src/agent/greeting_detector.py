"""Who answered? A fast LLM check on the callee's streaming transcript, run beside the opening line.

LiveKit's AMD classifies on the first full utterance and pauses the agent while it listens. With a 3 s cap
it ended before most greetings finished ("uncertain" on 27 of 28 real calls), so it only delayed the opener.
This detector never blocks anything: it reads what Deepgram has transcribed so far (interim results
included, so it does not wait for the callee to finish speaking), asks a small Gemini model whether that is
a person, a voicemail or a phone menu / carrier message, and calls `on_machine` the moment it is confident.
It asks again as more words arrive, because a business greeting can turn into "press one" a few seconds in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("posivex.greeting")

HUMAN = "human"
VOICEMAIL = "voicemail"
MENU = "menu"
UNCLEAR = "unclear"
CATEGORIES = (HUMAN, VOICEMAIL, MENU, UNCLEAR)
MACHINE_CATEGORIES = (VOICEMAIL, MENU)
LOGGED_OUTPUT_CHARS = 200  # how much of an unusable model reply is logged

GREETING_SYSTEM = """You listen to the first seconds of an outbound phone call that a business has just answered. You get the transcript of what the other party has said so far. It may be cut off mid-sentence and may contain transcription errors. Decide who or what answered.

Categories:
- human: a live person speaking to the caller. Examples: "Hello?", "Thank you for calling Joe's Auto, this is Maria, how can I help you?" said as a live greeting, "Yes?", a question, a conversational reply. A business name or a greeting with no instructions to press keys or leave a message is a person.
- voicemail: a recorded mailbox or answering machine. Examples: "You've reached ... leave a message after the tone", "I'm not available right now", a recorded greeting that gives business hours or says the office is closed, "the mailbox is full", "the person you are calling is not available".
- menu: an automated phone system, not a mailbox. Examples: "For sales press one", "for English press one", "please hold while we connect your call", "record your name and reason for calling and I'll see if this person is available" (a call screener), a text-us auto reply, a carrier or network message such as "the subscriber you have called is not available" or "this number is not in service".
- unclear: too few words to tell, or it could be either.

Rules:
- A notice such as "this call may be recorded" on its own is unclear, not a machine.
- Never label a live person as a machine. If it could be a person, choose human or unclear.
- Say menu or voicemail only when the words clearly show a recording or an automated system.

Return JSON with `category` and `confidence` (0 to 1: how sure you are)."""


@dataclass
class GreetingVerdict:
    category: str
    confidence: float
    seconds: float = 0.0  # how long the model took

    @property
    def is_machine(self) -> bool:
        return self.category in MACHINE_CATEGORIES


def machine_outcome(category: str) -> tuple[str, str]:
    """(call outcome, end reason) for a machine category.

    A phone menu, screener or carrier message is `automated_menu` (retried after 24 h, no follow-up email in
    lead-gen); a mailbox is `voicemail` (retried after 4 h, with a follow-up email). They used to be one label.
    """
    if category == MENU:
        return "automated_menu", "automated_menu_detected"
    return "voicemail", "voicemail_detected"


Classify = Callable[[str], Awaitable[GreetingVerdict]]
OnMachine = Callable[[GreetingVerdict, str], None]


def _make_client(api_key: str) -> Any:
    from google.genai import Client

    return Client(api_key=api_key)


def _thinking_config(model: str) -> Any:
    from google.genai import types

    from src.providers.llm import uses_thinking_budget

    if uses_thinking_budget(model):
        return types.ThinkingConfig(thinking_budget=0)
    return types.ThinkingConfig(thinking_level="MINIMAL")  # type: ignore[arg-type]


class GeminiGreetingClassifier:
    """One tiny Gemini request per call of `classify`. Build the client first with `warm()`."""

    def __init__(self, api_key: str, model: str, timeout: float = 4.0) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._client: Any | None = None

    async def warm(self) -> None:
        """Create the Gemini client off the event loop (loading the CA bundle takes ~150 ms of CPU)."""
        if self._client is None:
            self._client = await asyncio.to_thread(_make_client, self._api_key)

    async def classify(self, transcript: str) -> GreetingVerdict:
        from google.genai import types

        await self.warm()
        schema = types.Schema(
            type=types.Type.OBJECT,
            properties={
                "category": types.Schema(type=types.Type.STRING, enum=list(CATEGORIES)),
                "confidence": types.Schema(type=types.Type.NUMBER),
            },
            required=["category", "confidence"],
        )
        started = time.monotonic()
        response = await asyncio.wait_for(
            self._client.aio.models.generate_content(  # type: ignore[union-attr]
                model=self._model,
                contents=f"Transcript so far:\n{transcript}",
                config=types.GenerateContentConfig(
                    system_instruction=GREETING_SYSTEM,
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0.0,
                    max_output_tokens=60,
                    thinking_config=_thinking_config(self._model),
                ),
            ),
            timeout=self._timeout,
        )
        return parse_verdict(response.text, seconds=time.monotonic() - started)


def parse_verdict(text: str | None, *, seconds: float = 0.0) -> GreetingVerdict:
    """Turn the model's JSON into a verdict; anything unusable is 'unclear' with no confidence."""
    try:
        data = json.loads(text or "{}")
        category = str(data.get("category", "")).strip().lower()
        confidence = float(data.get("confidence", 0.0))
    except (ValueError, TypeError, AttributeError):
        logger.debug("Unusable greeting-check output, treating it as unclear: %r", (text or "")[:LOGGED_OUTPUT_CHARS])
        return GreetingVerdict(UNCLEAR, 0.0, seconds)
    if category not in CATEGORIES:
        return GreetingVerdict(UNCLEAR, 0.0, seconds)
    return GreetingVerdict(category, max(0.0, min(1.0, confidence)), seconds)


class GreetingDetector:
    """Feeds the callee's transcript to a classifier and reports a confident machine verdict once.

    `feed()` is called for every transcript event (interim and final). At most one classification runs at a
    time; a new one starts when at least `step_words` new words have arrived and `min_interval` seconds have
    passed since the last. It stops on a machine verdict, after `window_seconds`, after `max_requests`
    requests, or when `stop()` is called (for example once a real conversation is under way).
    """

    def __init__(
        self,
        classify: Classify,
        on_machine: OnMachine,
        *,
        confidence: float = 0.8,
        window_seconds: float = 25.0,
        min_words: int = 3,
        step_words: int = 3,
        min_interval: float = 1.0,
        max_requests: int = 8,
        on_verdict: Callable[[GreetingVerdict, str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._classify = classify
        self._on_machine = on_machine
        self._on_verdict = on_verdict
        self._confidence = confidence
        self._window = window_seconds
        self._min_words = min_words
        self._step_words = step_words
        self._min_interval = min_interval
        self._max_requests = max_requests
        self._clock = clock
        self._finals: list[str] = []
        self._interim = ""
        self._started_at: float | None = None
        self._active = False
        self._task: asyncio.Task[None] | None = None
        self._requests = 0
        self._last_words = 0
        self._last_started = float("-inf")
        self.last_verdict: GreetingVerdict | None = None

    # ---- lifecycle -----------------------------------------------------------------------------
    def start(self) -> None:
        """Call at pickup: the window and the transcript start now."""
        self._started_at = self._clock()
        self._finals, self._interim = [], ""
        self._active = True
        self._maybe_schedule()

    def stop(self) -> None:
        self._active = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def text(self) -> str:
        return " ".join(part for part in (*self._finals, self._interim) if part).strip()

    # ---- input ---------------------------------------------------------------------------------
    def feed(self, transcript: str, is_final: bool) -> None:
        text = (transcript or "").strip()
        if not self._active or not text:
            return
        if is_final:
            self._finals.append(text)
            self._interim = ""
        else:
            self._interim = text
        self._maybe_schedule()

    # ---- scheduling ----------------------------------------------------------------------------
    def _expired(self) -> bool:
        return self._started_at is not None and self._clock() - self._started_at > self._window

    def _maybe_schedule(self) -> None:
        if not self._active:
            return
        if self._expired() or self._requests >= self._max_requests:
            self._active = False
            return
        if self._task is not None and not self._task.done():
            return  # one request at a time; the finished one re-checks for newer words
        words = len(self.text.split())
        if words < self._min_words or words - self._last_words < (self._step_words if self._last_words else 1):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._run())

    async def _run(self) -> None:
        wait = self._min_interval - (self._clock() - self._last_started)
        if wait > 0:
            await asyncio.sleep(wait)
        if not self._active:
            return
        text = self.text  # the latest words, not the ones that triggered this run
        self._last_started = self._clock()
        self._last_words = len(text.split())
        self._requests += 1
        try:
            verdict = await self._classify(text)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # a classification failure must never disturb the call
            logger.warning("Greeting check failed (%s: %s); the agent's own tool is the fallback.", type(e).__name__, e)
            self._task = None
            return
        self.last_verdict = verdict
        logger.info(
            "Greeting check: %s (%.2f) in %.2fs after %d words", verdict.category, verdict.confidence, verdict.seconds,
            self._last_words,
        )
        if self._on_verdict is not None:
            try:
                self._on_verdict(verdict, text)
            except Exception:
                logger.exception("Verdict handler failed.")
        if verdict.is_machine and verdict.confidence >= self._confidence and self._active:
            self._active = False
            self._on_machine(verdict, text)
            return
        self._task = None
        self._maybe_schedule()  # more words may have arrived while the model was thinking
