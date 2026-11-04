# ElevenLabs agent `agent_2701m6e2fw2eehd97r93d4v8x9yc`: "Posivex - 6 - Amelia - Single Master Prompt"

Pulled from `GET /v1/convai/agents/{id}` on 2026-11-04 (last updated by ElevenLabs at unix 1793816019).
Reference only. Nothing here is wired into the code.

| File | Contents |
|---|---|
| `agent.json` | Complete API response (all settings) |
| `system_prompt.md` | `conversation_config.agent.prompt.prompt`, verbatim (28,981 chars, about 7.2k tokens) |
| `first_message.txt` | Spoken first message |
| `tools.json` | The three system tools |
| `platform_settings.json` | Post-call analysis (`call_outcome` data collection), guardrails, privacy, limits |
| `Knowledge-base.md` | KB document `VdUvsBylkbsLpMyUIkXP` (11,424 bytes; equals `prompts/knowledge/Knowledge-base.md` except list numbering) |
| `knowledge_base_meta.json` | KB document metadata |

## Key settings

| Area | Value |
|---|---|
| Workflow | None (start node only). All logic is in the one prompt. |
| LLM | `claude-haiku-4-5`, temperature 0.0, `cascade_timeout_seconds` 4.0, backup LLM "default" |
| ASR | `scribe_realtime`, quality high, pcm_16000 |
| Turn | mode `turn`, model `turn_v3`, eagerness `normal`, `speculative_turn` true, `turn_timeout` 7 s, `silence_end_call_timeout` 15 s |
| Interruptions | Ignore terms: mmhmm, uhhuh, uh huh, uh, mm, hmm, okay, ok, okey, i see, go on, go ahead, right, gotcha |
| VAD | `background_voice_detection` true |
| TTS | `eleven_v3_conversational`, voice `eXpIbVcVbLo8ZJQDlDnl`, stability 0.9, similarity 0.89, speed 1.0, latency opt 3, expressive mode on |
| Conversation | max duration 600 s, first message interruptible |
| First message | "Hi, this is Amelia from Clover Posivex. Am I speaking with the owner of {{business_name}}?" |
| Dynamic variables | business_name, business_context, current_datetime, is_followup, followup_notes, contact_name |
| Tools | `end_call` (not interruptible, "same turn as the closing line"), `skip_turn`, `voicemail_detection` (empty message) |
| Knowledge base | RAG on, usage `auto` (retrieved per turn, not inlined) |
| Post-call analysis | One data-collection field, `call_outcome`, 12-value enum with ordered judging rules |
| Guardrails | `focus` on; everything else off |

## How the prompt works

One prompt with Steps A to F (voicemail check, who is on the line, staff, owner five beats, lock-in, close). The model is told to
"silently work out which step you are in" before every reply. There are no state transitions or step tools. The call ends only through
`end_call`, in the same turn as the closing line. The outcome is decided after the call by an LLM reading the transcript.
