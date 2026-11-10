"""Words the callee says over our opening line, and the turn-handling settings that let them be heard."""

from __future__ import annotations

import pytest

from src.agent.opener import needs_reply
from src.agent.session_handler import build_turn_handling
from src.config import Settings

OPENER = "Hi, this is Amelia from Clover Posivex. Am I speaking with the owner of Acme Auto?"


@pytest.mark.parametrize("text", ["", "   ", "Hello?", "Hello. Hello?", "Hi, hello", "Hey"])
def test_a_bare_greeting_over_the_opener_needs_no_reply(text: str) -> None:
    assert needs_reply(text, OPENER) is False


@pytest.mark.parametrize(
    "text",
    [
        "Yes, this is the owner.",
        "Yes.",
        "Who is this?",
        "Thank you for calling Acme Auto, how can I help you?",
        "No, he's not here right now.",
    ],
)
def test_a_real_answer_over_the_opener_gets_a_reply(text: str) -> None:
    assert needs_reply(text, OPENER) is True


def test_our_own_opener_coming_back_as_an_echo_is_not_answered() -> None:
    assert needs_reply("Hi this is Amelia from Clover Posivex", OPENER) is False
    assert needs_reply("hi this is amelia from clover posivex am I speaking with the owner", OPENER) is False


def test_the_callees_audio_keeps_reaching_speech_to_text_while_the_opener_plays() -> None:
    options = build_turn_handling(Settings())
    assert options["interruption"]["discard_audio_if_uninterruptible"] is False


def test_the_end_of_turn_wait_is_capped_at_one_and_a_half_seconds() -> None:
    endpointing = build_turn_handling(Settings())["endpointing"]
    assert endpointing["max_delay"] == 1.5
    assert endpointing["min_delay"] == 0.5
