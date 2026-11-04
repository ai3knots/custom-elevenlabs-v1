"""Tests for the post-call outcome classifier wiring (no network)."""

import pytest

from src.analysis import CLASSIFIER_OUTCOMES, CallSignals, OutcomeResult, resolve_call_outcome
from src.analysis.outcome import RULES_PATH, format_transcript
from src.db.models import CallOutcome

TALK = [
    {"role": "agent", "message": "Hi, this is Amelia from Clover Posivex."},
    {"role": "user", "message": "Yes, speaking."},
]


async def fake_classifier(transcript, signals):
    return OutcomeResult("callback_set", "Callback Saturday 6 PM.", "A short call.")


async def failing_classifier(transcript, signals):
    raise RuntimeError("boom")


def test_classifier_outcomes_are_valid_crm_outcomes():
    valid = {o.value for o in CallOutcome}
    assert set(CLASSIFIER_OUTCOMES) <= valid
    assert len(CLASSIFIER_OUTCOMES) == 12


def test_rules_file_lists_every_classifier_outcome():
    rules = RULES_PATH.read_text(encoding="utf-8")
    for outcome in CLASSIFIER_OUTCOMES:
        assert outcome in rules


def test_transcript_is_formatted_with_speakers():
    text = format_transcript(TALK)
    assert text == "Agent: Hi, this is Amelia from Clover Posivex.\nOther party: Yes, speaking."


@pytest.mark.asyncio
async def test_classifier_result_is_used_for_a_normal_call():
    result = await resolve_call_outcome(TALK, CallSignals(), fake_classifier)
    assert (result.outcome, result.source) == ("callback_set", "classifier")
    assert result.summary == "A short call."


@pytest.mark.asyncio
async def test_forced_outcome_wins_without_calling_the_classifier():
    signals = CallSignals(forced_outcome="voicemail", forced_rationale="AMD: machine-vm")
    result = await resolve_call_outcome(TALK, signals, failing_classifier)
    assert (result.outcome, result.source) == ("voicemail", "forced")


@pytest.mark.asyncio
async def test_no_caller_speech_is_hangup_no_speak():
    transcript = [{"role": "agent", "message": "Hi, this is Amelia."}]
    result = await resolve_call_outcome(transcript, CallSignals(), fake_classifier)
    assert result.outcome == "hangup_no_speak"


@pytest.mark.asyncio
async def test_classifier_failure_falls_back_to_other_or_do_not_call():
    result = await resolve_call_outcome(TALK, CallSignals(), failing_classifier)
    assert (result.outcome, result.source) == ("other", "fallback")
    result = await resolve_call_outcome(TALK, CallSignals(do_not_call_requested=True), failing_classifier)
    assert result.outcome == "do_not_call"
