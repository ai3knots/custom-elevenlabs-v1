# Architecture Decision Records (ADRs) (`docs/DECISIONS.md`)

> **Scope:** Production Migration Architecture: ElevenLabs Voice Agent to LiveKit Agents (Python).  
> **Status:** All decisions documented with context, alternatives considered, trade-offs, and final rationale.

---

## ADR-01: State Management: Single Long-Lived Agent + Explicit State Machine vs. AgentTask vs. TaskGroup

### Context
ElevenLabs operates as a stateful graph where 4 conversational nodes share full conversation history, have distinct turn settings, override LLM models, and transition conditionally, including backward loops (Node 4 back to Node 3).

### Alternatives Considered
1. **`AgentTask` / `TaskGroup` Pattern:** Each node is implemented as an `AgentTask[ResultT]` subclass, orchestrated by `TaskGroup` or sequential `await Task()` calls.
2. **Multi-Agent Handoffs (`session.handoff(NewAgent())`):** Completely swapping the agent instance across boundaries.
3. **Single Long-Lived `Agent` + Explicit State Machine (Selected):** One `Agent` instance representing the persona (Amelia/Michael), governed by a deterministic finite state machine (FSM) that updates instructions, tools, and options dynamically.

### Evaluation & Comparison

| Criteria | AgentTask | Multi-Agent Handoff | Single Agent + FSM |
|---|---|---|---|
| **Chat History Continuity** | Requires explicit `chat_ctx` sharing across tasks | History must be cloned or serialized | Naturally unified in `self.session.chat_ctx` |
| **Backward Loops (4 -> 3)** | Awkward: requires recursive or while-loop state | Clutters agent stack | Trivial: state transition to `conversation` |
| **Turn Settings Overrides** | Re-applies turn options at task boundary | Re-applies on handoff | Handled via `agent.update_options()` |
| **LLM Overrides Per Node** | Specified in task constructor | Specified in agent constructor | Handled via `agent.update_options(llm=...)` |
| **Visual / Audit Traceability** | Task completion events | Handoff events | Explicit state transition log in Postgres |

### Decision
**Adopt Single Long-Lived Agent + Explicit State Machine.**
LiveKit 1.8.10 natively exposes `Agent.update_instructions(prompt)`, `Agent.update_tools(tools)`, and `Agent.update_options(llm=...)` [VERIFIED]. A single long-lived agent guarantees zero context fragmentation, seamless backward loops from `lock_in` to `conversation`, and unified session state tracking.

---

## ADR-02: Safety & Compliance: Parity DNC Architecture vs. Complex Pre-Dial Gate

### Context
In outbound telemarketing, prospects may ask to be removed from calling lists. The original plan proposed a complex dual-layer system (regex pattern matcher, secondary classifier, pre-dial database checks, and a new `dnc_records` table).

### Alternatives Considered
1. **Complex Dual-Layer DNC Gate:** Pre-dial DB checks + in-call regex scanner + secondary LLM classifier.
2. **LLM Direct Hangup Tool:** Giving the LLM an `end_call` tool directly (violates current ElevenLabs architecture).
3. **Parity DNC Architecture (Selected):**
   - Pre-dial suppression remains entirely within `Posivex-lead-gen` (which already has robust suppressions in `src/services/suppressions.ts`).
   - In-call behavior maintains strict parity with current ElevenLabs design: a dedicated `dnc_close` state, its literal closing line (*"Understood, my apologies. I'll make sure we don't call this number again. Take care."*), and the `do_not_call` outcome enum.
   - The conversational LLM requests transition to `dnc_close` via the standard `transition("dnc_close", reason)` tool when the prospect asks not to be contacted again.
   - The LLM never receives a hangup tool; only the state machine terminates calls.

### Decision
**Adopt Parity DNC Architecture.**
Drop the pre-dial DNC database check, regex/keyword matcher, and `dnc_records` table. Keep the `dnc_close` state, its literal speech, the `do_not_call` outcome emitted in post-call webhooks, and the prompt-driven transition to `dnc_close` from `opener`, `reach_owner`, `conversation`, and `lock_in`.

---

## ADR-03: LLM Integration: Google Plugin (`livekit-plugins-google`) vs. LiveKit Inference

### Context
LiveKit offers two pathways to LLMs:
1. **LiveKit Inference shorthand:** `inference.LLM("google/gemini-3.5-flash")` routed through LiveKit Cloud's managed inference proxies.
2. **Direct Provider Plugin:** `livekit.plugins.google.LLM` utilizing our own `GEMINI_API_KEY`.

### Evaluation
- **Control & Pricing:** We self-host and bring our own API keys. Routing through LiveKit Inference adds latency, dependency on LiveKit Cloud's model proxy catalog, and external billing.
- **Model Availability:** `gemini-3.5-flash` and `gemini-3.5-flash-lite` are verified available directly on Google's v1beta endpoint using `GEMINI_API_KEY` [VERIFIED via `spikes/check_gemini.py`].
- **Configuration Granularity:** `livekit-plugins-google.LLM` supports `thinking_config={"thinking_level": "MINIMAL"}` and `temperature=0.0` directly [VERIFIED].

### Decision
**Adopt `livekit-plugins-google.LLM` with direct `GEMINI_API_KEY`.**
Wrap with `livekit.agents.llm.FallbackAdapter` for automatic cascading to `gemini-2.5-flash` on provider failure.

---

## ADR-04: TTS Engine: ElevenLabs v3 Conversational vs. Cartesia / Deepgram Aura

### Context
ElevenLabs voice `eXpIbVcVbLo8ZJQDlDnl` (Amelia) with model `eleven_v3_conversational` defines the brand persona. Switching to Cartesia would require finding a voice twin and retraining stakeholders.

### Evaluation
- Code inspection of `livekit/plugins/elevenlabs/tts.py` in package `1.8.10` reveals lines 1119–1442: LiveKit specifically detects `is_dialogue_model("eleven_v3_conversational") == True` and connects to `wss://api.elevenlabs.io/v1/text-to-dialogue/multi-stream-input` [VERIFIED].
- ElevenLabs v3 conversational is fully supported for streaming out-of-the-box in both English and Spanish.

### Decision
**Retain ElevenLabs TTS as the primary voice provider.**
Configure `encoding="pcm_16000"`, `streaming_latency=3`.

---

## ADR-05: STT Engine: Single Deepgram Nova-3 (English & Spanish) vs. Multilingual Router

### Context
The original draft considered a multilingual router routing Hindi/Urdu to AssemblyAI or Soniox. With the scope narrowed to English and Spanish, Deepgram Nova-3 natively supports both languages.

### Evaluation
- In `livekit-plugins-deepgram` 1.8.10:
  - Deepgram Nova-3 supports per-call fixed language: `language="en-US"` or `language="es"`.
  - Deepgram Nova-3 also supports multilingual code-switching mode: `language="multi"`, with optional `language_hint=["en", "es"]` [VERIFIED].
- Dispatch metadata specifies language (`en` or `es`, default `en`). If unspecified, Nova-3 multilingual mode handles code-switching.
- Eliminating a custom router removes complex audio frame demuxing and multi-provider connection overhead.

### Decision
**Adopt Single Deepgram Nova-3 STT for English and Spanish.**
Compare per-call fixed language vs `language="multi"` in SPK-03 on real phone audio. Keep STT configuration swappable via config, but do not build an in-flight language router.

---

## ADR-06: Webhook Delivery: Outbox Pattern vs. Direct In-Memory Dispatch

### Context
When a call completes, `Posivex-lead-gen` must receive the post-call payload. If the lead-gen server is restarting or experiences a transient 500 error, direct in-memory HTTP requests would lose call logs and outcomes.

### Alternatives Considered
1. **Direct Background Task:** `asyncio.create_task(http_client.post(...))` in the shutdown callback.
2. **Transactional Outbox in Postgres (Selected):** When a call ends, the call record, transcript, outcome, and webhook event are committed inside a single Postgres transaction into the `webhook_outbox` table. A lightweight asyncio polling loop delivers pending webhooks with exponential backoff and idempotency keys.

### Decision
**Adopt Transactional Outbox Pattern.**
Guarantees at-least-once delivery, zero data loss, and decoupling of call lifecycle from backend network availability.

---

## ADR-07: Database Architecture: Postgres (SQLAlchemy 2 Async + Alembic) vs. MongoDB

### Context
`Posivex-lead-gen` uses MongoDB via Mongoose. The new voice agent service requires persistence for calls, prompt versions, experiments, and costs.

### Decision
**Adopt PostgreSQL with async SQLAlchemy 2.0 and Alembic.**
Keep MongoDB in `Posivex-lead-gen`; the voice agent service maintains its own Postgres database as the source of truth for voice calls. Note: `dnc_records` table is removed per ADR-02.

---

## ADR-08: Redis Utilization & Scope

### Context
LiveKit Server uses Redis for room routing and multi-node clusters. The question is how our Python agent and API service should utilize Redis.

### Justified Uses
1. **Distributed Concurrency Lock:** Atomic increment/decrement of active calls per campaign and global system limits.
2. **Idempotency Keys:** Prevent double-dialing of the same lead within a 60-second window.

### Decision
**Use Redis exclusively for concurrency counters and short-lived distributed dial locks.**

---

## ADR-09: Telephony Provider Abstraction

### Context
SIP trunking is deferred for initial development and testing. We must test via LiveKit Console, browser WebRTC, and automated text simulations before connecting real trunks.

### Decision
**Define a pluggable `TelephonyProvider` interface:**
- `MockTelephonyProvider`: Injects mock audio / simulated SIP events for text simulations.
- `LiveKitWebRTCProvider`: Connects audio via WebRTC browser client for manual audio testing.
- `LiveKitSIPProvider`: Calls `ctx.api.sip.create_sip_participant(wait_until_answered=True)`. Enabled only when `TELEPHONY_PROVIDER=sip`.

---

## ADR-10: Knowledge Base Integration: Inlined Prompt vs. Vector RAG

### Context
The Amelia knowledge base (`Knowledge-base.md`) is 129 lines (~7.5 KB, ~1,850 tokens). It applies to Node 3 only.

### Decision
**Implement a Hybrid Architecture:**
- Default mode: `kb.mode = "inline"`. The KB is injected directly into `conversation` state instructions.
- Startup Validator: Computes token budget; fails fast if KB exceeds 4,000 tokens.
- Interface Extensibility: Encapsulate behind `KnowledgeProvider` interface (`get_context(query) -> str`) so Vector RAG can be toggled via config later if needed.

---

## ADR-11: Scope Simplification: English & Spanish Only, Single Deepgram STT, and Lead-Gen Owned DNC

### Context
User instruction issued a scope change focusing on production simplicity:
1. Eliminate Hindi and Urdu support completely.
2. Remove the custom multilingual STT router; standardize on Deepgram Nova-3 for English and Spanish.
3. Remove the deterministic regex/classifier DNC gate, pre-dial database checks, and `dnc_records` table. Lead suppression remains the sole responsibility of `Posivex-lead-gen`.

### Consequences & Removals
- **Removed:**
  - AssemblyAI and Soniox/Uplift STT evaluation.
  - Urdu/Hindi first message presets and Roman-script Urdu/Hindi prompts.
  - 8 Hindi and Urdu terms from `interruption_ignore_terms` (हम्म, हूँ, ठीक है, बोलिए, ہمم, مم, اچھا, ٹھیک ہے).
  - Deterministic DNC pattern matcher, secondary classifier, and 60+ utterance corpus.
  - `dnc_records` table in PostgreSQL.
  - Pre-dial DNC check in API gateway.
- **Retained:**
  - Spanish first-message preset and bilingual guardrail in global prompt.
  - Deepgram Nova-3 as single STT (evaluating fixed `en`/`es` vs `multi`).
  - English and Spanish backchannel ignore terms (16 English, 5 Spanish).
  - Parity `dnc_close` state with literal apology line and `do_not_call` outcome enum.
  - Safety rule: LLM has no hangup tool; only state machine ends calls.


## Single prompt replaces the state machine (2026-11-05)

Superseding "Single Agent + Explicit State Machine" above. The agent is one `PersonaAgent` with the ElevenLabs "Single Master Prompt"
(Steps A to F), the knowledge base appended, and three tools (`end_call`, `skip_turn`, `voicemail_detection`). Reasons: no `transition`
round trip (it added 2 to 3 s to the turns that changed state), no FSM/tool race conditions, and parity with the ElevenLabs agent
`agent_2701m6e2fw2eehd97r93d4v8x9yc`. The outcome is decided after the call by a classifier instead of a tool the model could forget or misuse.
Trade-off: the model tracks the step itself, so it needs scenario testing. Details in `docs/SINGLE_PROMPT_MIGRATION_PLAN.md`.
