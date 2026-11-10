"""The greeting check (person, voicemail or phone menu) that replaced LiveKit's AMD gate."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from src.agent import greeting_detector as gd
from src.agent.greeting_detector import (
    GeminiGreetingClassifier,
    GreetingDetector,
    GreetingVerdict,
    machine_outcome,
    parse_verdict,
)


def verdict(category: str, confidence: float = 0.95) -> GreetingVerdict:
    return GreetingVerdict(category, confidence, 0.4)


class Harness:
    """A detector with a scripted classifier; records every transcript the classifier saw."""

    def __init__(self, answers: list[GreetingVerdict | Exception], **kwargs: Any) -> None:
        self.answers = list(answers)
        self.seen: list[str] = []
        self.machine: list[tuple[GreetingVerdict, str]] = []
        self.detector = GreetingDetector(
            self.classify, lambda v, t: self.machine.append((v, t)), min_interval=0.0, **kwargs
        )

    async def classify(self, text: str) -> GreetingVerdict:
        self.seen.append(text)
        answer = self.answers.pop(0) if self.answers else verdict("unclear", 0.0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def settle(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)


# ---- parsing --------------------------------------------------------------------------------------------
def test_parse_verdict_reads_category_and_clamps_confidence() -> None:
    assert parse_verdict('{"category": "voicemail", "confidence": 0.9}').category == "voicemail"
    assert parse_verdict('{"category": "menu", "confidence": 7}').confidence == 1.0
    assert parse_verdict('{"category": "human", "confidence": -1}').confidence == 0.0


@pytest.mark.parametrize("bad", [None, "", "not json", '{"category": "robot", "confidence": 1}', '{"confidence": 1}'])
def test_unusable_model_output_is_unclear_with_no_confidence(bad: str | None) -> None:
    result = parse_verdict(bad)
    assert (result.category, result.confidence) == ("unclear", 0.0)


def test_machine_outcome_separates_mailboxes_from_menus() -> None:
    assert machine_outcome("voicemail") == ("voicemail", "voicemail_detected")
    assert machine_outcome("menu") == ("automated_menu", "automated_menu_detected")


# ---- when it asks and what it does ----------------------------------------------------------------------
async def test_it_waits_for_a_few_words_before_asking() -> None:
    h = Harness([verdict("voicemail")])
    h.detector.start()
    h.detector.feed("Hi", False)
    h.detector.feed("Hi you've", False)
    await h.settle()
    assert h.seen == []
    h.detector.feed("Hi you've reached", False)
    await h.settle()
    assert h.seen == ["Hi you've reached"]


async def test_a_confident_machine_verdict_fires_once_with_the_latest_words_and_stops_the_check() -> None:
    h = Harness([verdict("menu", 0.93)])
    h.detector.start()
    h.detector.feed("for sales press one", False)
    await h.settle()
    assert [(v.category, t) for v, t in h.machine] == [("menu", "for sales press one")]
    assert h.detector.active is False
    h.detector.feed("for service press two for billing press three", True)
    await h.settle()
    assert len(h.machine) == 1 and len(h.seen) == 1


async def test_a_low_confidence_machine_verdict_does_not_hang_up_and_it_asks_again_as_words_arrive() -> None:
    h = Harness([verdict("voicemail", 0.5), verdict("voicemail", 0.95)])
    h.detector.start()
    h.detector.feed("you have reached", False)
    await h.settle()
    assert h.machine == []
    h.detector.feed("you have reached the voicemail of john please leave a message", False)
    await h.settle()
    assert len(h.machine) == 1 and h.machine[0][1].endswith("leave a message")


async def test_a_person_never_triggers_the_machine_handler() -> None:
    h = Harness([verdict("human", 0.99), verdict("human", 0.99)])
    h.detector.start()
    h.detector.feed("thank you for calling joes how can i help", True)
    await h.settle()
    h.detector.feed("thank you for calling joes how can i help you today sir", True)
    await h.settle()
    assert h.machine == []
    assert h.detector.last_verdict is not None and h.detector.last_verdict.category == "human"


async def test_a_greeting_that_turns_into_a_menu_is_caught_on_the_second_look() -> None:
    h = Harness([verdict("human", 0.9), verdict("menu", 0.97)])
    h.detector.start()
    h.detector.feed("thank you for calling acme", True)
    await h.settle()
    h.detector.feed("thank you for calling acme for sales press one", False)
    await h.settle()
    assert [v.category for v, _ in h.machine] == ["menu"]


async def test_one_request_at_a_time_and_it_uses_the_newest_words() -> None:
    gate = asyncio.Event()
    seen: list[str] = []
    machine: list[str] = []

    async def slow(text: str) -> GreetingVerdict:
        seen.append(text)
        await gate.wait()
        return verdict("unclear", 0.0) if len(seen) == 1 else verdict("voicemail", 0.9)

    det = GreetingDetector(slow, lambda v, t: machine.append(t), min_interval=0.0)
    det.start()
    det.feed("hello you have reached", False)
    await asyncio.sleep(0)
    det.feed("hello you have reached the offices of", False)
    det.feed("hello you have reached the offices of acme leave a message", False)
    await asyncio.sleep(0)
    assert len(seen) == 1  # the first request is still running
    gate.set()
    for _ in range(20):
        await asyncio.sleep(0)
    assert seen[1] == "hello you have reached the offices of acme leave a message"
    assert machine == [seen[1]]


async def test_a_failing_classifier_never_disturbs_the_call() -> None:
    h = Harness([RuntimeError("boom"), verdict("voicemail", 0.9)])
    h.detector.start()
    h.detector.feed("one two three", False)
    await h.settle()
    assert h.machine == []
    h.detector.feed("one two three four five six", False)
    await h.settle()
    assert len(h.machine) == 1  # it tried again with more words


async def test_it_gives_up_after_the_window() -> None:
    now = {"t": 0.0}
    seen: list[str] = []

    async def classify(text: str) -> GreetingVerdict:
        seen.append(text)
        return verdict("unclear", 0.0)

    det = GreetingDetector(classify, lambda v, t: None, window_seconds=25.0, min_interval=0.0, clock=lambda: now["t"])
    det.start()
    det.feed("one two three", False)
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(seen) == 1
    now["t"] = 26.0
    det.feed("one two three four five six seven", False)
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(seen) == 1 and det.active is False


async def test_stop_ends_the_check() -> None:
    h = Harness([verdict("voicemail", 0.99)])
    h.detector.start()
    h.detector.stop()
    h.detector.feed("please leave a message", False)
    await h.settle()
    assert h.seen == [] and h.machine == []


# ---- the Gemini call ------------------------------------------------------------------------------------
async def test_gemini_classifier_sends_the_transcript_and_parses_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class Models:
        async def generate_content(self, **kw: Any) -> Any:
            calls.append(kw)
            return SimpleNamespace(text='{"category": "menu", "confidence": 0.92}')

    monkeypatch.setattr(gd, "_make_client", lambda key: SimpleNamespace(aio=SimpleNamespace(models=Models())))
    classifier = GeminiGreetingClassifier("key", "gemini-3.5-flash-lite")
    result = await classifier.classify("for sales press one")
    assert (result.category, result.confidence) == ("menu", 0.92)
    assert "for sales press one" in calls[0]["contents"]
    assert calls[0]["model"] == "gemini-3.5-flash-lite"
    assert calls[0]["config"].max_output_tokens == 60  # a tiny, fast request
