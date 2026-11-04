# CONTEXT NOTES — ElevenLabs → LiveKit Agents Migration

> **Package verified:** `livekit-agents==1.8.10` (Python 3.12, installed in scratch venv `lk_inspect_venv`).
> All signatures marked **[VERIFIED]** come from `inspect.signature()` on that package.
> **Sources:** file paths point to files in this workspace. URLs are official docs.

---

## 1. ElevenLabs Agent Summary

### 1.1 Identity & Basics

| Field | Value |
|---|---|
| Agent name | `Posivex - 5 - Amelia` |
| Agent ID | `agent_8401m61r9k8kffpvcjqxtkx3ams1` |
| Call direction | Outbound only |
| Max duration | 600 s (10 min) |
| Persona | "Amelia from Clover Posivex" — B2B payment-solutions outbound sales |
| Base LLM | `gemini-3.5-flash-lite`, `reasoning_effort: "minimal"`, `temperature: 0` |
| Backup LLMs | `gemini-2.5-flash` → `gpt-4o` → `gemini-2.5-flash-lite` → `claude-sonnet-4-5` |
| Cascade timeout | 4 s |

**Source:** [`amelia-json-config.json`](reference/elevenlabs-agent-config/agent-v5-Amelia/amelia-json-config.json) lines 2–6, 267–379

---

### 1.2 Workflow Graph (full)

```
START
  │ (unconditional)
  ▼
[Node 1: Opener & Route]  (type: override_agent, entry: wait_for_user)
  │ DNC edge [0]     → [DNC Say] → [End]
  │ No live human [1] → [End]
  │ Employee [2]     → [Node 2: Reach the Owner]
  │ Owner [3]        → [Node 3: Conversation]

[Node 2: Reach the Owner]  (type: override_agent, entry: generate_immediately)
  │ DNC edge [0]     → [DNC Say] → [End]
  │ Owner on line [1] → [Node 3: Conversation]
  │ Details/refusal [2] → [Node 5: Close]

[Node 3: Conversation]  (type: override_agent, entry: generate_immediately)
  │ DNC edge [0]     → [DNC Say] → [End]
  │ Agreed next step [1] → [Node 4: Lock In Next Step]
  │   (backward_condition on same edge) → [Node 3: Conversation]

[Node 4: Lock In Next Step]  (type: override_agent, entry: generate_immediately)
  │ DNC edge [0]     → [DNC Say] → [End]
  │ Hesitates / question [backward] → [Node 3: Conversation]
  │ Confirmed / declines [1] → [Node 5: Close]

[Node 5: Close]  (type: say, prompt mode)
  │ (unconditional) → [End]

[DNC Say]  (type: say, literal)
  │ (unconditional) → [End]

[End]  (type: end, outcome: success)
```

**Edge condition types:** All active edges use `type: "llm"` (LLM evaluates natural language condition). Two unconditional edges (Close→End, DNC→End).

**Key architectural rule:** `end_call` tool is disabled on all nodes. A call can only end by reaching the End node. This prevents premature hang-ups.

**Backward condition:** The 3→4 edge also carries a `backward_condition` that loops 4→3 when prospect asks new objections. ElevenLabs enforces only one edge per pair of nodes, so the loop uses this mechanism.

**Source:** [`amelia-json-config.json`](reference/elevenlabs-agent-config/agent-v5-Amelia/amelia-json-config.json) lines 851–1313; [`Workflow-spec.md`](reference/elevenlabs-agent-config/agent-v5-shared/Workflow-spec.md)

---

### 1.3 Node Detail

| # | Label | Type | Entry | LLM | Turn eagerness | Turn timeout | Tools |
|---|---|---|---|---|---|---|---|
| 1 | Opener & Route | `override_agent` | `wait_for_user` | `gemini-3.5-flash` | `eager` | ~6 s | `voicemail_detection`, `skip_turn` |
| 2 | Reach the Owner | `override_agent` | `generate_immediately` | `gemini-3.5-flash` | (inherited) | ~8 s | `voicemail_detection`, `skip_turn` |
| 3 | Conversation | `override_agent` | `generate_immediately` | `gemini-3.5-flash` (node override) | `normal` | ~5–6 s | `skip_turn`, **KB attached** |
| 4 | Lock In Next Step | `override_agent` | `generate_immediately` | `gemini-3.5-flash` | `patient` | 10–12 s | `skip_turn` |
| 5 | Close | `say` (prompt mode) | n/a | fast (inherited) | n/a | n/a | none |
| DNC | Do-Not-Call | `say` (literal) | n/a | n/a | n/a | n/a | none |
| End | — | `end` | n/a | n/a | n/a | n/a | n/a |

**Node prompt layering:** Each `override_agent` node's `additional_prompt` is **appended** to the global system prompt at runtime. Full conversation history carries across all node transitions.

**Knowledge base:** Attached to **Node 3 only** (`auto` usage mode, RAG enabled).

**Source:** [`amelia-json-config.json`](reference/elevenlabs-agent-config/agent-v5-Amelia/amelia-json-config.json) lines 1040–1309; [`README.md`](reference/elevenlabs-agent-config/agent-v5-shared/README.md) lines 24–27

---

### 1.4 Node Prompts (summary)

| Node | Key instructions |
|---|---|
| **Global** | Persona (Amelia, Clover Posivex), environment (dynamic vars), tone rules (≤30 words/turn, one question), goal (qualify → demo → step down), guardrails (no fake prices, no `end_call`, honest AI disclosure), pricing quick reference, tool usage |
| **Node 1** | Voicemail check first (long keyword list), decide live person vs machine quickly, role question only once, `skip_turn` on hold, `voicemail_detection` on machine |
| **Node 2** | Ally to employee, four things to collect (name, time, number, email) in order, one question at a time, wrap-up trigger phrase: "Perfect, I've got everything noted for the owner" |
| **Node 3** | Five-beat cold-call flow (permission → explain → discover → connect → demo ask), objection ladder (demo → comparison email → info email → callback → permission → warm exit), never collect details here |
| **Node 4** | Collect details for agreed step (demo: name + email + platform + number + time; email: just email; callback: day + time), wrap-up trigger: "Perfect, you're all set" |
| **Node 5 (Close)** | Prompt-mode Say: one short warm close recap only confirmed commitments |
| **DNC** | Literal: "Understood, my apologies. I'll make sure we don't call this number again. Take care." |

**Sources:** [`Global-prompt.md`](reference/elevenlabs-agent-config/agent-v5-Amelia/Global-prompt.md), [`Node-1-Opener-and-Route.md`](reference/elevenlabs-agent-config/agent-v5-shared/Node-1-Opener-and-Route.md), [`Node-2-Reach-the-Owner.md`](reference/elevenlabs-agent-config/agent-v5-shared/Node-2-Reach-the-Owner.md), [`Node-3-Conversation.md`](reference/elevenlabs-agent-config/agent-v5-shared/Node-3-Conversation.md), [`Node-4-Lock-in-Next-Step.md`](reference/elevenlabs-agent-config/agent-v5-shared/Node-4-Lock-in-Next-Step.md)

---

### 1.5 Dynamic Variables

| Variable | Placeholder default (from JSON) | Usage |
|---|---|---|
| `business_name` | `"Tony's Pizza & Grill"` | First message, throughout prompts |
| `business_context` | Full business profile string | Silently informs device/industry recommendations |
| `current_datetime` | `"Tuesday, October 20, 2026 at 3:42 PM (America/New_York)"` | Node 4 (timezone for scheduling) |
| `is_followup` | `"true"` | Changes opener behavior (warm vs cold call) |
| `followup_notes` | `""` | Content for warm-call reference |
| `contact_name` | `"Zack"` | Skip asking for owner name in Node 2 |

**Source:** [`amelia-json-config.json`](reference/elevenlabs-agent-config/agent-v5-Amelia/amelia-json-config.json) lines 241–248

---

### 1.6 Tools

| Tool | Type | Nodes | Key params |
|---|---|---|---|
| `skip_turn` | Built-in system | All conversation nodes | `wait_timeout_secs: -1` (infinite wait), `omit_from_transcript: false` |
| `voicemail_detection` | Built-in system | Nodes 1 & 2 only | `voicemail_message: ""` (no message left) |

No custom webhook tools. No `end_call`. No `transfer_to_number`. No `language_detection` (despite being in built_in_tools with `null` config).

**Source:** [`amelia-json-config.json`](reference/elevenlabs-agent-config/agent-v5-Amelia/amelia-json-config.json) lines 277–418

---

### 1.7 ASR / Turn / TTS Settings

**ASR (Scribe Realtime):**
- Provider: `scribe_realtime` (ElevenLabs' own)
- Quality: `high`
- Format: `pcm_16000`
- Interruption ignore terms: 49 terms (English, Spanish, Hindi, Urdu) — filler/backchannels that must NOT interrupt

**Turn detection:**
- Mode: `turn` (ElevenLabs' own turn model v3)
- Turn timeout: 5 s (global); node overrides: Node 1 eager/6 s, Node 4 patient/10–12 s
- Silence end-call timeout: 15 s
- `speculative_turn: true` (start generating before full turn end)
- `interruption_ignore_terms`: long list of filler words (must not trigger interruption)
- `merge_with_default_ignore_terms: false` (uses ONLY the listed terms)
- Soft timeout: disabled (`timeout_seconds: -1`)
- `noisy_environment_detection: false`

**TTS:**
- Model: `eleven_v3_conversational`
- Voice ID: `eXpIbVcVbLo8ZJQDlDnl` (Amelia's voice)
- Expressive mode: `true` — this causes audio tags like `[Warmly]`, `[Patiently]` to be spoken by the voice model (not a bug; they're removed client-side by `strip_audio_tags: true` in the widget config)
- 10 suggested audio tags: Empathetically, Confidently, Warmly, Excitedly, Patiently, Enthusiastically, Seriously, Chuckles, Laughing, Sighs
- Stability: 0.5, similarity_boost: 0.75, speed: 1, optimize_streaming_latency: 3
- Text normalization: `system_prompt` mode
- Phoneme tags: `true`

**Source:** [`amelia-json-config.json`](reference/elevenlabs-agent-config/agent-v5-Amelia/amelia-json-config.json) lines 5–128

---

### 1.8 Language Presets

| Language | First message override | Prompt override |
|---|---|---|
| `en` (default) | "Hi, this is Amelia from Clover Posivex. Am I speaking with the owner of {{business_name}}?" | Global prompt |
| `es` | "Hola, soy Amelia de Clover Posivex. ¿Hablo con el propietario de {{business_name}}?" | None (global only) |
| `hi` | Hindi translation | None |
| `ur` | Urdu translation | None |

The agent auto-detects language (via ElevenLabs' built-in language detection) and switches first message. No per-language prompt override — agent handles Spanish/Hindi/Urdu via the global prompt's guardrail ("If they can't converse in English, stay warm and respectful, ask about a better time...").

**Source:** [`amelia-json-config.json`](reference/elevenlabs-agent-config/agent-v5-Amelia/amelia-json-config.json) lines 167–230

---

### 1.9 Post-Call Analysis & Data Collection

| Field | Type | Values |
|---|---|---|
| `call_outcome` | `string` (enum) | `voicemail`, `no_answer`, `automated_menu`, `employee_no_owner_details`, `owner_details_captured`, `callback_set`, `info_email`, `email_comparison`, `consultant_call_scheduled`, `declined`, `do_not_call`, `other` |

- Scope: `conversation` (whole call, not per turn)
- Analysis schedule: `on_session_end`, with 900 s idle delay for messaging channels
- Analysis LLM: `gemini-2.5-flash`
- No evaluation criteria (empty array)
- Post-call webhook: `2acbe8db8504435097ce0cad4398eac9` — fires `transcript` event in JSON format
- Topic discovery: enabled; sentiment analysis: disabled

**Source:** [`amelia-json-config.json`](reference/elevenlabs-agent-config/agent-v5-Amelia/amelia-json-config.json) lines 604–657, 705–722, 828–831

---

### 1.10 Knowledge Base

- **File:** `Knowledge-base.md` (129 lines, ~7.5 KB) — [`Knowledge-base.md`](prompts/knowledge/Knowledge-base.md)
- **RAG config:** `e5_mistral_7b_instruct` embeddings, `optional_rag_enabled: false` (always retrieves), `max_vector_distance: 0.6`, `max_retrieved_rag_chunks_count: 20`, `max_documents_length: 50000`
- **Usage mode:** `auto` (LLM decides when to query)
- **Strategies:** `cat` (concatenation), `keyword`, `semantic`, `ls` (listing)
- **Attached to:** Node 3 only

**Content sections:**
1. Company & core model (what Clover Posivex is, what they offer)
2. Processing rates (interchange-plus, debit 5¢, surcharge 3%)
3. 48-month lease-to-own pricing table (Flex $60/mo, Mini $70, Station Duo $150, NRS $150, KDS $60–80, accessories $40)
4. Outright buyout table (Flex $1,200, Mini $1,800, Station Duo $3,500, NRS $3,500)
5. Device lineup — Clover: Station Duo, Station Solo, Mini, Flex, Flex Pocket, Compact, Go, Kiosk, KDS 14"/24"; NRS: Puma, Cheetah, Panther Workstation, Panther Tablet, Self-Ordering Kiosk (retail line only, no Petro)
6. Business-type matching table (restaurant, retail, convenience, salon, mobile, payments-only)
7. Pitch facts (next steps, Consultant role, free setup)
8. Next steps & Consultant info
9. Sensitive data guardrails (no SSN/routing/DL over phone)
10. Not documented (what goes to Consultant)

---

### 1.11 Call Infrastructure & Privacy

- Recording: enabled, indefinite retention
- `prevent_subagent_loops: true` (workflow safety)
- Guardrails: focus filter ON, prompt injection ON, synthetic voice detection OFF, content moderation OFF (all categories)
- Background sound: none (source_type: null)
- Concurrency limit: -1 (unlimited), daily limit: 100,000
- Queueing: enabled, 180 s wait timeout

---

## 2. Problems / Inconsistencies in the Current Agent

### 2.1 Confirmed bugs (from README troubleshooting)
1. **Dead-end Say nodes:** First test showed "SAY node generated a response after progressing... no outgoing edges" because Close and DNC nodes lacked unconditional edges to End. Now fixed in live agent.
2. **Premature farewell:** A farewell edge from Node 3 fired on "Not interested" and ended calls on first objection. Fixed — Node 3 now has no farewell edge (only DNC + agreed-step edges).
3. **Owner identification false positive:** "Hello, how can I help you?" was incorrectly routing to Node 3 (owner confirmed). Fixed — Node 1 prompt now specifies "a business greeting is not an answer."
4. **Silence after "Mm-hmm":** Backchannel not recognized as a yes, agent waited. Fixed in Node 3 prompt: "short sounds are answers."
5. **Audio tags spoken aloud:** `[Warmly]`, `[Patiently]` were spoken by the voice model — this is by design in `expressive_mode: true` on `eleven_v3_conversational`. Widget strips them with `strip_audio_tags: true`.

**Source:** [`README.md`](reference/elevenlabs-agent-config/agent-v5-shared/README.md) lines 45–70

### 2.2 Structural issues to note for migration

1. **Voicemail via tool vs AMD:** ElevenLabs uses a system tool (`voicemail_detection`) that the LLM itself decides to call based on text recognition. LiveKit has a dedicated `AMD` class (Answering Machine Detection) that works at the audio pipeline level — more reliable, model-agnostic, and fires before any LLM turn.

2. **Edge conditions are LLM calls:** Every node transition is decided by the node's LLM evaluating a natural-language condition string. This is ElevenLabs-specific. In LiveKit there is no equivalent — the agent's own tool calls or `AgentTask.complete()` signal state transitions.

3. **`speculative_turn: true`** — partial equivalent exists in LiveKit via `preemptive_generation`.

4. **`interruption_ignore_terms` (49 terms):** ElevenLabs has a list of filler/backchannel words that suppress interruptions. LiveKit has `false_interruption_timeout` and `resume_false_interruption` but no term-based allow-list.

5. **No `end_call` on any node:** Design intent is that calls only end via End nodes. In LiveKit: equivalent is `session.shutdown()` / `ctx.api.room.delete_room()` called from a controlled location (post-call hook or AMD result handler), not from the LLM directly.

6. **`backward_condition` on edges:** ElevenLabs allows one edge to have both a forward and a backward condition (loop mechanism). No direct LiveKit equivalent — loops must be implemented as explicit `AgentTask` completions and re-entries.

7. **Node 3 has NO farewell edge** by design. In LiveKit, this translates to: Node 3 agent never calls `session.shutdown()` itself; caller hanging up ends the session naturally.

8. **`silence_end_call_timeout: 15 s`:** In LiveKit there is no built-in "end call after N seconds of silence" — must be implemented manually via `user_away_timeout` + `user_state_changed` event + a shutdown callback.

9. **Prompt references non-existent models:** The JSON references `gemini-3.5-flash-lite` and `gemini-3.5-flash`. These appear to be future/hypothetical model names (as of late 2026). The backup chain (`gemini-2.5-flash`, `gpt-4o`, etc.) is more familiar. When migrating, the actual model names for LiveKit inference must be verified against the LiveKit inference model catalog.

10. **Amelia says: "You do not end calls yourself. The workflow ends them."** — This is a prompt instruction reinforcing the graph-based routing. In LiveKit, you'll need to enforce this architecturally (never call shutdown in the agent body; only in terminal task completions).

---

## 3. LiveKit Concepts That Map to Each ElevenLabs Feature

### 3.1 Mapping Table

| ElevenLabs Feature | LiveKit Equivalent | Notes |
|---|---|---|
| Workflow graph (nodes + edges) | `AgentTask` sequence managed by `on_enter` logic + `TaskGroup` | No visual graph; routing is Python code |
| `override_agent` node | `AgentTask` subclass with its own `instructions` | Chat history preserved across tasks |
| `say` node (literal/prompt) | `session.say(text)` or `session.generate_reply(instructions=...)` called in a task's terminal step | |
| `entry_behavior: generate_immediately` | `async def on_enter(self): await self.session.generate_reply(...)` | |
| `entry_behavior: wait_for_user` | `async def on_enter(self): pass` (do nothing) | Session waits for user audio |
| `edge` (LLM condition) | Tool call in the current task (`@function_tool`) → calls `self.complete(result)` | Agent decides; no separate edge-evaluator LLM |
| `backward_condition` (loop) | `AgentTask.complete()` not called; parent re-enters task, or supervisor pattern | |
| `additional_prompt` (appended) | `AgentTask.instructions` (replaces global while task is active, OR concatenate) | Chat context carries across; instructions do NOT stack automatically |
| `skip_turn` tool | `session.say("Sure thing...", allow_interruptions=False)` + manual wait on `user_state_changed` | No built-in skip_turn; custom `function_tool` |
| `voicemail_detection` tool | `AMD` context manager (audio-level detection) | Far more reliable than text-based detection |
| `language_detection` (built-in) | `inference.STT("deepgram/nova-3", language="multi")` + custom logic | Deepgram supports multilingual; language switching requires prompt update |
| Dynamic variables (`{{business_name}}`) | `session.userdata` (typed dataclass) + f-strings in `instructions` | Injected at session start from `ctx.room.metadata` or dispatch metadata |
| Knowledge base (RAG) | Custom RAG via LlamaIndex, or inject retrieved chunks as tool results | No built-in RAG; see llamaindex-rag example |
| `data_collection` / post-call analysis | Custom shutdown callback: LLM call after session ends; write to DB/webhook | No built-in equivalent; implement in `ctx.add_shutdown_callback` |
| `call_outcome` enum | Custom dataclass field in `userdata`; set by terminal task | |
| Post-call webhook | `ctx.add_shutdown_callback` + HTTP POST | |
| Language presets (first message) | Conditional in `on_enter` based on `userdata.language` | |
| `silence_end_call_timeout: 15 s` | `user_away_timeout=15.0` + `user_state_changed` event + `session.shutdown()` | |
| Outbound SIP call | `ctx.api.sip.create_sip_participant(wait_until_answered=True)` | |
| `max_duration_seconds: 600` | Custom asyncio timer or periodic check in shutdown callback | No native param |
| Guardrails (focus, prompt injection) | Prompt engineering only; no built-in guardrail layer | |
| `speculative_turn: true` | `preemptive_generation=True` or `PreemptiveGenerationOptions` | |
| `turn_eagerness: eager/patient/normal` | `min_endpointing_delay` / `max_endpointing_delay` on `Agent` | eager ≈ low delay; patient ≈ high delay |
| `interruption_ignore_terms` | `false_interruption_timeout` + `resume_false_interruption=True` | No term list; timeout-based only |
| `eleven_v3_conversational` + expressive mode | `elevenlabs.TTS(voice_id=..., model="eleven_v3_conversational")` + `expressive=True` on session | ElevenLabs plugin supports this |
| `prevent_subagent_loops` | Python code (don't call `complete()` in a cycle unless intended) | |
| `wait_for_speech_completion` (Say node) | `await speech_handle.wait_for_playout()` on `SpeechHandle` returned by `session.say()` | |

**Sources:** Examples in `reference/examples/`; livekit-agents 1.8.10 inspection

---

### 3.2 Outbound Call Flow in LiveKit

```python
# In entrypoint:
async with AMD(session, participant_identity=participant_identity) as detector:
    await ctx.api.sip.create_sip_participant(
        api.CreateSIPParticipantRequest(
            room_name=ctx.room.name,
            sip_trunk_id=outbound_trunk_id,
            sip_call_to=phone_number,
            participant_identity=participant_identity,
            wait_until_answered=True,
        ),
        timeout=45,
    )
    result = await detector.execute()
    # result.category: "human" | "uncertain" | "machine-ivr" | "machine-vm" | "machine-unavailable"
    if result.category in ("human", "uncertain"):
        # start normal session
    elif result.category == "machine-vm":
        # leave voicemail or hang up
        ctx.shutdown("voicemail")
```

**Source:** [`amd.py`](reference/examples/telephony/amd.py)

---

### 3.3 Multi-Step Flow (AgentTask pattern) mapping to ElevenLabs nodes

```python
# ElevenLabs: 4 conversation nodes = 4 AgentTask subclasses

class OpenerTask(AgentTask):           # Node 1: wait_for_user, no on_enter reply
class ReachOwnerTask(AgentTask):       # Node 2: generate_immediately
class ConversationTask(AgentTask):     # Node 3: generate_immediately, KB as retrieved context
class LockInNextStepTask(AgentTask):   # Node 4: generate_immediately, patient endpointing

# Agent.on_enter runs the task group or first task:
class AmeliaAgent(Agent):
    async def on_enter(self):
        result = await OpenerTask()
        if result.outcome == "employee":
            result = await ReachOwnerTask(...)
        if result.outcome == "owner_ready":
            result = await ConversationTask(userdata=self.session.userdata)
            if result.next_step:
                result = await LockInNextStepTask(...)
        # terminal: call generate_reply for close, then shutdown
```

> [!IMPORTANT]
> In LiveKit, `AgentTask.complete(value)` is the ONLY way to signal transition to the next step. There is no LLM-evaluated edge condition — the agent decides via tool calls. This is a fundamental architecture change.

**Source:** [`survey/agent.py`](reference/examples/survey/agent.py); livekit-agents 1.8.10

---

## 4. Verified API Facts & Signatures

> All from **`livekit-agents==1.8.10`** — [VERIFIED]

### 4.1 `AgentSession.__init__`

```python
AgentSession(
    stt: STT | str | NOT_GIVEN = NOT_GIVEN,
    vad: VAD | None | NOT_GIVEN = NOT_GIVEN,
    llm: LLM | RealtimeModel | DuplexModel | str | NOT_GIVEN = NOT_GIVEN,
    tts: TTS | str | NOT_GIVEN = NOT_GIVEN,
    turn_handling: TurnHandlingOptions | NOT_GIVEN = NOT_GIVEN,
    stt_context_options: STTContextOptions | NOT_GIVEN = NOT_GIVEN,
    tools: list[Tool | Toolset] | NOT_GIVEN = NOT_GIVEN,
    tool_handling: ToolHandlingOptions | NOT_GIVEN = NOT_GIVEN,
    max_tool_steps: int = 3,
    userdata: Userdata_T | NOT_GIVEN = NOT_GIVEN,
    preemptive_generation: bool | PreemptiveGenerationOptions | NOT_GIVEN = NOT_GIVEN,
    min_endpointing_delay: float | NOT_GIVEN = NOT_GIVEN,
    max_endpointing_delay: float | NOT_GIVEN = NOT_GIVEN,
    false_interruption_timeout: float | None | NOT_GIVEN = NOT_GIVEN,
    turn_detection: TurnDetectionMode | NOT_GIVEN = NOT_GIVEN,
    min_interruption_duration: float | NOT_GIVEN = NOT_GIVEN,
    min_interruption_words: int | NOT_GIVEN = NOT_GIVEN,
    allow_interruptions: bool | NOT_GIVEN = NOT_GIVEN,
    resume_false_interruption: bool | NOT_GIVEN = NOT_GIVEN,
    user_away_timeout: float | None = 15.0,  # default 15s!
    ivr_detection: bool = False,
    expressive: bool | ExpressiveOptions = False,
    aec_warmup_duration: float | None | NOT_GIVEN = NOT_GIVEN,
    tts_text_transforms: Sequence[TextTransforms] | None | NOT_GIVEN = NOT_GIVEN,
    mcp_servers: list[MCPServer] | NOT_GIVEN = NOT_GIVEN,
    ...
)
```

### 4.2 `AgentSession.start`

```python
session.start(
    agent: Agent,
    *,
    room: rtc.Room | NOT_GIVEN = NOT_GIVEN,
    room_options: room_io.RoomOptions | NOT_GIVEN = NOT_GIVEN,
    capture_run: bool = False,
    record: bool | RecordingOptions | NOT_GIVEN = NOT_GIVEN,
) -> RunResult | None
```

### 4.3 `AgentSession.generate_reply`

```python
session.generate_reply(
    *,
    user_input: str | ChatMessage | NOT_GIVEN = NOT_GIVEN,
    instructions: str | Instructions | NOT_GIVEN = NOT_GIVEN,
    tool_choice: llm.ToolChoice | NOT_GIVEN = NOT_GIVEN,
    tools: list[str] | NOT_GIVEN = NOT_GIVEN,
    allow_interruptions: bool | NOT_GIVEN = NOT_GIVEN,
    chat_ctx: ChatContext | NOT_GIVEN = NOT_GIVEN,
    input_modality: Literal['text', 'audio'] = 'text',
) -> SpeechHandle
```

### 4.4 `AgentSession.say`

```python
session.say(
    text: str | AsyncIterable[str],
    *,
    audio: AsyncIterable[rtc.AudioFrame] | NOT_GIVEN = NOT_GIVEN,
    allow_interruptions: bool | NOT_GIVEN = NOT_GIVEN,
    add_to_chat_ctx: bool = True,
) -> SpeechHandle
```

### 4.5 `AgentSession.shutdown`

```python
session.shutdown(drain: bool = True) -> None
```

### 4.6 `Agent.__init__`

```python
Agent(
    *,
    instructions: str | Instructions,
    id: str | None = None,
    chat_ctx: ChatContext | None | NOT_GIVEN = NOT_GIVEN,
    tools: list[Tool | Toolset] | None = None,
    stt: STT | str | None | NOT_GIVEN = NOT_GIVEN,
    llm: LLM | str | None | NOT_GIVEN = NOT_GIVEN,  # per-agent LLM override
    tts: TTS | str | None | NOT_GIVEN = NOT_GIVEN,
    turn_handling: TurnHandlingOptions | NOT_GIVEN = NOT_GIVEN,
    min_endpointing_delay: float | NOT_GIVEN = NOT_GIVEN,
    max_endpointing_delay: float | NOT_GIVEN = NOT_GIVEN,
    allow_interruptions: bool | NOT_GIVEN = NOT_GIVEN,
    turn_detection: TurnDetectionMode | None | NOT_GIVEN = NOT_GIVEN,
    ...
)
```

> [!TIP]
> Each `Agent` (and `AgentTask`) can override LLM, TTS, STT, and turn settings. This maps directly to ElevenLabs' per-node LLM and turn setting overrides.

### 4.7 `AgentTask.__init__`

```python
AgentTask(
    *,
    instructions: str | Instructions,
    chat_ctx: ChatContext | NOT_GIVEN = NOT_GIVEN,
    tools: list[Tool | Toolset] | None = None,
    stt: STT | str | None | NOT_GIVEN = NOT_GIVEN,
    llm: LLM | str | None | NOT_GIVEN = NOT_GIVEN,
    tts: TTS | str | None | NOT_GIVEN = NOT_GIVEN,
    allow_interruptions: bool | NOT_GIVEN = NOT_GIVEN,
    ...
)
```

### 4.8 `AgentTask.complete`

```python
AgentTask[T].complete(result: T) -> None
```

Signals task completion and returns `result` to the awaiting caller.

### 4.9 `function_tool` decorator

```python
from livekit.agents.llm import function_tool

# Can be used with or without parens:
@function_tool           # no parens (bare decorator)
@function_tool()         # with parens
async def my_tool(self, context: RunContext, arg: str) -> str: ...
```

`RunContext` exposes: `session` (the `AgentSession`), `userdata`, and the current `agent`.

### 4.10 `AMD.__init__` and `.execute()`

```python
AMD(
    session: AgentSession,
    *,
    llm: LLM | str | None | NOT_GIVEN = NOT_GIVEN,
    stt: STT | str | None | NOT_GIVEN = NOT_GIVEN,
    interrupt_on_machine: bool = True,
    ivr_detection: bool = True,
    participant_identity: str | NOT_GIVEN = NOT_GIVEN,
    detection_options: DetectionOptions | NOT_GIVEN = NOT_GIVEN,
    wait_until_finished: bool = True,
) -> None

AMD.execute() -> AMDPredictionEvent
# AMDPredictionEvent.category: "human" | "uncertain" | "machine-ivr" | "machine-vm" | "machine-unavailable"
# AMDPredictionEvent.transcript: str
```

### 4.11 `ElevenLabs TTS` plugin

```python
from livekit.plugins import elevenlabs
elevenlabs.TTS(
    voice_id: str = "hpp4J3VqNfWAUOO0d1Us",
    model: TTSModels | str = "eleven_turbo_v2_5",
    voice_settings: VoiceSettings | NOT_GIVEN = NOT_GIVEN,
    # VoiceSettings has stability, similarity_boost, style, use_speaker_boost
    streaming_latency: int | NOT_GIVEN = NOT_GIVEN,
    language: str | NOT_GIVEN = NOT_GIVEN,
    apply_text_normalization: Literal['auto', 'off', 'on'] = 'auto',
    pronunciation_dictionary_locators: list[PronunciationDictionaryLocator] | NOT_GIVEN = NOT_GIVEN,
    ...
)
```

> Target Amelia's voice: `voice_id="eXpIbVcVbLo8ZJQDlDnl"`, `model="eleven_v3_conversational"`

### 4.12 `WarmTransferTask`

```python
WarmTransferTask(
    sip_call_to: str | NOT_GIVEN = NOT_GIVEN,
    *,
    sip_trunk_id: str | None | NOT_GIVEN = NOT_GIVEN,
    sip_number: str | NOT_GIVEN = NOT_GIVEN,
    dtmf: str | None | NOT_GIVEN = NOT_GIVEN,     # e.g. "wwww1234#"
    ringing_timeout: float | None | NOT_GIVEN = NOT_GIVEN,
    chat_ctx: ChatContext | NOT_GIVEN = NOT_GIVEN,
    extra_instructions: str = "",
    ...
)
```

### 4.13 `TaskGroup`

```python
from livekit.agents.beta.workflows import TaskGroup

group = TaskGroup()
group.add(lambda: MyTask(), id="my_task", description="...")
results: TaskGroupResult = await group
# results.task_results: dict[str, T]
```

`TaskGroup.add` signature: `(task_factory: Callable[[], AgentTask], *, id: str, description: str) -> Self`

### 4.14 `beta.workflows` — Built-in data capture tasks

Available in `livekit.agents.beta.workflows`:
- `GetEmailTask` / `GetEmailResult` (`.email_address`)
- `GetPhoneNumberTask` / `GetPhoneNumberResult` (`.phone_number`)
- `GetAddressTask` / `GetAddressResult` (`.address`)
- `GetNameTask` / `GetNameResult` (`.first_name`, `.last_name`)
- `GetDOBTask` / `GetDOBResult` (`.date_of_birth`, `.time_of_birth`)
- `GetCreditCardTask` / `GetCreditCardResult`
- `GetDtmfTask` / `GetDtmfResult`
- `WarmTransferTask` / `WarmTransferResult`
- `TaskGroup` / `TaskGroupResult`

These are **directly useful** for Node 4 (Lock In Next Step) — `GetEmailTask` handles email confirmation with spelling, `GetNameTask` for name.

**Source:** [VERIFIED] from `inspect` on `livekit.agents.beta.workflows`

### 4.15 `AgentServer.rtc_session`

```python
@server.rtc_session(
    agent_name: str = "",   # empty = any job; named = only dispatch to this agent name
    type: ServerType = ServerType.ROOM,
    on_request: Callable[[JobRequest], Any] | None = None,
    on_session_end: Callable[[JobContext], Any] | None = None,
)
```

### 4.16 `room_io.RoomOptions`

```python
RoomOptions(
    audio_input: AudioInputOptions | bool | NOT_GIVEN = NOT_GIVEN,
    close_on_disconnect: bool = True,
    delete_room_on_close: bool = False,  # set True to auto-delete room on call end
    participant_identity: str | NOT_GIVEN = NOT_GIVEN,
)
```

### 4.17 `create_sip_participant` (from amd.py example)

```python
await ctx.api.sip.create_sip_participant(
    api.CreateSIPParticipantRequest(
        room_name=ctx.room.name,
        sip_trunk_id=outbound_trunk_id,
        sip_call_to=phone_number,
        participant_identity=participant_identity,
        wait_until_answered=True,  # blocks until answered (not just ringing)
    ),
    timeout=45,  # overall API call timeout in seconds
)
```

**Source:** [`amd.py`](reference/examples/telephony/amd.py) lines 84–93

---

### 4.18 Quick shorthand syntax (from warm-transfer example)

```python
# AgentSession can accept model shorthand strings directly:
session = AgentSession(
    llm="openai/gpt-4.1-mini",
    stt="deepgram/nova-3:en",
    tts="cartesia/sonic-3:9626c31c-bec5-4cca-baa8-f8ba9e84c8bc",
)
```

**Source:** [`support_agent.py`](reference/examples/warm-transfer/support_agent.py) lines 69–73

---

## 5. Gotchas, Limitations & No Direct Equivalent

### 5.1 Things with NO direct LiveKit equivalent

| ElevenLabs Feature | Status in LiveKit |
|---|---|
| Visual workflow graph / editor | **None.** Code-only in LiveKit. |
| LLM-evaluated edge conditions | **None.** Must use tool calls / `complete()` for routing. |
| `backward_condition` on edges | **None.** Re-implement as: task returns a "go back" sentinel, caller re-enters the previous task. |
| `interruption_ignore_terms` (word list) | **No term list.** Only `false_interruption_timeout` (time-based). Will need prompt engineering. |
| `voicemail_message` (automated VM message) | **Not built-in.** Leave manually with `session.say(...)` before shutdown. |
| `silence_end_call_timeout` | **Not built-in.** Use `user_away_timeout` + `user_state_changed` event + `session.shutdown()`. |
| `max_duration_seconds` | **Not built-in.** Manual asyncio timer in entrypoint. |
| Built-in RAG / knowledge base | **Not built-in.** Use LlamaIndex, Chroma, or inject retrieved context as tool results. |
| Post-call analysis LLM (separate) | **Not built-in.** Custom LLM call in shutdown callback. |
| `call_outcome` data collection | **Not built-in.** Custom `userdata` field + shutdown callback → webhook. |
| `backup_llm_config` (automatic fallback) | **Not built-in.** Custom retry logic or use LLM with built-in fallback. |
| `cascade_timeout_seconds` | **Not built-in.** Custom timeout wrapping LLM calls. |
| `speculative_turn` | Partial: `preemptive_generation`. Not identical — ElevenLabs' speculative_turn starts TTS before full LLM output; LiveKit's preemptive starts LLM before full turn end. |
| `text_normalisation_type: system_prompt` | Manual: instruct TTS in prompt how to normalize, or use `tts_text_transforms`. |
| Pronunciation dictionaries | ElevenLabs plugin supports `pronunciation_dictionary_locators`. Cartesia has `pronunciation_dict_id`. |
| Language auto-detection (auto-switch) | Deepgram `detect_language=True` can detect; but prompts/voice won't auto-switch. Needs custom logic. |
| `prevent_subagent_loops` | Code discipline — do not call `complete()` cyclically. |
| Content guardrails (focus, prompt injection) | Prompt-only. No built-in guardrail layer. |
| `source_attribution` (KB source URLs) | Not applicable without a built-in KB. |
| `queueing_config` (wait queue) | LiveKit server handles concurrency; no Python-side queue config. |

### 5.2 Behavioral gotchas

1. **`user_away_timeout` defaults to 15.0 s** in `AgentSession`. If the user is silent for 15 s, `user_state_changed` fires with `new_state == "away"`. This is similar to ElevenLabs' silence detection but fires a Python event rather than ending the call. You must manually end the call in response.

2. **`preemptive_generation`** starts LLM generation while the user is still speaking (before end-of-turn). This reduces latency but may generate a reply based on incomplete input. ElevenLabs' `speculative_turn` is similar. Both can be disabled for nodes where patience is needed (Node 4).

3. **`AMD` needs session to be started BEFORE creating SIP participant** — the session must be recording audio when the call is answered. The `amd.py` example shows: start AMD context manager → create SIP participant → await detection.

4. **`session.room_io.set_participant(identity)`** must be called before AMD starts if there is a pre-existing participant, to avoid processing wrong audio. See `amd.py` line 73.

5. **Task/Agent `instructions` do NOT automatically stack** like ElevenLabs' `additional_prompt`. Each `AgentTask.instructions` fully replaces the system prompt for that task. You must manually prepend the global prompt if you want it to remain active.

6. **`TaskGroup` is sequential by default** (based on the `survey/agent.py` pattern using `await task_group`). Individual tasks run one at a time, each waiting for `complete()`. This matches ElevenLabs' sequential node flow.

7. **Chat context DOES carry across tasks** — `self.session.chat_ctx` is the same object throughout the session. Individual tasks can be given a separate `chat_ctx` at init, but by default they share the session's context.

8. **`session.shutdown(drain=True)`** waits for current TTS playout to finish before shutting down. Use `drain=False` for immediate hang-up.

9. **`EndCallTool`** (from `livekit.agents.beta`) provides an LLM-callable end-call tool that can say a goodbye message and delete the room. For this agent, we do NOT want the LLM to end calls (matching ElevenLabs' architecture) — do NOT use `EndCallTool` on Node 1–4 equivalents.

10. **`delete_room_on_close: bool = False`** in `RoomOptions` — default is FALSE. Must set `True` or manually call `ctx.api.room.delete_room()` in shutdown callback to clean up SIP. The `amd.py` example uses a shutdown callback for this.

11. **Inference models `inference.LLM("google/gemini-...")` names** — verify exact model name strings in the LiveKit inference catalog before use. The ElevenLabs model names (`gemini-3.5-flash`, `gemini-3.5-flash-lite`) likely do NOT match LiveKit's naming.

12. **ElevenLabs `eleven_v3_conversational` expressive mode** produces audio style tags inline with text output (`[Warmly]`, etc.). With the ElevenLabs plugin in LiveKit, these tags should be handled by the TTS model if expressive mode is enabled. Test to confirm behavior.

13. **`ctx.add_shutdown_callback(fn)`** — accepts both sync and async functions. Fires when `JobContext` is about to shut down. This is where you implement post-call analysis and webhooks.

---

## 6. Open Questions Before Building

### Critical (blocks architecture decisions)

1. **State persistence between tasks:** How will the six dynamic variables (`business_name`, `business_context`, `current_datetime`, `is_followup`, `followup_notes`, `contact_name`) reach the agent? Via LiveKit Room metadata (JSON string), dispatch metadata, or a pre-call webhook? The `ctx.room.metadata` or dispatch request metadata is the most likely path — what format?

2. **LLM model names:** What exact LiveKit inference model names should be used? The ElevenLabs agent uses `gemini-3.5-flash` (node fast), `gemini-3.5-flash-lite` (global default). What are the equivalent LiveKit inference strings? (e.g., `google/gemini-2.5-flash`, `openai/gpt-4.1-mini`, etc.)

3. **ElevenLabs voice (Amelia) via LiveKit plugin:** Confirm that `livekit-plugins-elevenlabs` TTS can use `voice_id="eXpIbVcVbLo8ZJQDlDnl"` and `model="eleven_v3_conversational"` and that streaming latency will be acceptable for telephony. Alternatively, is there a Cartesia voice that matches Amelia's persona?

4. **RAG implementation:** The knowledge base (129 lines, ~7.5 KB) is small enough to fit entirely in a single system prompt (< 2,000 tokens). Should we just inject the full KB as a static section of Node 3's instructions, rather than implementing RAG? This would be simpler and eliminate retrieval latency.

5. **Post-call webhook format:** The existing backend depends on `call_outcome` being set (11 enum values) plus the transcript. How is the backend currently consuming this? Direct ElevenLabs webhook call, or a middleware? What format/endpoint must the LiveKit replacement match?

6. **Outbound calling infrastructure:** Is the LiveKit SIP trunk already provisioned? What is `SIP_OUTBOUND_TRUNK_ID`? Is there an existing call dispatcher (the CRM/dialer that triggers calls)? It will need to `create_dispatch` or directly `create_sip_participant` for each outbound call.

7. **Silence / timeout handling:** The ElevenLabs agent uses `silence_end_call_timeout: 15 s`. Should the LiveKit agent hang up after 15 s of mutual silence (user_away + no agent reply), or just wait for the user to hang up?

### Important (affects specific node behavior)

8. **`skip_turn` equivalent:** When the prospect says "hold on," the current agent calls `skip_turn` (ElevenLabs system tool — disables both speaking and listening for up to N seconds). In LiveKit, what's the implementation? Options: (a) `allow_interruptions=False` + a custom tool that pauses the session, (b) detecting "hold on" in a tool and awaiting a `user_state_changed` "active" event, (c) other. Which approach?

9. **AMD tuning:** The current agent relies on LLM-based voicemail detection in Nodes 1 and 2. LiveKit `AMD` is audio-based and fires before any LLM turn. Should AMD handle ALL machine detection (cleaner), with Nodes 1 and 2 only seeing "human" traffic? Or should there be a fallback text-based check as well?

10. **Node 3 loop back to Node 3:** In ElevenLabs, the "Node 4 → Node 3 backward" transition happens when the prospect raises a new objection. In LiveKit, `ConversationTask.complete()` not being called keeps the task alive. The `LockInNextStepTask` would call `self.complete(GoBackToConversation(...))` which the parent handler interprets as re-entering `ConversationTask` from a new `on_enter`. Should the loop re-create a new `ConversationTask` (fresh) or somehow continue the existing one?

11. **Language handling:** The current agent uses ElevenLabs' auto language detection to switch the first message. Should the LiveKit version: (a) rely on the caller app to pass language in metadata, (b) detect language from first user utterance using Deepgram `detect_language`, or (c) only support English with a Spanish/Hindi/Urdu prompt fallback?

12. **Expressive tags:** The ElevenLabs prompt uses plain text (no `[Warmly]` tags in the prompt itself — those are generated by the voice model). With the ElevenLabs plugin in LiveKit, will the voice generate the same expressive tags? Or do we need to add them explicitly to TTS output?

13. **Interruption handling in Node 4:** Node 4 uses `patient` eagerness (10–12 s endpointing) so emails and phone numbers are collected without being cut off. In LiveKit: `min_endpointing_delay` on the `LockInNextStepTask`. What value (seconds) should be used?

14. **Max tool steps:** `AgentSession` defaults to `max_tool_steps=3`. If a task needs multiple tool calls in a sequence (e.g., Node 4 calling `record_email`, then `record_platform`, then `record_time`), is 3 enough? Should this be increased?

15. **Testing infrastructure:** The existing test plan (`Test-plan.md`) has 20 workflow checks and 20+ scenarios. LiveKit has simulation support (`on_simulation_end` on `AgentServer.rtc_session`). Should we build `text-only` simulations (like `data_capture_sim/agent.py`) that cover the W1–W20 test matrix?

### Nice to have

16. **Michael (second persona):** The v5 system has two personas (Amelia + Michael). Michael shares the workflow but has a different global prompt, voice, and knowledge base. Should the LiveKit implementation be a single codebase with persona switching (via `userdata.persona`) or two separate agents?

17. **Observability / logging:** ElevenLabs has built-in conversation history, cost tracking, and node analytics. LiveKit provides `MetricsCollectedEvent` and `session.usage`. What level of per-call logging / analytics is needed? Should we integrate with an existing observability stack (Datadog, OpenTelemetry, etc.)?

18. **`aec_warmup_duration`:** The ElevenLabs agent has no explicit AEC (acoustic echo cancellation) setting. The `basic_agent.py` example uses `aec_warmup_duration=3.0` (blocks interruptions for 3 s at call start to let the client calibrate AEC). Should this be used for telephony as well?

---

## 7. LiveKit Example Code Patterns (Key Takeaways)

### Pattern: Outbound SIP with AMD
**File:** [`amd.py`](reference/examples/telephony/amd.py)
- AMD is an async context manager (`async with AMD(session) as detector`)
- Must call `session.room_io.set_participant(identity)` before AMD starts
- `create_sip_participant` with `wait_until_answered=True`, `timeout=45`
- On `SipCallError` or `asyncio.TimeoutError`: `ctx.shutdown("call not answered")`
- AMD categories: `"human"`, `"uncertain"`, `"machine-ivr"`, `"machine-vm"`, `"machine-unavailable"`

### Pattern: Multi-step data collection (TaskGroup, sequential)
**File:** [`survey/agent.py`](reference/examples/survey/agent.py)
- Tasks are `AgentTask[ResultType]` subclasses
- `on_enter()` calls `self.session.generate_reply(instructions=...)`
- `@function_tool` on task methods call `self.complete(result)`
- `TaskGroup.add(lambda: Task(), id=..., description=...)` then `results = await task_group`
- `session.userdata` carries persistent state across tasks
- `user_away_timeout=10.0` + `user_state_changed` → nudge loop

### Pattern: Built-in data capture (beta workflows)
**File:** [`data_capture_sim/agent.py`](reference/examples/data_capture_sim/agent.py)
- `await beta.workflows.GetEmailTask(require_confirmation=True)` returns `GetEmailResult`
- Works inline inside a `@function_tool` — task runs and returns when complete
- `require_confirmation=True` forces agent to read email back and confirm

### Pattern: Warm transfer (SIP)
**File:** [`warm_transfer.py`](reference/examples/warm-transfer/warm_transfer.py), [`support_agent.py`](reference/examples/warm-transfer/support_agent.py)
- `await WarmTransferTask(sip_call_to=..., sip_trunk_id=..., chat_ctx=self.chat_ctx, extra_instructions=...)`
- Hold audio plays while dialing supervisor
- On success: `result.human_agent_identity`; then `session.shutdown()`

### Pattern: DTMF / IVR navigation
**File:** [`ivr_navigator_agent.py`](reference/examples/telephony/bank-ivr/ivr_navigator_agent.py)
- `ivr_detection=True` on `AgentSession` adds `send_dtmf_events` tool automatically
- `min_endpointing_delay=5` (5 s wait) for IVR prompts

### Pattern: Shutdown and room cleanup
```python
ctx.add_shutdown_callback(log_usage)  # async or sync function

async def hangup():
    await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
ctx.add_shutdown_callback(hangup)
```

### Pattern: Dispatch for outbound call
**File:** [`dial_bank_agent.py`](reference/examples/telephony/bank-ivr/dial_bank_agent.py)
- Caller app creates a dispatch AND creates SIP participant independently
- Dispatch: `lkapi.agent_dispatch.create_dispatch(CreateAgentDispatchRequest(agent_name=..., room=..., metadata=json_string))`
- Agent reads metadata: `ctx.room.metadata`

---

## 8. Package Version Reference

| Package | Version | Verified |
|---|---|---|
| `livekit-agents` | **1.8.10** | ✅ via `import livekit.agents; __version__` |
| Examples target | `>=1.6` | From `pyproject.toml` in voice_agents and survey examples |
| `livekit-plugins-elevenlabs` | bundled with extras | ✅ importable |
| `livekit-plugins-deepgram` | bundled | ✅ importable |
| `livekit-plugins-cartesia` | bundled | ✅ importable |

---

*Last updated: 2026-10-30. Notes will be updated when web research subagent completes.*

