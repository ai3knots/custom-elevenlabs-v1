# Single-prompt agent: research and migration plan

Branch: `feat/single-prompt-agent` (from `feat/livekit-cloud-sip`).
Reference agent: ElevenLabs `agent_2701m6e2fw2eehd97r93d4v8x9yc` ("Posivex - 6 - Amelia - Single Master Prompt"), saved in
`reference/elevenlabs-agent-config/agent_2701m6e2fw2eehd97r93d4v8x9yc/`.

## 1. How the reference agent works

- No workflow. One prompt (28,981 chars, about 7.2k tokens) with Steps A to F: voicemail check, who is on the line, staff, owner (five beats),
  lock-in, close. The model is told to "silently work out which step you are in" before every reply.
- Three system tools only: `end_call` (called in the same turn as the closing line), `skip_turn`, `voicemail_detection`.
  There is no `transition` and no `set_call_outcome`.
- The call outcome is decided after the call by an LLM that reads the transcript (data-collection field `call_outcome`, 12 values,
  ordered judging rules). The rules are in `platform_settings.json` and are reused verbatim.
- Knowledge base is retrieved per turn (RAG); it is the same document as `prompts/knowledge/Knowledge-base.md`.
- Settings: Claude Haiku 4.5 (we keep Gemini 3.5 Flash), turn model `turn_v3`, max duration 600 s, silence ends the call after 15 s,
  background-voice detection on, first message interruptible, TTS stability 0.9 / similarity 0.89.

## 2. What LiveKit documents (docs and installed 1.8.10 source)

- Workflows guide: "Start with a single agent and a small set of tools." Split into handoffs or tasks only when instructions grow too large,
  tools conflict, or a step needs its own loop. The prompting guide is more cautious and says most use cases are more consistent when split.
- Prompt structure: identity, output formatting for TTS, tools, goals, guardrails, user information. The reference prompt already follows it.
- A function tool that returns `None` completes silently with no follow-up LLM reply.
- `EndCallTool(end_instructions=None)` (`livekit/agents/beta/tools/end_call.py`) shuts the session down once the turn that called it has finished
  playing, which is exactly "closing line plus `end_call` in the same turn"; `delete_room=True` hangs up the SIP leg.
- `max_tool_steps` (default 3) caps consecutive tool calls per LLM turn.
- Outbound telephony: voicemail answers at the SIP layer, so it must be handled with AMD; AMD example is `examples/telephony/amd.py`.

## 3. Decisions (confirmed)

| # | Decision |
|---|---|
| 1 | LLM: Gemini 3.5 Flash (primary and conversation), `gemini-3.5-flash-lite` fallback |
| 2 | Michael: same architecture and prompt, name changes only |
| 3 | English only (the Spanish block and `es` branches are dropped) |
| 4 | Outcome only from a post-call classifier. The agent has no outcome tool |
| 5 | Silence follows the reference agent: after `silence_end_call_seconds` (15 s) of caller silence the call ends. `skip_turn` suspends it for `hold_duration_seconds` |

## 4. Architecture after the migration

- One `PersonaAgent` with one instruction string: the ElevenLabs prompt rendered with the dynamic variables, then the knowledge base.
- Tools: `end_call(reason)`, `skip_turn()`, `voicemail_detection(reason)`.
- `end_call` is built on the SDK `EndCallTool` semantics (shutdown after the turn's speech finishes). A guard refuses it when the model has spoken
  nothing this turn, except for the reasons `voicemail`, `silence` and `do_not_call`. With reason `do_not_call` the exact apology is spoken by code
  if the model did not already say it.
- Post-call classifier (`src/analysis/outcome.py`): one structured Gemini call over the transcript returns `{call_outcome, rationale, summary}`.
  Deterministic rules run first: no caller speech gives `hangup_no_speak`; the `voicemail_detection` tool or an AMD machine verdict gives
  `voicemail` / `automated_menu`; classifier errors fall back to `other`.
- Unchanged: SIP dialing, AMD (3 s cap), scripted uninterruptible opener, reply gate, long-turn guard, backchannel filter, persistence,
  webhook outbox and payload shape, cost tracking.

## 5. What is removed

- `src/fsm/` and `tests/test_fsm.py`.
- `prompts/nodes/`, `prompts/{amelia,michael}/global.j2` (replaced by `prompts/{persona}/system.j2`).
- `transition`, `set_call_outcome`, `_apply_state`, model switching and thought-signature sync in `PersonaAgent`.
- Close watch, safety timeout, state-transition events, per-state endpointing and FSM outcome resolution in `session_handler.py`.
- `llm.conversation_model` / `LLM_CONVERSATION_MODEL`, Spanish prompt handling.

## 6. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Gemini follows a long implicit state machine less reliably than the tuned model | Scripted scenario checklist (section 8); compare against the recorded calls; roll back to `feat/livekit-cloud-sip` |
| Model emits `end_call` without a closing line, or ends the call on "okay" | `end_call` guard; prompt wording kept verbatim |
| Do-not-call apology paraphrased | Code speaks the exact apology when `reason="do_not_call"` and it was not said |
| Outcome accuracy now depends on the classifier | Golden tests from `recent-calls` transcripts; deterministic overrides; `other` on failure |
| More tokens per turn (about 7.2k prompt + 2.5k KB) | Static text first so provider prompt caching applies; no transition round trip any more |
| One endpointing setting for all phases (lock-in used to wait up to 6 s for dictated emails) | `max_endpointing_delay` raised to 4 s; `TURN_DETECTOR_MODE=v1` available to test |
| `voicemail_detection` misfire on a recording notice | Tool refuses when AMD said "human" and no machine cue was heard (kept from the current guard) |
| Per-state telemetry disappears | Transcript and tool calls remain in Agent insights |

## 7. Step-by-step execution plan

1. Save the reference agent and this plan on the branch. Commit.
2. Prompt: create `prompts/{amelia,michael}/system.j2` from the ElevenLabs prompt (variables `{{business_name}}` etc. unchanged); remove the
   Spanish block; Michael differs by name only. Keep the knowledge base appended by `PromptManager`.
3. `PromptManager`: single `render_prompt(variables)`; English first message only; drop the state argument.
4. Config: remove `conversation_model`; add `silence_end_call_seconds`; `max_endpointing_delay` 4 s; drop `max_silence_count`; update `defaults.yaml`, env docs.
5. Outcome classifier: `src/analysis/outcome.py` with the ElevenLabs rules, structured output, deterministic overrides, and tests.
6. New `PersonaAgent`: tools `end_call`, `skip_turn`, `voicemail_detection`; reply gate, backchannel filter, machine-claim guard; `CallState` dataclass replaces the FSM.
7. `session_handler.py`: remove FSM/close-watch plumbing; wire silence ending, hold, end-of-call outcome classification before persistence; keep AMD/opener/guards.
8. Delete `src/fsm/`, `prompts/nodes/`, `tests/test_fsm.py`, dead config and docs; fix imports in `recorder`, `calls.py`, `prometheus.py`.
9. Tests: rewrite agent and prompt tests, add classifier and `end_call` guard tests; run `pytest` and `ruff`.
10. Update `RUNBOOK.md`, `CONTEXT_NOTES.md`; commit in logical commits.
11. Live verification (after deploy): scenarios from section 8, then compare an Agent insights export with `recent-calls/`.

## 8. Live scenario checklist

1. Immediate "Hello?" then "Yes, I'm the owner": opener once, five beats in order, demo ask, lock-in, close, agent hangs up.
2. Recording notice, then "Hello?" repeated: opener plays once, uninterrupted.
3. Employee answers, owner absent: four details collected, "I've got everything noted for the owner", hang up.
4. "Stop calling me": exact apology, hang up, outcome `do_not_call`.
5. Voicemail greeting: silent hang-up, outcome `voicemail`.
6. "Hold on" then silence for 20 s: agent stays quiet, then continues.
7. Prolonged silence after the opener: call ends after 15 s, outcome `no_answer` or `other`.
8. "I'm busy, call me Saturday at 6": callback locked in, outcome `callback_set`.
