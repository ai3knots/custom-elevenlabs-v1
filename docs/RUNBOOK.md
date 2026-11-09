# Operations Runbook: Posivex LiveKit Voice Calling Service

> **Service:** Posivex LiveKit Outbound Voice Calling Service  
> **Repository:** `Khizar-v1`  
> **Status:** Production Build Ready  
> **Target Integration:** Zero-code change compatibility with `Posivex-lead-gen`

---

## 1. Architectural Overview

The Posivex Voice Calling service is a modular, high-performance voice AI solution designed to replace ElevenLabs ConvAI with self-hosted LiveKit Agents (Python 3.12), Deepgram Nova-3, Google Gemini, and ElevenLabs Conversational v3.

```
                  +-----------------------------------+
                  |        Posivex-lead-gen           |
                  |     (Next.js Webhook Client)      |
                  +-----------------+-----------------+
                                    |
          1. Dial Request           |  4. Signed Post-Call Webhook
             (ConvAI Emulation)     |     (HMAC-SHA256)
                                    v
+-----------------------------------------------------------------------+
|                      Posivex Voice Service                            |
|                                                                       |
|   +---------------------+             +---------------------------+   |
|   |   FastAPI Service   | <---------> |  Transactional Outbox     |   |
|   |  - /v1/convai/*     |             |  - Worker (Exp. Backoff)  |   |
|   |  - /api/v1/calls    |             |  - HMAC Signature Header  |   |
|   |  - /health & metrics|             +-------------+-------------+   |
|   +----------+----------+                           |                 |
|              |                                      |                 |
|              v                                      v                 |
|   +---------------------+             +---------------------------+   |
|   | Telephony Provider  |             | PostgreSQL 16 (AsyncPG)   |   |
|   | - LiveKit SIP       |             | - calls & events          |   |
|   | - Mock / Console    |             | - transcripts & costs     |   |
|   +----------+----------+             | - webhook_outbox queue    |   |
|              |                        +---------------------------+   |
|              v                                                        |
|   +---------------------------------------------------------------+   |
|   |                LiveKit Voice Agent Worker                     |   |
|   |  - One system prompt, 3 tools (end_call/skip_turn/voicemail)  |   |
|   |  - Deepgram Nova-3 STT (en / es / multi)                      |   |
|   |  - ElevenLabs Conversational v3 TTS (WebSocket streaming)     |   |
|   |  - Gemini 3.5 Flash / Lite + FallbackAdapter                  |   |
|   |  - Layered AMD & Silence Timeout Guardrails                   |   |
|   +---------------------------------------------------------------+   |
+-----------------------------------------------------------------------+
```

---

## 2. Process Entrypoints & CLI Commands

All operational tasks use the single unified CLI entrypoint: `python -m src.main` via `uv run`.

### 2.1 LiveKit Voice Agent Worker
Connects to the LiveKit server, waits for participant rooms, and executes conversation sessions.

```bash
# Launch default Amelia voice agent
uv run python -m src.main agent --persona amelia

# Launch Michael voice agent
uv run python -m src.main agent --persona michael
```

### 2.2 FastAPI Calling & Emulation API Service
Exposes native LiveKit endpoints and ElevenLabs ConvAI emulation layer for `Posivex-lead-gen`.

```bash
# Production HTTP server
uv run python -m src.main api --host 0.0.0.0 --port 8000
```

### 2.3 Transactional Webhook Outbox Worker
Continuously polls PostgreSQL `webhook_outbox` table, computes HMAC-SHA256 signatures, and delivers post-call transcripts to `${LEAD_GEN_BASE_URL}/api/webhooks/elevenlabs` with exponential backoff retries.

```bash
# Standalone background worker
uv run python -m src.main outbox --interval 2.0
```

### 2.4 Local Development Mode (All-in-One)
Runs both the FastAPI service and the outbox worker concurrently within a single process.

```bash
# Local development mode
uv run python -m src.main dev --port 8000
```

---

## 3. Environment Configuration & Secret Management

Copy `.env.example` to `.env` and configure credentials:

| Environment Variable | Description | Default / Example |
|---|---|---|
| `ENVIRONMENT` | Deployment environment (`development`, `staging`, `production`) | `production` |
| `LOG_LEVEL` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` |
| `DATABASE_URL` | PostgreSQL AsyncPG connection string | `postgresql+asyncpg://posivex:posivex@localhost:5432/posivex_calls` |
| `REDIS_URL` | Redis cache and queue connection string | `redis://localhost:6379/0` |
| `LIVEKIT_URL` | LiveKit Cloud project WebSocket URL | `wss://<project>.livekit.cloud` |
| `LIVEKIT_API_KEY` | LiveKit Cloud API key (Project Settings -> Keys) | `API...` |
| `LIVEKIT_API_SECRET` | LiveKit Cloud API secret | `...` |
| `GEMINI_API_KEY` | Google Gemini API Key | `AIzaSy...` |
| `DEEPGRAM_API_KEY` | Deepgram API Key | `token...` |
| `ELEVENLABS_API_KEY` | ElevenLabs API Key | `xi-...` |
| `WEBHOOK__LEAD_GEN_BASE_URL` | Base URL of `Posivex-lead-gen` app | `http://localhost:3000` |
| `WEBHOOK__WEBHOOK_SECRET` | Shared secret for HMAC-SHA256 signing | `posivex_secret_123` |
| `TELEPHONY__PROVIDER` | Provider mode (`livekit_sip`, `mock`, `console`) | `livekit_sip` |
| `TELEPHONY__SIP_TRUNK_ID` | Default outbound SIP Trunk ID | `ST_xxxxxxxxx` |
| `TELEPHONY__MAX_CONCURRENT_CALLS` | Global concurrency ceiling | `50` |
| `TURN_DETECTOR_MODE` | End-of-turn strategy: `vad`, `stt-only`, `v1`, `v1-mini` (LiveKit audio turn detector) | `vad` |
| `LLM_DEFAULT_MODEL` / `LLM_FALLBACK_MODEL` | Override the Gemini models without a rebuild. The fallback must differ from the default. The default also runs the post-call outcome classifier. | `gemini-3.5-flash` / `gemini-3.5-flash-lite` |

The `conversation.amd_timeout_seconds` setting in `config/defaults.yaml` (default `3.0`) caps how long answering-machine detection may delay the opening line after pickup. The opening line waits for the caller to stop talking (up to 3 s) and cannot be interrupted. `conversation.max_user_speech_seconds` (default `8.0`, `0` disables) makes the agent answer when VAD reports one uninterrupted caller turn for that long, e.g. because of background noise.

### Single-prompt agent

The agent is one system prompt (`prompts/<persona>/system.j2`, Steps A to F) plus the knowledge base, with three tools: `end_call`, `skip_turn` and `voicemail_detection`. There is no state machine. Related settings in `config/defaults.yaml` (`conversation:`):

- `silence_end_call_seconds` (15): the call ends after this much caller silence; `skip_turn` ("hold on") suspends it for `hold_duration_seconds` (12). Before the caller has said anything the agent says one "Hello?" first.
- `min_endpointing_delay` / `max_endpointing_delay` (0.5 / 4.0): one turn-taking setting for the whole call.
- If the model says a goodbye but forgets `end_call`, the call is ended 2.5 s after the goodbye (`CLOSING_GRACE_SECS`).
- The call outcome is decided after the call by one structured Gemini call over the transcript (`src/analysis/outcome.py`, rules in `prompts/analysis/call_outcome.md`). An answering-machine verdict or the voicemail tool forces `voicemail` / `automated_menu`; no caller speech gives `hangup_no_speak`; a classifier failure gives `other`.

The Amelia knowledge base lives at `prompts/knowledge/Knowledge-base.md` (not under `reference/`, which is excluded from the Docker image). If you re-export it from ElevenLabs, copy it there. A missing file is logged as an error and the call continues without a knowledge base.

### 3.1 LiveKit Cloud SIP Setup

Signaling, media and SIP are hosted by LiveKit Cloud. The agent worker only makes outbound connections to Cloud, so the host needs no inbound SIP/RTP port forwarding. No `livekit-server` or `livekit/sip` containers are run.

1. Create a project at https://cloud.livekit.io and copy its URL, API key and secret into `.env` (`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`).
2. Create the Telnyx outbound trunk on that project (once):
   ```bash
   uv run python scripts/setup_telnyx_trunk.py --phone "+1XXXXXXXXXX" --sip-user "<telnyx user>" --sip-pass "<telnyx pass>"
   ```
   Put the printed trunk ID in `.env` as `SIP_OUTBOUND_TRUNK_ID` and set `TELEPHONY_PROVIDER=livekit_sip`.
3. Start the stack: `docker compose up -d --build`. The agent registers with Cloud as `posivex-agent`.
4. Telnyx authenticates the trunk by username and password. If the Telnyx SIP connection is restricted by IP, allow LiveKit Cloud's static ranges (Static IPs docs) or `0.0.0.0/0`.

The older self-hosted SIP debugging notes are in `docs/SIP_NETWORK_DEBUG_GUIDE.md` (historical).

---

## 4. Database Migrations (Alembic)

Database schema migrations are managed via Alembic with async SQLAlchemy 2 support.

```bash
# Check current migration revision
uv run alembic current

# Upgrade to latest migration revision
uv run alembic upgrade head

# Autogenerate a new migration after model edits
uv run alembic revision --autogenerate -m "describe_changes"

# Downgrade by one revision
uv run alembic downgrade -1
```

---

## 5. Health Probes & Monitoring

### 5.1 Health Check Endpoints
- **Liveness Probe:** `GET http://localhost:8000/health/live`
  - Returns `{"status": "alive"}` (HTTP 200).
- **Readiness Probe:** `GET http://localhost:8000/health/ready`
  - Verifies database connectivity (`SELECT 1`).
  - Returns `{"status": "ready", "database": "connected"}` (HTTP 200).
  - Returns HTTP 503 if database connection fails.

### 5.2 Prometheus Metrics Exposition
- **Metrics Scrape URL:** `GET http://localhost:8000/metrics`
- **Key Metrics Tracked:**
  - `posivex_calls_total{persona, language, status}`
  - `posivex_call_outcomes_total{persona, outcome}`
  - `posivex_call_duration_seconds{persona}` (Histogram with buckets 5s to 900s)
  - `posivex_cost_dollars_total{provider, metric_type}`
  - `posivex_webhook_deliveries_total{status}`
  - `posivex_active_calls` (Gauge)

### 5.3 Logging & Sensitive Data Redaction
- All log lines are emitted as single-line structured JSON with `call_id` context propagation.
- Credit card numbers (13–19 digits), authorization tokens, and API secrets are automatically scrubbed and replaced with `[REDACTED]`.

---

## 6. Webhook Outbox Delivery & Dead-Letter Recovery

### 6.1 Delivery Workflow
1. When a voice call session completes, `CallRecorder.finalize_call()` transactionally inserts the post-call payload into `webhook_outbox` with `status='pending'`.
2. The outbox worker polls records where `next_retry_at <= now()`.
3. The payload is signed with HMAC-SHA256 (`t=<ts>,v0=<hmac>`) and posted to `Posivex-lead-gen`.
4. On 2xx response: status updated to `delivered`.
5. On failure: `attempts` incremented, status set to `pending`, and `next_retry_at` rescheduled with exponential backoff (`delay = 2.0 * (2 ** attempts)`).
6. If `attempts >= max_attempts` (default: 10), record transitions to `dead_letter`.

### 6.2 Dead-Letter Recovery
To inspect and re-queue dead-lettered webhooks in PostgreSQL:

```sql
-- View dead-lettered webhooks
SELECT id, call_id, attempts, last_error, created_at 
FROM webhook_outbox 
WHERE status = 'dead_letter' 
ORDER BY created_at DESC;

-- Re-queue dead-lettered webhooks for retry
UPDATE webhook_outbox 
SET status = 'pending', attempts = 0, next_retry_at = NOW() 
WHERE status = 'dead_letter';
```

---

## 7. Posivex-Lead-Gen Integration

Lead-gen keeps using its ElevenLabs client. Setting `ELEVENLABS_API_BASE_URL` points that client at this service's
ElevenLabs-compatible API instead of `https://api.elevenlabs.io` (this needs the lead-gen change on branch
`feat/voice-api-base-url`; before it, the URL was hard-coded and no setting could redirect it).

### 7.1 What the API provides

| Lead-gen calls | Handled by |
|---|---|
| `POST /v1/convai/sip-trunk/outbound-call` (and `/twilio/`) | `convai_emulation.py`: creates the call row, picks the persona from `agent_id`, dials through LiveKit |
| `GET /v1/convai/agents`, `GET /v1/convai/phone-numbers` | `convai_read.py`: the agents and numbers from `config/defaults.yaml` (`agents:`, `phone_numbers:`) |
| `GET /v1/convai/conversations`, `/{id}` | `convai_read.py`: the same conversation shape ElevenLabs returns (transcript, tool calls, metadata, analysis) |
| `GET /v1/convai/conversations/{id}/audio` | `convai_read.py`: streams the recording from the bucket (404 until a recording exists) |
| Post-call webhook (we call lead-gen) | outbox worker: signed `t=...,v0=...`, full `data` object built by `src/webhook/payload.py` |

All of them need the `xi-api-key` header, which must equal `POSIVEX_API_KEY` (or `ELEVENLABS_API_KEY` when that is unset).

An `agent_id` that is not listed in `agents:` (for example an old ElevenLabs agent) is served by Amelia. The master-prompt agent
`agent_2701m6e2fw2eehd97r93d4v8x9yc` is Amelia; `agent_posivex_michael` is Michael.

### 7.2 Settings

On this service (`.env`):

| Variable | Purpose |
|---|---|
| `POSIVEX_API_KEY` | Key lead-gen sends as `xi-api-key` (any long random value) |
| `LEAD_GEN_WEBHOOK_URL` | Lead-gen's public URL, for example `https://xyz.thebestdevsolutions.space` (the webhook path is added). The default `http://localhost:3000` is wrong for production |
| `ELEVENLABS_WEBHOOK_SECRET` | The secret this service signs webhooks with; lead-gen's `VOICE_WEBHOOK_SECRET` must equal it |
| `VOICE_PHONE_NUMBER_ID` / `VOICE_PHONE_NUMBER` | The calling number lead-gen shows and stores (default id `phnum_posivex_trunk`) |
| `RESULTS_CALLBACK_URL` / `RESULTS_CALLBACK_SECRET` | Only for an agent that cannot reach Postgres (LiveKit Cloud): the API's public URL and a shared secret. Leave unset when the agent runs next to the database |
| `RECORDING_ENABLED`, `RECORDING_BUCKET`, `RECORDING_REGION`, `RECORDING_ENDPOINT`, `RECORDING_ACCESS_KEY`, `RECORDING_SECRET`, `RECORDING_PREFIX` | Call recording (below) |

On lead-gen (`.env.server`), see `docs/voice-provider.md` in that repo:

```
ELEVENLABS_API_BASE_URL=http://127.0.0.1:8000        # this service's API (same machine), or its public URL
VOICE_API_KEY=<the same value as POSIVEX_API_KEY>
VOICE_API_AGENT_IDS=agent_2701m6e2fw2eehd97r93d4v8x9yc   # ONLY these agents use this service; everything else stays on ElevenLabs
VOICE_WEBHOOK_SECRET=<the same value as ELEVENLABS_WEBHOOK_SECRET on this service>
```

Nothing is routed until `VOICE_API_AGENT_IDS` lists an agent, so adding the other three variables changes no existing campaign.
Lead-gen picks the agent per campaign, so create a new campaign with the listed agent and its number. ElevenLabs keeps signing
with lead-gen's own `ELEVENLABS_WEBHOOK_SECRET`; lead-gen accepts a webhook signed with either secret. Conversation ids from this
service start with `lk_`, which is how lead-gen reads each conversation and recording from the right provider.

### 7.3 Results callback (agent without database access)

When `RESULTS_CALLBACK_URL` is set the agent keeps the transcript, tool events, usage and recording location in memory and
sends them once at the end of the call to `POST /internal/calls/{id}/result` (header `X-Posivex-Callback-Secret`, 4 attempts
with backoff). The API stores them and stages the webhook for lead-gen, so the outbox keeps its retries. A repeat of the same
report is ignored. Expose only `/internal/` and `/v1/convai/` through the reverse proxy, never the rest of the API.

### 7.4 Call recording

LiveKit egress records the call (audio only) to an S3-compatible bucket (AWS S3, Cloudflare R2, MinIO, Wasabi...) from the moment
the callee answers. The file appears shortly after the call ends; the API streams it for lead-gen's recording player.
It is off until `RECORDING_ENABLED=true` and the bucket settings are present. Egress minutes are billed by LiveKit separately
from agent minutes. Not yet tested against a real bucket.

### 7.5 Checks before the first campaign

1. `curl -H "xi-api-key: $POSIVEX_API_KEY" $BASE/v1/convai/agents` returns the two agents.
2. Lead-gen Settings lists the agents and the number; one manual call completes and its call page shows the transcript and outcome.
3. Keep `DISABLE_REAL_CALLS=true` on lead-gen until step 2 works, then switch it to `false` for a one-lead campaign.

## 8. Rollback Procedure

If any issue arises:
1. Remove `ELEVENLABS_API_BASE_URL` and `VOICE_API_KEY` from lead-gen's environment (the default is ElevenLabs).
2. Restore `ELEVENLABS_WEBHOOK_SECRET` to the original ElevenLabs signing secret if you changed it.
3. Restart `Posivex-lead-gen`. New calls go through ElevenLabs again; calls already placed through this service still finish and report.

