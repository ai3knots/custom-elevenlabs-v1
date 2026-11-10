# Call analysis, 10 Nov 2026: campaign `6af20857…` on LiveKit Cloud

Data: 29 LiveKit session exports (audio, logs, traces, metrics) plus the 53 calls lead-gen stored for our agent (46 from this campaign, 7 manual tests). The agent logs from the day were gone (the Cloud agent scales to zero), so the exports were the only source of exact timings.

## What the calls showed

| # | Problem | Evidence |
|---|---|---|
| 1 | LiveKit AMD delayed the opener and never classified | `uncertain / detection_timeout` with an empty transcript on 27 of 28 calls, ending about 2.7 s after pickup. Opener audio started a median 5.9 s after pickup, 4.6 s after the callee's first words. In 8 of 46 campaign calls the callee hung up as the opener started. |
| 2 | Machines were handled slowly, by the main LLM | 17 of 28 answered calls were voicemail or phone menus. The `voicemail_detection` tool fired a median 21.6 s after pickup (90th percentile 38.5 s). Machines used 52% of talk time and 75% of LLM calls (45 of 60). |
| 3 | After the "Hello?" nudge nothing could end a silent call | Two sessions ran 68 s and 187 s. LiveKit starts the away timer only while user and agent are both "listening"; after the nudge the user stays "away" (`agent_session.py` lines 2248 and 2303), so the timer was never re-armed. |
| 4 | A carrier message ("subscriber not available") was not recognised | The 187 s call. |
| 5 | Durations included ringing | `time_in_call_secs` and `duration_secs` counted from job start. An unanswered call showed 78 s; ring time was a median 4.5 s, 90th percentile 30 s. |
| 6 | Hold was cut short | `skip_turn` hold limit was 12 s; two campaign calls ended while the receptionist fetched the owner. |
| 7 | Labels | 10 of the 24 "voicemail" outcomes were phone menus only; one-line hang-ups were `other`. In lead-gen `voicemail` retries after 4 h and creates an AI follow-up email, `other` counts as a conversation, while `automated_menu` and `hangup_no_speak` do neither (retry after 24 h). |

Also measured: time to first audio from ElevenLabs 0.32 s, LLM time to first token median 1.21 s (90th percentile 2.05 s), end-of-turn wait median 0.58 s, about 7,500 to 10,000 input tokens per LLM call. The ElevenLabs `input_timeout_exceeded` ERROR when ringing exceeds 20 s is harmless (the connection is reused and first-audio time is unchanged).

## What changed

| Change | Where |
|---|---|
| AMD removed. A fast Gemini check (`gemini-3.5-flash-lite`) reads the streaming transcript beside the opener, asks again as words arrive, and on a confident verdict (≥ 0.8) interrupts, hangs up and labels the call `voicemail` or `automated_menu`. The `voicemail_detection` tool stays as a fallback and now says `kind="voicemail"` or `"menu"`. | `src/agent/greeting_detector.py`, `session_handler.py`, `persona_agent.py` |
| Opener starts 1.0 s after pickup, waiting at most 1.0 s more if the callee is mid-sentence. | `conversation.opener_delay_seconds`, `opener_quiet_wait_seconds` |
| Nudge re-arms the silence timer. | `src/agent/silence.py` (`nudge_and_rearm`) |
| No-speech watchdog: ends the call after 40 s with no transcribed words, even on hold music. | `silence.py` (`watch_for_silence`), `conversation.no_speech_watchdog_seconds` |
| Hold raised to 30 s. | `conversation.hold_duration_seconds` |
| Durations and transcript times count from pickup. | `src/agent/call_clock.py` |
| Timing events stored with the call: `timing_pickup`, `timing_first_callee_speech`, `timing_greeting_check`, `timing_machine_detected`, `timing_opener_started`, `timing_opener_finished`, `timing_silence_nudge`, `timing_no_speech_watchdog`, `timing_call_end`. | `session_handler.py` (`mark`) |
| Labels: a callee who only spoke before our opener and never replied is `hangup_no_speak`; a menu is `automated_menu`. | `src/analysis/outcome.py` (`replied_to_opener`) |

## Offline check of the new detector

The same checker was run on the real callee greetings from the 46 campaign calls (37 had callee speech), asking again with more turns when unsure:

- Latency per check: median 0.86 s, 90th percentile 1.0 s, maximum 1.23 s.
- All 9 mailbox greetings were `voicemail` and all 10 menus or auto-attendants were `menu`.
- No live person was labelled a machine. One call screener ("Before I try to connect you, could you give me a description of why you're calling") was labelled `menu`; it is ambiguous. Two greetings that only turned into menus later were `human` on the first words and are caught on the next look.

This is offline, on transcripts. The live effect (how soon a machine is hung up on) and the false-positive rate on real calls still need a test round.

## Test round

1. A number that goes to voicemail: expect a hang-up within a few seconds of the greeting, outcome `voicemail`.
2. A business with a phone menu: expect `automated_menu`.
3. Answer "Hello?": the opener should start about 1 to 2 s after pickup and the conversation should proceed.
4. Answer and stay silent: "Hello?" about 15 s after the opener, then the call ends about 15 s later as `hangup_no_speak`.
5. Say "hold on a second" and go quiet: the agent holds about 30 s.
6. Hang up right after "Hello": `hangup_no_speak`.

Read the timings on the server: `SELECT event_type, payload FROM call_events WHERE call_id = 'lk_…' AND event_type LIKE 'timing_%' ORDER BY timestamp;`. The outcome rationale of a machine hang-up contains the first 200 characters of what was heard, so false positives are easy to spot in lead-gen.

## Follow-up: first test call on the new build (`lk_65ffa21de3214a`, 216 s, `callback_set`)

The fixes above worked: the opener was requested 1.26 s after pickup (audible at 1.58 s, it was 5.9 s), the timing events were stored, the greeting check ran twice (human, 1.1 to 1.4 s each) and the outcome was right. The call was slow to respond, though: median 3.6 s from the callee stopping to the agent's audio (8 of 11 turns over 3 s).

| Cause | Evidence | Change |
|---|---|---|
| End-of-turn wait reached the 4.0 s maximum on short replies | 4 of 11 turns ("Hello.", "Yes. I am.", "Yes.") at about 4.3 s each; the turn detector rated them unlikely to be finished (p 0.01 to 0.36 against a 0.386 threshold) | `max_endpointing_delay` 4.0 to 1.5. Estimated median gap 3.6 s to 2.3 s. Risk: a caller dictating an email with long pauses can be cut off. |
| The callee's audio was discarded while the opener played | First speech at +1.95 s, first transcript at +9.7 s. LiveKit's `discard_audio_if_uninterruptible` defaults to true, so the greeting check could not read anything until the opener ended | `discard_audio_if_uninterruptible: false`, plus a reply to a real answer given over the opener (`src/agent/opener.py`) |
| Gemini time to first token | median 1.47 s, mean 2.28 s, one 6.9 s spike, about 11,400 input tokens per request, 68% cached, 15 requests for 12 replies (preemptive generation) | Not changed yet. Options: shorter prompt, reorder for caching across calls, test Flash-Lite as the main model. |

Not slow: speech-to-text 0.21 to 0.30 s, ElevenLabs first audio median 0.29 s. The agent talked for 50% of the call (median 7.9 s per turn, up to 16.6 s).
