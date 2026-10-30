# Migration Progress & Execution Status (`docs/PROGRESS.md`)

> **Last Updated:** 2026-10-30  
> **Status:** Planning & Verification Complete — Awaiting Approval ("continue") to Begin Phase 1

---

## 1. Project Overview & Scope Baseline

- **Goal:** Migrate production ElevenLabs outbound sales voice agent (Amelia & Michael personas) to LiveKit Agents (Python 3.12).
- **Core Architecture:** Single Long-Lived Agent + Explicit State Machine (7 states: `opener`, `reach_owner`, `conversation`, `lock_in`, `close`, `dnc_close`, `end`).
- **Languages:** English and Spanish only (Hindi and Urdu dropped).
- **STT:** Deepgram Nova-3 (evaluating fixed `en`/`es` vs `language="multi"`).
- **LLM:** Google Gemini (`gemini-3.5-flash-lite` global / `gemini-3.5-flash` conversation) with `thinking_level: MINIMAL` and `livekit.agents.llm.FallbackAdapter`.
- **TTS:** ElevenLabs `eleven_v3_conversational` streaming via `text-to-dialogue/multi-stream-input` (voice `eXpIbVcVbLo8ZJQDlDnl`).
- **Safety / DNC:** Parity with ElevenLabs — prompt-driven transition to `dnc_close` state with literal apology line and `do_not_call` outcome enum. Suppression remains managed by `Posivex-lead-gen`. The LLM has no hangup tool; only the state machine terminates calls.
- **Backend Integration:** Zero-change compatibility with `Posivex-lead-gen` via an ElevenLabs emulation layer (`/v1/convai/*`) and transactional webhook outbox with HMAC-SHA256 signatures.

---

## 2. Completed Milestones (Planning & Verification)

- [x] **ElevenLabs Agent Export Deconstruction:** Fully analyzed workflow graph, 4 conversation nodes, say nodes, guardrails, pricing tables, dynamic variables, and `amelia-json-config.json`.
- [x] **Backend Contract Discovery:** Inspected `C:\Users\Khizar\Desktop\Projects\Posivex-lead-gen` codebase. Documented outbound call requests, dynamic variables, post-call webhook structure, and HMAC signature verification in [`docs/BACKEND_CONTRACT.md`](BACKEND_CONTRACT.md).
- [x] **Package & API Verification:**
  - Verified `livekit-agents==1.8.10` API surfaces (`AgentSession`, `Agent`, `update_instructions`, `update_tools`, `update_options`).
  - Verified `livekit-plugins-elevenlabs` natively supports `eleven_v3_conversational` over Text-to-Dialogue WebSocket.
  - Verified `models/gemini-3.5-flash` and `models/gemini-3.5-flash-lite` exist and respond on the live Google API.
  - Verified `livekit-plugins-deepgram` Nova-3 language modes (`language="en-US"`, `language="es"`, and `language="multi"` with hints).
  - Verified `livekit.agents.llm.FallbackAdapter` signature and cascading behavior.
- [x] **Architecture Specifications:** Produced [`docs/ARCHITECTURE_PLAN.md`](ARCHITECTURE_PLAN.md), [`docs/DECISIONS.md`](DECISIONS.md), and [`docs/SPIKES.md`](SPIKES.md).

---

## 3. Execution Roadmap (Build-First Strategy)

| Step | Milestone | Scope / Deliverables | Status |
|---|---|---|---|
| **B0** | **Project Scaffold & Tooling** | `uv init`, `pyproject.toml`, pinned dependencies (`livekit-agents==1.8.10`), ruff, pyright, pytest, structured JSON logging with `call_id` context propagation and sensitive data redaction, `Taskfile.yaml`. | **Completed** |
| **B1** | **Config Engine & Provider Registry** | Layered configuration (`defaults.yaml` -> `profiles/<persona>.yaml` -> `.env` -> dispatch metadata); pluggable provider factories (STT, LLM, TTS, VAD, FallbackAdapter); single-line switchability unit tests. | Queued |
| **B2** | **Infrastructure & Database** | `docker-compose.yml` (`core` profile: Postgres, Redis, LiveKit dev mux); multi-stage uv `Dockerfile`; async SQLAlchemy 2 models (`calls`, `call_events`, `transcripts`, `usage_costs`, `webhook_outbox`); Alembic migration. | Queued |
| **B3** | **Finite State Machine Engine** | Pure-Python FSM (`src/fsm/`) with 7 states (`opener`, `reach_owner`, `conversation`, `lock_in`, `close`, `dnc_close`, `end`); allowed transition matrix; prompt-driven DNC transition; hold timers; unit tests. | Queued |
| **B4** | **Prompts & Knowledge Base** | Jinja2 templates for Amelia and Michael (Nodes 1–5, bilingual guardrails, pricing tables); inline KB injector with startup token budget validator. | Queued |
| **B5** | **LiveKit Voice Agent Worker** | `PersonaAgent` subclass driven by FSM; dynamic instruction and tool swapping (`Agent.update_instructions`, `Agent.update_tools`); Deepgram Nova-3 STT; ElevenLabs v3 conversational TTS; Gemini LLM + FallbackAdapter; layered AMD. | Queued |
| **B6** | **Persistence & Cost Tracking** | Event recording; transcript persistence; per-call token, character, and provider cost tracking to Postgres. | Queued |
| **B7** | **FastAPI API Service** | `/api/v1/calls` native API + `/v1/convai/*` ElevenLabs emulation layer; request validation schemas matching Posivex-lead-gen. | Queued |
| **B8** | **Transactional Webhook Outbox** | Database outbox queue; worker with exponential backoff retries; HMAC-SHA256 signature generator matching ElevenLabs contract. | Queued |
| **B9** | **Telephony Abstraction** | `TelephonyProvider` interface; `LiveKitSIPProvider` implementation; Mock/Console provider for offline verification. | Queued |
| **B10** | **Hardening & Verification** | Prometheus metrics; health check endpoints; smoke test script; deployment runbook. | Queued |

