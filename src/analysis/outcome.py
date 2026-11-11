"""Post-call outcome classifier.

The agent no longer records an outcome during the call. After it ends, one structured Gemini call reads
the transcript and picks the outcome using the same ordered rules as the ElevenLabs `call_outcome`
data-collection field (`prompts/analysis/call_outcome.md`). A few facts the transcript cannot show
(answering-machine verdict, the voicemail tool, nobody ever spoke) are applied first.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("posivex.analysis")

# The values the CRM understands from the classifier (hangup_no_speak / unknown_dropped are fallbacks only).
CLASSIFIER_OUTCOMES: tuple[str, ...] = (
    "voicemail",
    "no_answer",
    "automated_menu",
    "employee_no_owner_details",
    "owner_details_captured",
    "callback_set",
    "info_email",
    "email_comparison",
    "consultant_call_scheduled",
    "declined",
    "do_not_call",
    "other",
)

RULES_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "analysis" / "call_outcome.md"
CLASSIFIER_TIMEOUT_SECS = 20.0


@dataclass
class OutcomeResult:
    outcome: str
    rationale: str
    summary: str | None = None
    source: str = "classifier"  # classifier | forced | no_speech | fallback


@dataclass
class CallSignals:
    """Facts known to the runtime that override or inform the transcript classifier."""

    forced_outcome: str | None = None
    forced_rationale: str = ""
    voicemail_tool_called: bool = False
    do_not_call_requested: bool = False
    greeting_verdict: str | None = None
    notes: list[str] = field(default_factory=list)


Classifier = Callable[[Sequence[Mapping[str, Any]], CallSignals], Awaitable[OutcomeResult]]


def format_transcript(transcript: Iterable[Mapping[str, Any]]) -> str:
    lines = []
    for turn in transcript:
        speaker = "Agent" if turn.get("role") == "agent" else "Other party"
        text = str(turn.get("message", "")).strip()
        if text:
            lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


def replied_to_opener(transcript: Iterable[Mapping[str, Any]]) -> bool | None:
    """Whether the callee said anything after the agent's first line (the scripted opener).

    True or False when the agent spoke, None when it never did (so there was no opener to reply to). Callers
    that only care whether a reply happened can treat None like False.
    """
    seen_agent = False
    for turn in transcript:
        text = str(turn.get("message", "")).strip()
        if not text:
            continue
        if turn.get("role") == "agent":
            seen_agent = True
        elif seen_agent:
            return True
    return False if seen_agent else None


def _load_rules() -> str:
    return RULES_PATH.read_text(encoding="utf-8")


def _make_client(api_key: str) -> Any:
    from google.genai import Client

    return Client(api_key=api_key)


async def classify_transcript(
    transcript: Sequence[Mapping[str, Any]],
    signals: CallSignals,
    *,
    api_key: str | None,
    model: str,
    timeout: float = CLASSIFIER_TIMEOUT_SECS,
) -> OutcomeResult:
    """Ask Gemini for the outcome. Raises on any failure; `resolve_call_outcome` handles fallbacks."""
    if not api_key:
        raise RuntimeError("No Gemini API key for the outcome classifier.")

    # Building the client loads the CA bundle (~150 ms of CPU) and this runs on the agent's event loop, so
    # it goes to a thread instead of stalling the loop (LiveKit logs "event loop blocked" otherwise).
    client = await asyncio.to_thread(_make_client, api_key)
    from google.genai import types

    schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "call_outcome": types.Schema(type=types.Type.STRING, enum=list(CLASSIFIER_OUTCOMES)),
            "rationale": types.Schema(type=types.Type.STRING),
            "summary": types.Schema(type=types.Type.STRING),
        },
        required=["call_outcome", "rationale", "summary"],
    )
    system = (
        "You classify a finished outbound sales phone call. Return JSON with `call_outcome`, `rationale` "
        "(one short sentence with the facts: names, emails, times agreed) and `summary` (two or three "
        "sentences describing the call).\n\n"
        + _load_rules()
    )
    context_lines = list(signals.notes)
    if signals.greeting_verdict:
        context_lines.append(f"Greeting check verdict (who answered): {signals.greeting_verdict}.")
    if signals.voicemail_tool_called:
        context_lines.append("The agent used the voicemail_detection tool.")
    if signals.do_not_call_requested:
        context_lines.append("The agent ended the call with the do-not-call apology.")
    contents = "Transcript:\n" + format_transcript(transcript)
    if context_lines:
        contents += "\n\nSystem notes:\n" + "\n".join(f"- {line}" for line in context_lines)

    response = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0.0,
            ),
        ),
        timeout=timeout,
    )
    data = json.loads(response.text or "{}")
    outcome = str(data.get("call_outcome", "")).strip().lower()
    if outcome not in CLASSIFIER_OUTCOMES:
        raise ValueError(f"Classifier returned an unknown outcome: {outcome!r}")
    return OutcomeResult(
        outcome=outcome,
        rationale=str(data.get("rationale", "")).strip(),
        summary=str(data.get("summary", "")).strip() or None,
        source="classifier",
    )


async def resolve_call_outcome(
    transcript: Sequence[Mapping[str, Any]],
    signals: CallSignals,
    classifier: Classifier,
) -> OutcomeResult:
    """Final outcome for a call: runtime facts first, then the classifier, then a safe fallback."""
    if signals.forced_outcome:
        return OutcomeResult(
            outcome=signals.forced_outcome,
            rationale=signals.forced_rationale or signals.forced_outcome,
            source="forced",
        )

    if not any(t.get("role") == "user" and str(t.get("message", "")).strip() for t in transcript):
        return OutcomeResult(
            outcome="hangup_no_speak",
            rationale="Call ended without the callee speaking.",
            source="no_speech",
        )

    if not replied_to_opener(transcript):
        # A greeting, a question or a hold request before our opening line, then nothing: the callee left (or
        # went quiet) without answering us. Labelled "other" this would count as a conversation in lead-gen,
        # with an AI follow-up email; as a quick hang-up the lead is simply retried later.
        return OutcomeResult(
            outcome="hangup_no_speak",
            rationale="The callee only spoke before our opening line and never replied to it.",
            source="no_speech",
        )

    try:
        return await classifier(transcript, signals)
    except Exception as e:
        logger.exception("Outcome classifier failed: %s", e)
        if signals.do_not_call_requested:
            return OutcomeResult("do_not_call", "Do-not-call apology was delivered.", source="fallback")
        return OutcomeResult("other", "Outcome classifier unavailable.", source="fallback")
