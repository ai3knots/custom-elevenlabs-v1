# Agent Call Analysis: 5 Recent Calls (4 Nov 2026)

Source: LiveKit Cloud Agent Insights exports in `recent-calls/` (chat history, logs, traces, metrics for 5 sessions).
All five calls went to the same destination number (+19432094556) between 12:35 and 14:01 UTC.

**Not covered:** the `.oga` audio files were not listened to. Everything below comes from transcripts, logs, spans and metrics, so audio-quality problems (voice, clipping, echo) are not assessed.

## 1. Summary

Telephony is working. The SIP leg connects, the callee answers, speech-to-text, TTS and the state machine all run. The problems are in the agent's own logic, and they cost real conversations:

| # | Problem | Calls hit | Impact |
|---|---|---|---|
| 1 | Knowledge-base file missing from the Docker image; every transition to `conversation` crashes | 2 of 2 that reached it | Call dies or goes silent right after "I'm the owner" |
| 2 | "This call is being recorded" is treated as a voicemail; the agent hangs up on a live person | 2 of 5 | Live human lost, call wrongly logged as `automated_menu` |
| 3 | After a terminal `transition`, the LLM returns empty replies and the framework retries for 10-12 s | 2 of 5 | Callee hears nothing; their "Yes." is ignored |
| 4 | 5-13 s of silence between pickup and the first agent audio | 4 of 4 that spoke | Callee says "Hello?" into dead air |
| 5 | The LLM and the scripted opener both speak (double greeting, agent cuts itself off) | 3 of 5 | Confusing, unprofessional opening |
| 6 | Turn latency about 2.0 s average, up to 5.8 s | all | Feels slow for a phone call |
| 7 | Agent does not close the call itself | 1 of 1 completed | 13 s of silence, outcome recorded late |

Outcomes recorded: `owner_details_captured` x1 (the only call that completed properly), `automated_menu` x2 (both wrong, a live person was on the line), `unknown_dropped` x2 (both caused by problem 1).

## 2. Per-call results

| Time (UTC) | Call ID | Length | Recorded outcome | What actually happened |
|---|---|---|---|---|
| 12:35 | conv_9a0de8dfe7e645 | 131 s | `owner_details_captured` | Worked end to end. First audio 12.8 s after pickup; two openers collided; 13 s of dead air before closing. |
| 13:08 | conv_0bcffca47ae242 | 33 s | `automated_menu` (wrong) | Model ended the call on "This call is being recorded." The agent never spoke a word. |
| 13:13 | conv_a57ba1daef0641 | 60 s | `unknown_dropped` | Callee said "I'm the owner" at 40 s. The knowledge-base error fired. Callee hung up at 55 s. |
| 13:20 | conv_e529e2759ced4c | 50 s | `unknown_dropped` | Same knowledge-base error, then an illegal repeat transition, then silence. Callee hung up at 44 s. |
| 14:01 | conv_59b23afce8b846 | 28 s | `automated_menu` (wrong) | Model ended the call; the scripted greeting still played afterwards; the callee's "Yes." was ignored. |

## 3. Findings

### 3.1 Knowledge-base file missing, so `transition("conversation")` crashes (critical)
Log in two sessions (13:13 and 13:20):

```
FileNotFoundError: Knowledge base file not found at:
/app/reference/elevenlabs-agent-config/agent-v5-Amelia/Knowledge-base.md
  persona_agent.py:226 _apply_state -> manager.py:82 render_prompt -> kb.py:50 load_kb
```

- Cause: `.dockerignore` excludes `reference/`, so the file is not in the image.
- The state machine commits the transition *before* the prompt is rendered, so a render failure leaves the FSM in `conversation` while the agent still runs the opener instructions. The model retries the transition and gets `Illegal transition from 'conversation' to 'conversation'` (13:20 call).
- Result: in 13:13 the agent improvised a pitch and the callee hung up; in 13:20 it went silent for about 5 s and the callee hung up.
- Every successful run to the `conversation` state will fail the same way, so this blocks the main purpose of the agent.
- The 12:35 call only worked because `reach_owner` does not load the knowledge base.

### 3.2 A recording notice is misclassified as a machine
- The answering-machine detector (LiveKit `AMD`, which reports `reason: llm`) returned `human` in all five calls, correctly.
- But the opener prompt ([prompts/nodes/node_1_opener.md](prompts/nodes/node_1_opener.md), lines 11-13, 31, 42) tells the LLM that any "recorded or carrier message" or "call screener" is a machine and to call `set_call_outcome("automated_menu")` plus `transition("end")`.
- The callee's phone plays "This call is being recorded." before connecting. In 2 of 5 calls the LLM treated that as a machine and ended the call; in the other 3 it did not, so the behavior is also inconsistent run to run.
- Real prospects with call-recording notices, or call screeners that later connect to a human, will hit the same path.

### 3.3 Terminal transition produces empty LLM replies and a 10-12 s hang
In the two misclassified calls, after the model called `transition("end")`:
- The framework requests a follow-up reply. Gemini returns `finish reason: STOP` with no text (`no response generated`).
- The fallback adapter treats an empty reply as a failure and retries about 11 times over 10-12 s (`all LLMs are unavailable, retrying..`), logging `ERROR`.
- Both LLMs in the fallback list are the same model (`gemini-3.5-flash`), so the "fallback" cannot help.
- During this time the live person said "Yes." (13:08 and 14:01 sessions). The log shows `skipping user input, speech scheduling is paused`. In the 13:08 call a TTS attempt also failed (`no audio frames were pushed for text: "!"`), so the agent never spoke.
- [src/agent/persona_agent.py:211-223](src/agent/persona_agent.py#L211-L223) already handles the end state by shutting the session down, but the follow-up LLM turn runs anyway.

### 3.4 Dead air after pickup
Seconds from SIP answer to first agent audio and to AMD completing:

| Call | Callee first speaks | AMD done | First agent audio |
|---|---|---|---|
| 12:35 | +1.4 s | +11.9 s | +12.8 s |
| 13:08 | +1.4 s | +3.8 s | never |
| 13:13 | +1.1 s | +5.1 s | +5.4 s |
| 13:20 | +1.9 s | +7.3 s | +8.3 s |
| 14:01 | +1.5 s | +7.7 s | +8.2 s |

- The callee says "Hello?" within 2 seconds; the agent's scripted greeting is held until AMD finishes ([src/agent/session_handler.py:512-547](src/agent/session_handler.py#L512-L547)).
- AMD waits for the whole utterance sequence (the notice, a pause, then "Hello?"), so it ran 18 s in the 12:35 call.
- A human caller will typically give up or repeat "Hello?" after 3-4 s of silence.

### 3.5 Two openers at once
- `session.start()` runs before AMD, so the agent's LLM is live while AMD is still listening. When the callee speaks, the agent generates its own reply.
- Evidence: in the 12:35 call an `agent_turn` started at 11.3 s while the `amd` span ran until 19.1 s. Chat history shows an LLM-written opener, then a second scripted opener ("Hi, this is Amelia from Clover Posivex. Am I speaking...") cut off mid-sentence (`interrupted: true`).
- Same pattern in the 13:13 and 13:20 calls (several half-spoken openers).
- The opener prompt says "The first message has already been spoken", which is false at that moment.

### 3.6 Latency
From 26 turns:

| Stage | Typical | Worst |
|---|---|---|
| End-of-turn delay (VAD) | 0.58 s | 0.77 s |
| LLM first token | 1.0-1.8 s | 1.9 s |
| TTS first byte | 0.3 s | 0.62 s |
| End-to-end per turn | 2.0 s average | 5.8 s |

- The LLM is the biggest contributor. Some turns also show two overlapping LLM requests in the spans (cause not determined).
- The 5.3 s and 5.8 s worst cases coincide with turns that also made tool calls.
- Turn detection is currently VAD only (constant 0.577 s delay). See the earlier discussion of LiveKit turn detector v1.

### 3.7 The agent does not close the call itself
In the 12:35 call the agent finished with "Thanks so much for your help!" at 92 s and then waited. The outcome and the `close` transition came only after the callee said "Goodbye" at about 110 s, 13-19 s later. The closing line was then cut by the callee hanging up. The call ended by the callee disconnecting, not by the agent.

### 3.8 Smaller issues
- **Event loop stalls at call start:** 360-650 ms blocks from `ssl.create_default_context`, an SSL handshake and `sqlalchemy`/`asyncpg` imports. The 14:01 call also logged `VAD inference is slower than realtime`. These delay audio at the moment the callee is first speaking.
- **AMD LLM warning:** `gemini-3.5-flash` is not benchmarked for AMD, so classification quality is unverified.
- **STT confidence on short phone utterances:** 0.64-0.85 for "Hello? Is there... There...", "Yes." This is normal for 8 kHz audio but worth watching.
- **Harmless noise:** ElevenLabs `similarity_boost` ignored for `eleven_v3_conversational`.
- **Fallback LLM list:** both entries are the same model, so it adds retries without resilience.
- **Data hygiene:** `recent-calls/` contains phone numbers and conversation text and is untracked but not in `.gitignore`.

## 4. What is working
- SIP signaling, answer detection and hang-up through LiveKit Cloud are reliable (5 of 5 answered).
- AMD classified all five callees as `human`, which was correct.
- The one fully working call (12:35) produced a coherent conversation, captured the owner's name, best time and number, and recorded the correct outcome.
- Tool calls (`set_call_outcome`, `transition` to `reach_owner`/`close`) execute in 20-200 ms.
- TTS first-byte latency is good (about 0.3 s).

## 5. Recommended fixes, in priority order

1. **Ship the knowledge base in the image.** Remove `reference/` from `.dockerignore`, or move `Knowledge-base.md` to a path that is copied. Also make a missing KB degrade to "no KB" instead of crashing.
2. **Make state transitions atomic.** Render and apply the new prompt before committing the FSM transition, or roll the FSM back on failure.
3. **Stop treating a recording notice as a machine.** Reword the opener prompt (lines 11-13, 31, 42) so "this call is being recorded" alone, without a voicemail/menu cue, is not a machine. Prefer using the AMD result as the single source and keep the LLM from ending the call on its own in the opener.
4. **Prevent the empty-reply retry loop.** When the model calls `transition("end")` or `dnc_close`, make that tool not request a follow-up reply (as `skip_turn` does by returning `None`), or treat an empty reply as success in the end state. Fix the fallback list so the second model differs.
5. **Remove the dead air and double opener.** Speak the first message immediately on pickup (or after a short 1-1.5 s AMD grace) and keep the LLM from replying until the scripted greeting has played. Fix the prompt wording that says the first message is already spoken.
6. **Cut latency.** Try the lower-latency model options and the LiveKit audio turn detector (already added behind `TURN_DETECTOR_MODE`); check why some turns show two overlapping LLM requests.
7. **Make the agent end calls itself.** After the closing line, set the outcome and call `transition("end")` in the same turn instead of waiting for the callee to say goodbye.
8. **Reduce start-up stalls.** Prewarm the SSL context and the database imports, and watch CPU on the host (the VAD slower-than-realtime warning).
9. **Add `recent-calls/` to `.gitignore`.**

## 6. How to confirm the fixes
Repeat the same scripted test: (a) a callee who says "Hello?" immediately; (b) a callee whose phone plays a recording notice first; (c) a callee who confirms "I'm the owner". Success criteria: first agent audio within about 2 s of pickup, a single opener, no `FileNotFoundError`, no `all LLMs are unavailable`, the agent ends the call itself, and the outcome matches the actual conversation.

## 7. Implementation status

All nine fixes in section 5 were implemented on branch `feat/livekit-cloud-sip` and covered by unit tests (102 tests pass). They have not yet been verified on a live call; use the test in section 6 after deploying.

| Fix | What was done | Where |
|---|---|---|
| 1 Knowledge base in image | Moved to `prompts/knowledge/Knowledge-base.md`; a missing or unreadable file is logged and the call continues without it | `src/prompts/kb.py`, `src/prompts/manager.py`, `config/defaults.yaml` |
| 2 Atomic transitions | `validate_transition` first, new prompt built before the FSM is committed, FSM rolled back if applying the state fails | `src/fsm/machine.py`, `src/agent/persona_agent.py` |
| 3 Recording notice | Opener prompt reworded; AMD verdict `human` blocks the LLM from ending the opener as a machine unless the callee actually said a voicemail/menu phrase | `prompts/nodes/node_1_opener.md`, `src/agent/persona_agent.py` |
| 4 Empty-reply loop | `transition(end/dnc_close)` and `set_call_outcome` return `ToolResult(reply_required=False)`; the fallback model now differs (`gemini-3.5-flash-lite`) | `src/agent/persona_agent.py`, `src/config.py` |
| 5 Dead air and double opener | AMD hard-capped at `amd_timeout_seconds` (5 s) from pickup; LLM replies are held until the scripted opener is spoken; held turns still saved to history and the transcript | `src/agent/session_handler.py`, `src/agent/persona_agent.py` |
| 6 Latency | The "two overlapping LLM requests" are LiveKit's preemptive generation (on by default, up to 3 attempts per turn, discarded if the transcript changes); not a bug. Model overrides via env; `TURN_DETECTOR_MODE=v1` available | `src/config.py` |
| 7 Agent ends the call | Wrap-up steps call outcome + `close` in the same turn; in `close` the call ends once the closing line has played, with a 25 s safety timeout | `prompts/nodes/node_2_*.md`, `node_4_*.md`, `src/agent/session_handler.py` |
| 8 Start-up stalls | Per-process prewarm: imports, TLS context and Silero VAD model load before a call arrives | `src/agent/session_handler.py` |
| 9 `.gitignore` | `recent-calls/` ignored | `.gitignore` |

Known trade-offs and things to watch on the live test:
- The 5 s AMD cap means a long voicemail greeting may be classified as `uncertain`; the opener prompt still ends the call on explicit voicemail phrases.
- Wrap-up steps no longer wait for the callee's reply, so a correction spoken after "I've got everything noted" is not captured.
- `gemini-3.5-flash-lite` as the fallback is a name already used elsewhere in the codebase but was not exercised in these traces.
- Prewarm reuses one Silero VAD per process only if its settings match the call's; otherwise it loads one as before.
