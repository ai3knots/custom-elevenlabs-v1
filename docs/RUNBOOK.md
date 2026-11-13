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
|   |  - /internal/*      |             |  - HMAC Signature Header  |   |
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
| `LOG_LEVEL` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` |
| `DATABASE_URL` | PostgreSQL AsyncPG connection string | `postgresql+asyncpg://posivex:posivex@localhost:5432/posivex_calls` |
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
| `TELEPHONY__MAX_CONCURRENT_CALLS` | Global concurrency ceiling. Matches the Ship plan's 20 concurrent agent sessions; raise it only with the plan | `20` |
| `NOISE_CANCELLATION` | Background-noise cancellation on the callee's audio: `krisp_nc` (LiveKit Cloud, no extra charge) or `none`. Sent to the Cloud agent by `deploy_agent.py`, so changing it needs a redeploy | `krisp_nc` |
| `TURN_DETECTOR_MODE` | End-of-turn strategy: `vad`, `stt-only`, `v1`, `v1-mini` (LiveKit audio turn detector) | `vad` |
| `LLM_DEFAULT_MODEL` / `LLM_FALLBACK_MODEL` | Override the Gemini models without a rebuild. The fallback must differ from the default. The default also runs the post-call outcome classifier. | `gemini-3.5-flash` / `gemini-3.5-flash-lite` |

Call start (`conversation:` in `config/defaults.yaml`):

- The opening line starts `opener_delay_seconds` (default `1.0`) after pickup. If the callee is mid-sentence it waits up to `opener_quiet_wait_seconds` (default `1.0`) more, then speaks anyway. It cannot be interrupted by the callee.
- LiveKit's AMD is no longer used. Its 3 s cap ended before most greetings finished, so it returned "uncertain" on 27 of 28 calls and only delayed the opener. Instead `src/agent/greeting_detector.py` runs beside the opener: a small Gemini model (`machine_detection_model`, default `gemini-3.5-flash-lite`) reads the callee's streaming transcript as it arrives and classifies it as a person, a voicemail or a phone menu / carrier message. At or above `machine_detection_confidence` (default `0.8`) the agent stops speaking and hangs up, and the call is labelled `voicemail` or `automated_menu`. The `voicemail_detection` tool remains as a fallback. Set `machine_detection_enabled: false` to turn the check off.
- The callee's audio keeps reaching speech-to-text while the opener plays (`discard_audio_if_uninterruptible: false` in `build_turn_handling`). LiveKit's default replaces it with silence, which left the greeting check blind for about 7 s and lost the callee's first words. A callee turn that finishes during the opener is still skipped by LiveKit; if it was a real answer ("Yes, this is the owner") the agent replies to it right after the opener (`src/agent/opener.py` and `src/agent/call_session/opening.py`), but never to a bare "Hello?", an echo of the opener, or when the greeting check has not confirmed a person.
- Every call records timing events (`timing_pickup`, `timing_first_callee_speech`, `timing_opener_started`, `timing_machine_detected`, `timing_silence_nudge`, `timing_no_speech_watchdog`) in `call_events`, with `since_pickup_secs` in the payload. Durations and transcript times count from pickup, not from job start, so ringing is not included.

Silence: `silence_end_call_seconds` (default `15`) ends the call after that much caller silence, after one "Hello?" nudge if the callee never spoke. `no_speech_watchdog_seconds` (default `40`, `0` disables) also ends it when no words have been transcribed for that long, for example on hold music. `hold_duration_seconds` (default `30`) is how long `skip_turn` ("hold on", "let me get them") pauses the silence timer.

`conversation.max_user_speech_seconds` (default `8.0`, `0` disables) makes the agent answer when VAD reports one uninterrupted caller turn for that long, e.g. because of background noise.

### Single-prompt agent

The agent is one system prompt (`prompts/<persona>/system.j2`, Steps A to F) plus the knowledge base, with three tools: `end_call`, `skip_turn` and `voicemail_detection`. There is no state machine. Related settings in `config/defaults.yaml` (`conversation:`):

- `silence_end_call_seconds` (15): the call ends after this much caller silence; `skip_turn` ("hold on") suspends it for `hold_duration_seconds` (30). Before the caller has said anything the agent says one "Hello?" first.
- `min_endpointing_delay` / `max_endpointing_delay` (0.5 / 1.5): one turn-taking setting for the whole call. The maximum is how long the agent waits for a reply the turn detector is unsure has ended; it was 4.0, which caused 4 s of dead air after short answers such as "Yes.". Raise it if callers dictating emails are cut off.
- If the model says a goodbye but forgets `end_call`, the call is ended 2.5 s after the goodbye (`CLOSING_GRACE_SECS`).
- The call outcome is decided after the call by one structured Gemini call over the transcript (`src/analysis/outcome.py`, rules in `prompts/analysis/call_outcome.md`). An answering-machine verdict or the voicemail tool forces `voicemail` / `automated_menu`; no caller speech gives `hangup_no_speak`; a classifier failure gives `other`.

The Amelia knowledge base lives at `prompts/knowledge/Knowledge-base.md`. If you re-export it from ElevenLabs, copy it there. A missing file is logged as an error and the call continues without a knowledge base.

### 3.1 LiveKit Cloud SIP Setup

Signaling, media and SIP are hosted by LiveKit Cloud. The agent worker only makes outbound connections to Cloud, so the host needs no inbound SIP/RTP port forwarding. No `livekit-server` or `livekit/sip` containers are run.

1. Create a project at https://cloud.livekit.io and copy its URL, API key and secret into `.env` (`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`).
2. Create the Telnyx outbound trunk on that project (once):
   ```bash
   uv run python scripts/setup_telnyx_trunk.py --phone "+1XXXXXXXXXX" --sip-user "<telnyx user>" --sip-pass "<telnyx pass>"
   ```
   Put the printed trunk ID in `.env` as `SIP_OUTBOUND_TRUNK_ID` and set `TELEPHONY_PROVIDER=livekit_sip`.

   The script sets `destination_country` to `us` (change with `--destination-country`). LiveKit then starts the phone leg
   on a US server (Ashburn, Chicago or Phoenix). Without it the leg starts in the region of whoever makes the dial
   request, which is far from the callee when the API runs in another country. For a trunk that already exists:
   `lk sip outbound update --id <ST_...> --destination-country us` (only that field changes).
   This pins the phone leg only. The LiveKit room, the agent (`--region`, `us-east`) and the recording bucket are
   separate settings.
3. Start the API stack: `docker compose up -d --build posivex_api`. The agent is deployed to LiveKit Cloud separately (`scripts/deploy_agent.py`, section 7) and registers as `posivex-agent`.
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

**On the server** the runtime image has no `uv`, and Postgres is not published to the host, so run Alembic inside the API
container (it already has `DATABASE_URL`, `alembic.ini` and the `alembic` command on its `PATH`). Rebuild first so the container
has the new migration file:

```bash
docker compose up -d --build posivex_api
docker compose exec posivex_api alembic current            # expect: b7c41e5a9d20 (head) after the upgrade, 19ddfd7201e0 before
docker compose exec posivex_api alembic upgrade head
```

If `alembic current` prints nothing, the database was not created by Alembic (the tables exist but there is no
`alembic_version` row): run `docker compose exec posivex_api alembic stamp 19ddfd7201e0` first, then `upgrade head`.

---

## 5. Health Probes & Monitoring

### 5.1 Health Check Endpoints
- **Liveness Probe:** `GET http://localhost:8000/health/live`
  - Returns `{"status": "alive"}` (HTTP 200).
- **Readiness Probe:** `GET http://localhost:8000/health/ready`
  - Verifies database connectivity (`SELECT 1`).
  - Returns `{"status": "ready", "database": "connected"}` (HTTP 200).
  - Returns HTTP 503 if database connection fails.
- **Docker:** `docker-compose.yml` runs a liveness healthcheck on `posivex_api` (`/health/live` every 30 s), so `docker compose ps` shows `healthy` or `unhealthy`. Docker does not restart on `unhealthy`; the restart policy handles crashes. The API gets 30 s to stop (`stop_grace_period`) so the outbox worker can finish the webhook it is delivering.

### 5.2 Prometheus Metrics Exposition
- **Metrics Scrape URL:** `GET http://localhost:8000/metrics`
- **Key Metrics Tracked:**
  - `posivex_calls_total{persona, language, status}`
  - `posivex_webhook_deliveries_total{status}`

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
| `POST /v1/convai/sip-trunk/outbound-call` | `convai_emulation.py` calling `services/dispatch.py`: creates the call row, picks the persona from `agent_id`, dials through LiveKit |
| `GET /v1/convai/agents`, `GET /v1/convai/phone-numbers` | `convai_read.py`: the agents and numbers from `config/defaults.yaml` (`agents:`, `phone_numbers:`) |
| `GET /v1/convai/conversations`, `/{id}` | `convai_read.py`: the same conversation shape ElevenLabs returns (transcript, tool calls, metadata, analysis) |
| `GET /v1/convai/conversations/{id}/audio` | `convai_read.py`: streams the recording from the bucket (404 until a recording exists) |
| Post-call webhook (we call lead-gen) | outbox worker: signed `t=...,v0=...`, full `data` object built by `src/webhook/payload.py` |

All of them need the `xi-api-key` header, which must equal `POSIVEX_API_KEY` (or `ELEVENLABS_API_KEY` when that is unset).

An `agent_id` that is not listed in `agents:` (for example an old ElevenLabs agent) is served by Amelia. `agent_posivex_amelia`
("Posivex LiveKit - Amelia") is Amelia; `agent_posivex_michael` ("Posivex LiveKit - Michael") is Michael. These ids are our own on
purpose: lead-gen lists ElevenLabs agents and ours in one dropdown, so an id shared with an ElevenLabs agent (the earlier default
reused `agent_2701m6e2fw2eehd97r93d4v8x9yc`) cannot be told apart there. Moving a campaign between providers means choosing the other
agent, not editing `VOICE_API_AGENT_IDS`.

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
VOICE_API_AGENT_IDS=agent_posivex_amelia,agent_posivex_michael   # ONLY these agents use this service; everything else stays on ElevenLabs
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
report is ignored. Expose only `/internal/` through the reverse proxy for the Cloud agent, never the rest of the API; lead-gen
keeps reaching `/v1/convai/` on `127.0.0.1:8000`.

### 7.4 Call recording

LiveKit egress records the call (audio only) to an S3-compatible bucket (AWS S3, Cloudflare R2, MinIO, Wasabi...) from the moment
the callee answers. The file appears shortly after the call ends; the API streams it for lead-gen's recording player.
It is off until `RECORDING_ENABLED=true` and the bucket settings are present. Egress minutes are billed by LiveKit separately
from agent minutes. Verified against AWS S3. On start-up the API and the agent write and delete one tiny object
(`recordings/.startup-check`) and log `Recording bucket '...' is reachable and writable`, or name the setting to fix
(`RECORDING_BUCKET`, `RECORDING_REGION`, the keys). The audio endpoint's 404 says why there is nothing to play.

### 7.5 Checks before the first campaign

1. `curl -H "xi-api-key: $POSIVEX_API_KEY" $BASE/v1/convai/agents` returns the two agents.
2. Lead-gen Settings lists the agents and the number; one manual call completes and its call page shows the transcript and outcome.
3. Keep `DISABLE_REAL_CALLS=true` on lead-gen until step 2 works, then switch it to `false` for a one-lead campaign.

### 7.6 Deploying the agent to LiveKit Cloud

The API, Postgres and the outbox stay on your server. Only the agent worker moves; it reports each finished call to the API
over HTTPS (7.3), so it never needs the database.

1. **Public address for `/internal/` only.** Point a DNS name (for example `voice.thebestdevsolutions.space`) at the server and
   add an nginx site (aaPanel: Website, reverse proxy) that forwards just that path to the API:
   ```nginx
   location /internal/ { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; }
   location / { return 404; }
   ```
2. **Both `.env` files** (server and `.env.prod`) get the same two values; recreate the API afterwards:
   ```
   RESULTS_CALLBACK_URL=https://voice.thebestdevsolutions.space
   RESULTS_CALLBACK_SECRET=<openssl rand -hex 32>
   ```
3. **Deploy from this machine** (the `lk` CLI must be logged in to the project in `LIVEKIT_URL`):
   ```
   uv run python scripts/deploy_agent.py --dry-run     # shows names only, and probes the results endpoint
   uv run python scripts/deploy_agent.py               # `lk agent create` the first time, `lk agent deploy` after
   ```
   The script sends only what the agent needs from `.env.prod` (never `LIVEKIT_*`, the database or webhook settings),
   through a temporary file it deletes. The region defaults to `us-east` (`--region` to change it).
4. **No worker on the server.** The compose file no longer has an agent service, so nothing on your server can take a call. If an old
   `posivex_agent` container from an earlier compose file still exists there, remove it (it would register under the same
   name as the Cloud agent): `docker compose up -d --build --remove-orphans posivex_api`, or `docker rm -f posivex_agent`.
   To roll the agent back, see 7.7.
5. **Watch it:** `lk agent logs`, `lk agent status`. Calls still finish through the API's outbox to lead-gen.

### 7.7 Rolling the agent back (Ship plan and above)

A deploy that goes wrong is undone without rebuilding. Always name the project: the CLI's default may be the old one.

```
lk agent versions --project posivex-new                 # the list; the current one is marked
lk agent rollback --project posivex-new                 # back to the version before the current one
lk agent rollback --project posivex-new --version <id>  # or to a specific version
lk agent status --project posivex-new                   # wait until the rolled-back version is running
```

The rollout is rolling: new calls go to the restored version once it is healthy, and calls already running on the bad version
are allowed to finish (up to an hour). Rolling back the agent does not touch the API; if the API was deployed in the same
change, roll that back with git (`git checkout <good commit>` then `docker compose up -d --build posivex_api`).
On the Build plan there is no `rollback`: revert the code and run `deploy_agent.py` again.

### 7.8 Plan limits and noise cancellation

**Limits (Ship plan, from LiveKit's pricing page).** 20 concurrent agent sessions, 5,000 agent-session minutes a month included
(then $0.01 per minute), 5,000 third-party SIP minutes (then $0.004 per minute), 5,000 recording minutes. `telephony.max_concurrent_calls`
is 20 to match, so the API refuses call 21 itself. Raise the two together (here and in the plan) and never one alone.
Lead-gen's campaign concurrency should climb in steps (5, then 10, then 20), watching for failed dials.

**Check once in the LiveKit dashboard** (Settings, usage and limits): the **concurrent egress** limit. Every call starts an audio recording
through egress, and the Build plan allowed only 2 at once. LiveKit's documentation does not list Ship's egress limit, so read it there.
If it is lower than 20, calls past that number run without a recording (the call itself is unaffected; the agent logs
"Could not start the recording for call ..."). Ask LiveKit support to raise it, or keep campaign concurrency at or below it.

**Noise cancellation.** `conversation.noise_cancellation` (`NOISE_CANCELLATION`) is `krisp_nc` by default: Krisp's background-noise model on the
callee's audio before speech-to-text, the voice detector and the turn detector. LiveKit Cloud includes it at no extra charge (the
voice-isolation models, including the telephony one, are metered). It does not change the recording. Each call logs
`Noise cancellation: krisp_nc` (or `off`) and the `session_init` event carries `noise_cancellation: true|false`.
A plugin that cannot load never fails a call: the agent logs a warning and carries on with the raw audio.
Noise cancellation can occasionally make speech-to-text *worse*, so compare it on real calls: change the setting to `none` in `.env.prod`,
run `uv run python scripts/deploy_agent.py` (the script resends the agent's whole environment), place the same test calls, and compare the
transcripts and any "interrupted" or missed words. Turn it back on the same way. A single API-placed call can also be switched off with
`config_overrides: {"conversation": {"noise_cancellation": "none"}}`.

On the Build plan the agent scales to zero when idle, so the first call after a quiet period waits roughly 10 to 20 seconds
for a cold start (callee answers before the worker has joined). The Ship plan keeps it warm.

## 8. Rollback Procedure

If any issue arises:
1. Remove `ELEVENLABS_API_BASE_URL` and `VOICE_API_KEY` from lead-gen's environment (the default is ElevenLabs).
2. Restore `ELEVENLABS_WEBHOOK_SECRET` to the original ElevenLabs signing secret if you changed it.
3. Restart `Posivex-lead-gen`. New calls go through ElevenLabs again; calls already placed through this service still finish and report.


## 9. Moving to a new LiveKit Cloud project

Build the new project completely next to the old one, test it with a separate API instance that never touches production, then switch four variables on the server. The old project keeps working until you delete it, so rolling back is restoring those four variables.

**What lives inside a LiveKit project and must be recreated:** the outbound SIP trunk, the Cloud agent and its secrets, and the API key and secret. **What does not move:** the Telnyx account and number, lead-gen, Postgres, the S3 recordings (each recording request carries the S3 credentials), the prompts.

**What the server uses:** `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` and `SIP_OUTBOUND_TRUNK_ID` in `.env`. The Cloud agent gets its own URL and keys injected by LiveKit.

### 9.1 Prepare the new project (from the machine with the repo; production is not touched)

1. Save the project in the `lk` CLI under a name, **not** as default:
   `lk project add posivex-new --url wss://<new>.livekit.cloud --api-key <KEY> --api-secret <SECRET>`
   The deploy script now refuses to run unless the project from `.env.prod` is saved with the same key. (`lk --subdomain` alone silently falls back to the CLI's default project, which is why this matters.)
2. Check it is the empty project you expect: `lk agent list --project posivex-new` and `lk sip outbound list --project posivex-new` should show nothing. In the LiveKit dashboard check the plan and that the project region is in the US.
3. Create the trunk **in the new project**. The credentials are the Telnyx SIP connection's user and password (Telnyx portal). Inline variables win over `.env`, so the old project cannot be hit by mistake; the script prints the URL it connects to:
   `LIVEKIT_URL=wss://<new>.livekit.cloud LIVEKIT_API_KEY=<KEY> LIVEKIT_API_SECRET=<SECRET> uv run python scripts/setup_telnyx_trunk.py --phone +14706000407 --sip-user <USER> --sip-pass <PASS> --name Telnyx-NoHeader`
   Note the printed `SIP_OUTBOUND_TRUNK_ID`. It builds the same trunk as the live one (US destination, no username header; add `--username-header` only if you ever need it). If the Telnyx connection is restricted by IP, LiveKit Cloud's SIP addresses are the same for every project.
4. Keep the old file and point `.env.prod` at the new project: `cp .env.prod .env.prod.old`, then change `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`. Everything else in it stays.
5. `livekit.toml` records the last deploy's project and agent id. Move it aside so the agent is created in the new project, then dry-run and deploy:
   `mv livekit.toml livekit.<old-subdomain>.toml`
   `uv run python scripts/deploy_agent.py --dry-run` (shows `lk project: posivex-new`; stop if it does not)
   `uv run python scripts/deploy_agent.py`
   Commit the new `livekit.toml` (it holds a subdomain and an agent id, no secrets). Check: `lk agent status --project posivex-new`.

### 9.2 Test it in isolation (on the server; production API untouched)

Run a second API instance, API only (the `api` command has no outbox worker), on port 8001 with the new project's four variables. The compose service reads `.env`; `-e` overrides it:

```bash
docker compose run --rm -d --name posivex_api_test -p 127.0.0.1:8001:8000 \
  -e LIVEKIT_URL=wss://<new>.livekit.cloud -e LIVEKIT_API_KEY=<KEY> -e LIVEKIT_API_SECRET=<SECRET> \
  -e SIP_OUTBOUND_TRUNK_ID=<ST_new> \
  posivex_api api --host 0.0.0.0 --port 8000
curl -s http://127.0.0.1:8001/health/ready
```

Dial your own phone through it (the key is `POSIVEX_API_KEY` from `.env`):

```bash
curl -s -X POST http://127.0.0.1:8001/v1/convai/sip-trunk/outbound-call \
  -H "xi-api-key: $POSIVEX_API_KEY" -H "Content-Type: application/json" \
  -d '{"agent_id":"agent_posivex_amelia","to_number":"+1YOURNUMBER","conversation_initiation_client_data":{"dynamic_variables":{"business_name":"Test Pizza","contact_name":"Khizar"}}}'
```

Check, in this order: the phone rings and the opener plays about a second after you answer; the conversation works; after hanging up, `curl -s -H "xi-api-key: $POSIVEX_API_KEY" http://127.0.0.1:8001/v1/convai/conversations/<conversation_id>` shows `done` with an outcome and the `timing_*` events; `.../audio` plays (the recording went through the new project's egress to S3); `lk agent logs --project posivex-new` shows the call. The finished call is reported to `RESULTS_CALLBACK_URL`, which is the production API, so its outbox delivers it to lead-gen and it appears there as a manual call.

Cases to try: answered and talking; a number that goes to voicemail (hangs up within seconds, outcome `voicemail`); declined or unanswered (`no_answer`); hanging up during the opener. Then `docker stop posivex_api_test`.

### 9.3 Cut over

1. Pause campaigns in lead-gen and wait for no active calls:
   `docker compose exec postgres psql -U posivex -d posivex_calls -c "select count(*) from calls where status in ('queued','in_progress')"` (use your `POSTGRES_USER` and `POSTGRES_DB` if they are not the defaults).
2. `cp .env .env.bak-livekit`, then change the four variables in `.env`.
3. `docker compose up -d --force-recreate posivex_api` (`--force-recreate` because Docker does not notice changes inside `env_file`). Check `docker compose logs --tail=50 posivex_api` and `curl -s http://127.0.0.1:8000/health/ready`.
4. Start one call from lead-gen, confirm it end to end, then resume campaigns.

### 9.4 Roll back

`cp .env.bak-livekit .env && docker compose up -d --force-recreate posivex_api`. The old project and its agent were never changed, and calls already placed still report to the API.

### 9.5 Afterwards

Keep the old project for a week, then delete its agent, trunk or the project itself. Rotate any LiveKit key that was shared outside the repo and `.env` files.
