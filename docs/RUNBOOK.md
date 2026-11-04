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
|   |  - Single Agent + 7-State FSM (opener -> close/dnc)           |   |
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

## 7. Zero-Code-Change Posivex-Lead-Gen Integration

To route calls through this service instead of ElevenLabs:

1. In `Posivex-lead-gen/.env.local`:
   ```bash
   # Point ElevenLabs API base URL to this service
   ELEVENLABS_API_BASE_URL="http://localhost:8000"
   ELEVENLABS_WEBHOOK_SECRET="posivex_secret_123"
   ```
2. No TypeScript code changes are required in `Posivex-lead-gen`.
3. Calls dispatched to `/v1/convai/sip-trunk/outbound-call` or `/v1/convai/twilio/outbound-call` will automatically route to LiveKit SIP trunks.
4. Post-call webhooks received by `Posivex-lead-gen/src/app/api/webhooks/elevenlabs/route.ts` will verify the HMAC signature and process transcripts normally.

---

## 8. Rollback Procedure

If any issue arises:
1. Revert `ELEVENLABS_API_BASE_URL` in `Posivex-lead-gen/.env.local` to `https://api.elevenlabs.io`.
2. Revert `ELEVENLABS_WEBHOOK_SECRET` to the original ElevenLabs signing secret.
3. Restart `Posivex-lead-gen`. Production traffic immediately reverts to ElevenLabs.

