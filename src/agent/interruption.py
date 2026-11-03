"""Backchannel filtering: keep "mm-hmm" style acknowledgements from interrupting the agent."""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, Callable, Iterable

from livekit.agents import stt

_NON_WORD = re.compile(r"[\W_]+")


def _key(text: str) -> str:
    """Lowercase and drop punctuation and spaces so 'Mm-hmm', 'mm hmm' and 'mmhmm' compare equal."""
    return _NON_WORD.sub("", text.lower())


def build_ignore_keys(terms: Iterable[str]) -> frozenset[str]:
    """Normalise the configured ignore terms into comparison keys."""
    return frozenset(k for k in (_key(t) for t in terms) if k)


def is_ignorable(text: str, keys: frozenset[str]) -> bool:
    """True when the whole utterance is made only of ignore terms (e.g. 'okay, mm-hmm')."""
    target = _key(text)
    if not target or not keys:
        return False
    # Can the string be segmented entirely into ignore terms?
    reachable = [False] * (len(target) + 1)
    reachable[0] = True
    for i in range(len(target)):
        if not reachable[i]:
            continue
        for k in keys:
            if target.startswith(k, i):
                reachable[i + len(k)] = True
    return reachable[-1]


async def drop_backchannel_transcripts(
    events: AsyncIterable[stt.SpeechEvent | str],
    keys: frozenset[str],
    agent_is_speaking: Callable[[], bool],
) -> AsyncIterable[stt.SpeechEvent | str]:
    """Drop transcript events that are only backchannel terms while the agent is talking.

    With no transcript text the interruption check (min_words) never fires, and no user turn is
    committed. When the agent is silent the same words pass through untouched, because after a
    check question "mm-hmm" or "okay" is a real yes.
    """
    async for ev in events:
        if (
            isinstance(ev, stt.SpeechEvent)
            and ev.type
            in (stt.SpeechEventType.INTERIM_TRANSCRIPT, stt.SpeechEventType.FINAL_TRANSCRIPT)
            and ev.alternatives
            and is_ignorable(ev.alternatives[0].text, keys)
            and agent_is_speaking()
        ):
            continue
        yield ev
