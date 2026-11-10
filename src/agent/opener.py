"""What to do about words the callee said while our opening line was still playing.

The opener cannot be interrupted, and LiveKit skips (and logs) any callee turn that finishes while it plays.
Before, their audio was thrown away during the opener, so those words were never heard at all. Now they are
transcribed, which lets the voicemail check read a greeting within seconds. A callee who answered over the
opener ("Yes, this is the owner") would otherwise get silence, so the handler replies to it afterwards, but
only when it is clearly something to answer.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# A bare "Hello?" over our opener needs no reply: the opener already asks the question.
BARE_GREETING_WORDS = frozenset({"hello", "hi", "hey", "hallo", "yo"})
# Text is an echo of our opener when one unbroken run of its words, at least this long, makes up this share of
# it. "Yes, this is the owner" shares words with the opener but not a long run, so it is a real answer.
ECHO_MIN_RUN = 4
ECHO_SHARE = 0.8

OVERLAP_REPLY_INSTRUCTIONS = (
    "The other party spoke while your opening line was still playing, and their words are the latest message. "
    "Answer what they said in one or two short sentences and carry on with the call. Do not repeat your introduction."
)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def needs_reply(text: str, opener: str) -> bool:
    """True when `text` (said over the opener) is something the agent should still answer.

    Not a reply-worthy turn: nothing, a bare greeting ("Hello?", "Hi, hello"), or mostly our own opener
    coming back through the line as an echo.
    """
    words = _words(text)
    if not words:
        return False
    if all(word in BARE_GREETING_WORDS for word in words):
        return False
    run = SequenceMatcher(None, words, _words(opener), autojunk=False).find_longest_match().size
    return not (run >= ECHO_MIN_RUN and run / len(words) >= ECHO_SHARE)
