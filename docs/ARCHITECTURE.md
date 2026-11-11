# Posivex voice platform: architecture as deployed

**Status date:** 10 Nov 2026. This document describes what is running now, not a plan.

**What the platform is.** A cold-calling system for local US businesses. A lead-generation web app decides who to call and when. A voice service places the call through a phone carrier, an AI agent (Amelia or Michael) holds the conversation, and the result flows back to the lead-gen app, which decides what happens next (retry, follow-up, email, suppression).

**Two repositories**

| Repo | Folder | What it is |
|---|---|---|
| `custom-elevenlabs-v1` (this one) | `Khizar-v1` | The voice service: FastAPI API, outbox worker, Postgres, and the LiveKit voice agent |
| `Posivex-lead-gen` | `Projects/Posivex-lead-gen` | The lead-gen app: Next.js, MongoDB, Inngest background jobs |

**How to read this document.** Section 1 is the picture. Section 2 says where each piece runs. Section 3 follows one call from start to finish. Sections 4 to 10 explain each part. Sections 11 to 14 cover security, operations, regions and costs. Section 15 lists what is not built or not yet known.

---

## 1. The system in one picture

```
                                 LEAD-GEN (your server, Pakistan)
   people  ─────────────►  https://xyz.thebestdevsolutions.space  (nginx ► Next.js :3005)
   (browser)                 │   MongoDB Atlas (leads, campaigns, calls, emails…)
                             │   Inngest jobs (campaign pump, follow-ups, emails)
                             │   SMTP (posivex.com) for post-call emails
                             │
        ┌────────────────────┴───────────────────────────────┐
        │ chooses the provider by AGENT ID                   │
        ▼                                                    ▼
 ElevenLabs Agents (SaaS)                          VOICE API (your server, Pakistan)
 every agent NOT on the list                       https://api.thebestdevsolutions.space
 (legacy path, still the default)                  (nginx ► container posivex_api :8000)
                                                    │  FastAPI + webhook outbox worker
                                                    │  Postgres 16 (container posivex_postgres)
                                                    │
                   dial request                     │  POST /v1/convai/sip-trunk/outbound-call
                                                    ▼
                                         LIVEKIT CLOUD  (project-3knots-digital-01)
                                          ├─ rooms, agent dispatch
                                          ├─ SIP: outbound trunk ST_Sf8PZnHSN76S (destination_country us)
                                          ├─ egress (call recording)
                                          └─ AGENT HOSTING: posivex-agent  (us-east, scale to zero)
                                                │
              ┌─────────────────────────────────┼──────────────────────────────┐
              ▼                                 ▼                              ▼
        Deepgram Nova-3                  Gemini 3.5 Flash                 ElevenLabs TTS
        (speech to text)                 (+ Flash-Lite fallback)          (eleven_v3_conversational)

   LiveKit SIP ──► Telnyx (sip.telnyx.com) ──► phone network ──► the business
   LiveKit egress ──► AWS S3  posivex-bucket (us-east-1)  recordings/<call_id>.ogg
   Cloud agent ──HTTPS──► Voice API  /internal/calls/{id}/result   (the finished call)
   Voice API outbox ──HTTPS, HMAC-signed──► lead-gen /api/webhooks/elevenlabs
```

Three ideas explain the whole design:

1. **The voice API pretends to be ElevenLabs.** Lead-gen was built against ElevenLabs. The voice API implements the same endpoints, conversation format and signed webhook, so lead-gen needs no new code path per call: it only routes by agent id.
2. **The audio never touches the server in Pakistan.** The agent runs on LiveKit Cloud in the US. During a call nothing is written to the Pakistan server. The agent keeps everything in memory and sends one report when the call ends.
3. **Nothing is lost if a part is down.** A finished call is stored first, then delivered to lead-gen through a transactional outbox with retries. Lead-gen stores every webhook before processing it.

---

## 2. Where everything runs

| Component | Where | How it runs | Reached at |
|---|---|---|---|
| Lead-gen app (Next.js) | Your server (aaPanel, Pakistan) | Node process, port 3005, behind nginx | `https://xyz.thebestdevsolutions.space` |
| Lead-gen database | MongoDB Atlas (cloud) | Database `posivex` on cluster `cluster0` | MongoDB connection string in lead-gen `.env` |
| Inngest (background jobs for lead-gen) | Your server | Event/cron runner the app talks to at `INNGEST_BASE_URL` (`127.0.0.1:8589` in the production env) | Local only |
| Lead-gen emails | Your mail host (`posivex.com`, SMTP 465) | Used by Nodemailer | n/a |
| Voice API + outbox worker | Your server | Docker container `posivex_api`, command `dev` (API and outbox worker in one process), bound to `127.0.0.1:8000` | `https://api.thebestdevsolutions.space` (nginx proxy) |
| Voice database (Postgres 16) | Your server | Docker container `posivex_postgres`, volume `postgres_data` | Internal to the Docker network |
| Cloud voice agent | LiveKit Cloud, region **us-east** | Agent `CA_NQ7cB3ZyKoWg` named `posivex-agent`, version `Ej95aPksCzvi` (deployed 10 Nov 2026), one instance with 2 CPUs and 4 GB, scales to zero | Dispatched by LiveKit per call |
| Rooms, SIP, egress, observability | LiveKit Cloud | Project `project-3knots-digital-01` (`p_3c1gzyddfo8`), Build plan | `wss://project-3knots-digital-01-sc2w8xlh.livekit.cloud` |
| Phone carrier | Telnyx | SIP connection used by the LiveKit trunk, number **+1 470 600 0407** | `sip.telnyx.com` |
| Call recordings | AWS S3, **us-east-1** | Bucket `posivex-bucket`, prefix `recordings/` | Written by LiveKit egress, read by the voice API |
| Speech-to-text | Deepgram | Nova-3, streaming | Called from the Cloud agent |
| LLM | Google Gemini API | `gemini-3.5-flash`, fallback `gemini-3.5-flash-lite` | Called from the Cloud agent (conversation, greeting check, outcome classifier) |
| Text-to-speech | ElevenLabs API | `eleven_v3_conversational`, voice "Amelia" | Called from the Cloud agent |
| Legacy voice provider | ElevenLabs Agents | All agents lead-gen does not route to the voice API | Called from lead-gen |

**No worker on your server.** `docker-compose.yml` defines only Postgres and the API; the agent exists only on LiveKit Cloud, so nothing on the Pakistan server can take a call. An old `posivex_agent` container from earlier compose files may still exist (stopped) on the server: `docker compose up -d --build --remove-orphans posivex_api` removes it, or `docker rm -f posivex_agent`.

**Things this document cannot see from code** and that were configured by hand: the nginx site files on the server (aaPanel), the Telnyx portal (SIP connection, number, billing), the LiveKit dashboard (plan, observability), and the AWS account. They are described from what the code and CLI show.

---

## 3. One call, end to end

```mermaid
sequenceDiagram
    participant LG as Lead-gen (campaign pump)
    participant API as Voice API
    participant PG as Postgres
    participant LK as LiveKit Cloud
    participant AG as Cloud agent
    participant TX as Telnyx / phone
    participant S3 as S3
    LG->>API: POST /v1/convai/sip-trunk/outbound-call (xi-api-key)
    API->>PG: create call row (queued) + event
    API->>LK: create agent dispatch (posivex-agent, call metadata)
    API->>LK: CreateSIPParticipant (trunk, number to dial)
    API-->>LG: conversation_id lk_xxxxxxxxxxxxxx
    LK->>AG: start job in room call_lk_xxx
    LK->>TX: SIP INVITE, the phone rings
    TX-->>LK: answered
    AG->>LK: start recording (egress)
    LK->>S3: write recordings/lk_xxx.ogg
    AG->>AG: greeting check, opener, conversation
    AG->>AG: call ends, outcome classified
    AG->>API: POST /internal/calls/lk_xxx/result (secret header)
    API->>PG: store transcript, usage, events, outcome; stage webhook
    API->>LG: signed post_call_transcription webhook (outbox, with retries)
    LG->>LG: store event, update call, apply outcome policy
    LG->>API: GET conversations/lk_xxx and /audio when a person opens the call
    API->>S3: stream the recording
```

Step by step:

1. **Campaign pump (lead-gen).** An Inngest function takes a free slot, picks the next eligible lead, writes a `calls` row (the unique active-call index is the lock) and asks the provider to dial. The provider is chosen by agent id (section 4.3).
2. **Dial request (voice API).** `POST /v1/convai/sip-trunk/outbound-call` checks the concurrent-call limit (default 50), maps the agent id to a persona, creates the call row with a conversation id starting `lk_`, then calls LiveKit twice: create an **agent dispatch** to `posivex-agent` for room `call_<id>`, and **CreateSIPParticipant** to dial the number through the outbound trunk (ringtone on, 900 s maximum duration). These two calls leave from the Pakistan server; this is the only pre-call work done there.
3. **Agent starts (LiveKit Cloud).** LiveKit starts a job on the Cloud agent (cold start of 10 to 20 s if it had scaled to zero), which joins the room and waits for the phone participant. A ring timeout of 75 s ends a call nobody answers (`no_answer`).
4. **Pickup.** When Telnyx reports the call as answered, the agent starts the call clock (everything is timed from pickup), starts the recording and the greeting check, and about 1 s later speaks the scripted opening line.
5. **Conversation.** Speech-to-text, turn detection, Gemini and the voice run as described in section 7. A fast Gemini check hangs up on voicemail and phone menus. The agent can also end the call itself (`end_call`), and silence rules end calls nobody is on.
6. **End of call.** The agent classifies the outcome with one structured Gemini call over the transcript, adds up usage, and sends **one HTTPS report** to `POST /internal/calls/{id}/result` on the voice API, retrying on failures.
7. **Storage and webhook (voice API).** The API stores transcript, usage, timing events and outcome in Postgres and creates a row in `webhook_outbox`. The outbox worker signs and POSTs it to lead-gen with retries.
8. **Lead-gen processes it.** It verifies the signature, stores the raw event, acknowledges quickly, and runs the result (section 4.5): outcome policy, retry or follow-up, suppression, email, owner-detail extraction.
9. **Playback.** When someone opens the call in lead-gen, it reads the conversation and audio from the voice API (routed by the `lk_` prefix). The API streams the file from S3.

Observed timings on the latest test call: ring 8.1 s, opener requested 1.26 s after pickup, outcome stored and reported 221 s after pickup (call length 216 s).

---

## 4. The lead-gen app

**Stack.** Next.js 15 (React 19, TypeScript), Mongoose 8 on MongoDB Atlas, Inngest 3 for background work, Nodemailer for email, Zod for validation, Tailwind for the UI, NextAuth credentials with JWT for login. Its own detailed docs live in `Posivex-lead-gen/docs/` (`architecture.md`, `database-design.md`, `call-queue.md`, `call-pipeline-safety.md`, `voice-provider.md`, `agent-tools.md`, `inbound-calls.md`).

### 4.1 Layers

`src/app` (pages and thin route handlers) → `src/services` (the only code that orchestrates models) → `src/models` (Mongoose schemas) and `src/lib` (pure logic and clients). Inngest functions in `src/inngest` only call services.

### 4.2 What it stores (MongoDB `posivex`)

`leads` (one per phone number), `lead_imports`, `folders`, `folder_leads`, `campaigns`, `campaign_leads` (enrolment and dial queue), `calls` (one per attempt, with outcome, transcript turns, cost and provider ids), `provider_events` (every webhook, unmodified, deduplicated), `follow_ups`, `email_messages`, `suppressions`, `lead_notes`, `audit_events`, `users`, `app_settings` (default agent and number), plus team briefing, team work and lease collections. Calls placed through the voice API are ordinary `calls` documents whose `provider.conversationId` starts with `lk_`.

### 4.3 Routing between ElevenLabs and the voice API

`src/lib/elevenlabs.ts` is the only place that talks to a voice provider.

| Variable (lead-gen env) | Meaning |
|---|---|
| `ELEVENLABS_API_BASE_URL` | Root of the voice API (`http://127.0.0.1:8000` on the server, or the public URL from elsewhere) |
| `VOICE_API_KEY` | Sent as `xi-api-key` to the voice API. The ElevenLabs key is never sent there |
| `VOICE_API_AGENT_IDS` | Comma-separated agent ids served by the voice API. Empty means nothing is routed |
| `VOICE_WEBHOOK_SECRET` | The secret the voice API signs webhooks with; lead-gen accepts either this or the ElevenLabs secret |

- **Dial:** by agent id. A listed agent goes to the voice API, any other to ElevenLabs.
- **Read a conversation or recording:** by conversation id. The voice API's ids start with `lk_`; ElevenLabs ids never do.
- **Agent and number dropdowns:** both providers are merged. Only the listed voice agents are shown from ours. If one provider is down the other's entries still appear.
- **Our ids:** agents `agent_posivex_amelia` ("Posivex LiveKit - Amelia") and `agent_posivex_michael`, number `phnum_posivex_trunk` ("Posivex LiveKit number"). These ids must be in `VOICE_API_AGENT_IDS`.
- **Switching a campaign back:** choose an ElevenLabs agent. Calls already placed through the voice API keep being read from it by their `lk_` id.

### 4.4 Dialing safety (summary of `call-pipeline-safety.md` and `call-queue.md`)

One active call per lead and per phone number (partial unique indexes), slots taken atomically under a database lease, one pump per campaign, at most one call per pump with minimum spacing, a gradual ramp after a resume, a reaper for calls stuck over 20 minutes, a circuit breaker on a calling number after repeated account refusals, and `DISABLE_REAL_CALLS` / `DISABLE_REAL_EMAILS` switches. Queue order favours leads with known opening hours, the best local time window, eastern time zones first, and retries after "staff, no owner" ahead of retries after voicemail. Nobody is called before 9:00 in their own time zone.

### 4.5 What happens to a result

The webhook route verifies the HMAC, stores the event in `provider_events` (unique dedupe key) and hands it to Inngest `process-provider-event`, which records the call result in one transaction and then fans out: post-call email, owner-contact extraction (Gemini plus validation), follow-up analysis and the next pump. What each outcome means is a table in `src/lib/call-outcome.ts`:

| Outcome | Retry | Conversation? | Post-call email |
|---|---|---|---|
| `voicemail` | after 4 h | no | yes |
| `no_answer`, `automated_menu`, `hangup_no_speak`, `unknown_dropped` | after 24 h | no | no |
| `employee_no_owner_details`, `other` | after 24 h | yes | yes |
| `callback_set` | follow-up call | yes | yes |
| `owner_details_captured`, `info_email`, `email_comparison`, `consultant_call_scheduled` | done | yes | yes |
| `declined` | done | yes | yes |
| `do_not_call` | done and suppressed lead-wide | yes | no |

This is why the voice agent's labels matter: a mislabelled phone menu as `voicemail` earns a 4 h retry and a follow-up email, while `automated_menu` does not.

### 4.6 Background jobs (Inngest)

`process-provider-event`, `watch-call`, `post-call-actions`, `pump-campaign`, `execute-follow-up`, `analyze-follow-up`, `send-email` (throttled to 30 a minute, 2 at once), `team-briefing`, and a `scheduler-tick` cron every 2 minutes that re-triggers pumps and dispatches due follow-ups so lost events self-heal.

---

## 5. The voice API (`src/api`, `src/main.py`)

### 5.1 Processes

`python -m src.main <command>`:

| Command | What it runs |
|---|---|
| `dev` (used in production for the API container) | FastAPI app **and** the webhook outbox worker in one process; the outbox worker is started by wrapping the app's own lifespan |
| `api` | FastAPI only |
| `outbox` | Outbox worker only |
| `agent start` | A LiveKit agent worker (the server-side one, normally stopped) |

All of them run **startup checks**: the API and outbox refuse to start with the default webhook secret when lead-gen is a real host; the recording bucket is test-written once and any problem is logged (it never blocks starting).

### 5.2 Endpoints

All `/v1/convai/*` routes need the header `xi-api-key` equal to the voice API key (`POSIVEX_API_KEY`, or the ElevenLabs key if that is unset).

| Method and path | Used by | Purpose |
|---|---|---|
| `POST /v1/convai/sip-trunk/outbound-call` | lead-gen | Place a call |
| `GET /v1/convai/agents` | lead-gen | List our agents (`agent_posivex_amelia`, `agent_posivex_michael`) |
| `GET /v1/convai/phone-numbers` | lead-gen | List our number (`phnum_posivex_trunk`) |
| `GET /v1/convai/conversations` and `/{id}` | lead-gen | List and read conversations in ElevenLabs format |
| `GET /v1/convai/conversations/{id}/audio` | lead-gen | Stream the recording from S3 (descriptive 404 when there is none yet) |
| `POST /internal/calls/{id}/result` | Cloud agent | Report a finished call. Needs header `X-Posivex-Callback-Secret`. Safe to repeat |
| `GET /health`, `/health/live`, `/health/ready` | monitoring | Health and database readiness |
| `GET /metrics` | monitoring | Prometheus metrics |
| `GET /docs` | people | OpenAPI page (currently public on the hostname) |

### 5.3 Dispatching a call

`LiveKitSIPProvider.dial` creates the agent dispatch (`agent_name` from `LIVEKIT_AGENT_NAME`, default `posivex-agent`, with the whole call request as metadata) and the SIP participant. Lead-gen sends its own calling-number id (for example `phnum_posivex_trunk`), which is not a LiveKit trunk id, so only an explicit LiveKit id (`ST_…`) overrides the configured trunk (`SIP_OUTBOUND_TRUNK_ID`). The API picks the persona from the agent id; an unknown id is served by Amelia.

### 5.4 The database (Postgres 16)

Tables in `src/db/models.py`:

| Table | Holds |
|---|---|
| `calls` | One row per call: phone, persona, status (`queued`, `in_progress`, `completed`, `failed`), outcome and rationale, duration (from pickup), termination reason, summary, cost, `metadata_json` (agent id, dynamic variables, recording location) |
| `call_events` | State and system events, including the per-call `timing_*` events |
| `transcripts` | Each spoken turn with its time in the call |
| `usage_costs` | Per-provider usage and estimated cost (tokens, cached tokens, characters, audio seconds) |
| `webhook_outbox` | Webhooks to deliver: payload, destination, signature, status, attempts, next retry |

Migrations are managed with Alembic (see the runbook, section 4, for the exact command on the server). The same Postgres is shared by the API and the outbox worker.

### 5.5 The webhook outbox

When a call is finalized, one transaction stores the final state **and** a `webhook_outbox` row. The worker polls every 2 s (`--interval`), signs the body (`t=<unix>,v0=<HMAC-SHA256 of "t.body">`, same scheme as ElevenLabs, 30 min replay tolerance) and POSTs to `LEAD_GEN_WEBHOOK_URL` (`/api/webhooks/elevenlabs` is added if missing). Success is any 2xx. A failure is retried up to **10 attempts** with exponential backoff (base 2), then the row becomes `dead_letter` and needs a person. The payload is built to match what lead-gen's parser expects (`src/webhook/payload.py`): transcript turns, `metadata.phone_call`, termination reason, charging, and `analysis.data_collection_results.call_outcome`.

### 5.6 Costs and metrics inside the service

`src/metrics/costs.py` estimates per-call cost from usage. Its rates are out of date (for example Gemini Flash at $0.15 per million input tokens against about $1.50 now), so stored per-call costs understate real spend. The accurate figures are in `USAGE_COST_REPORT.md`.

---

## 6. LiveKit Cloud

| Part | Detail |
|---|---|
| Project | `project-3knots-digital-01` (`p_3c1gzyddfo8`), **Build plan**: 1 deployed agent, 5 concurrent agent sessions, 1,000 agent and 1,000 SIP minutes, 60 recording minutes, scale to zero, no rollback. Analytics and usage APIs need the Scale plan or higher |
| Rooms | One room per call, named `call_<conversation id>`. Deleting the room on close hangs up the phone leg |
| Agent dispatch | Explicit: the voice API creates a dispatch to `posivex-agent` for each call, so no call lands on an unrelated agent |
| Agent hosting | Agent `CA_NQ7cB3ZyKoWg` in **us-east**, built from this repo's `Dockerfile` on LiveKit's builders (10 minute limit) and run as a non-root user. Config in `livekit.toml`. Deployed with `scripts/deploy_agent.py` (section 12.3). Older versions are kept while draining |
| SIP | Outbound trunk `ST_Sf8PZnHSN76S` "Telnyx-NoHeader": address `sip.telnyx.com`, transport auto, number +14706000407, username and password authentication, `destination_country = us`. No inbound trunk or dispatch rule is in use |
| Egress | Audio-only room recording per call to S3 (section 9) |
| Observability | The dashboard "Sessions" shows per-call traces, logs, metrics, chat history and audio (exportable as zips). The agent's `lk agent logs` is only available while the agent is awake; logs are lost when it scales to zero |

---

## 7. The voice agent (`src/agent`)

### 7.1 What it is

A LiveKit Agents (Python, version 1.8.x) worker. `create_agent_server` builds an `AgentServer` registered under `LIVEKIT_AGENT_NAME` (`posivex-agent`), pre-warms the voice-activity model once per process, and runs `run_agent_session` for each call. The persona (Amelia or Michael, the same prompt with names swapped) comes from the dispatch metadata.

### 7.2 The pipeline for one conversation

```
callee audio ► LiveKit SIP ► agent
   ► VAD (Silero) + turn detector ("v1" audio model, via LiveKit Inference) decides when they finished
   ► Deepgram Nova-3 (en-US) streaming speech to text
   ► Gemini 3.5 Flash (system prompt + knowledge base ≈ 40,000 characters, ≈ 11,000 tokens per request)
        tools: end_call, skip_turn, voicemail_detection
   ► ElevenLabs eleven_v3_conversational (streaming) text to speech
► LiveKit ► SIP ► callee
```

| Stage | Setting |
|---|---|
| Speech to text | Deepgram `nova-3`, `en-US`, interim results on, 25 ms endpointing |
| Turn handling | Min end-of-turn wait 0.5 s, **max 1.5 s**, interruption needs 1 transcribed word, a callee's audio keeps reaching speech-to-text while the opener plays |
| LLM | `gemini-3.5-flash`, temperature 0, thinking level minimal, fallback `gemini-3.5-flash-lite` through LiveKit's fallback adapter (10 s timeout, no retries). Preemptive generation is on (LiveKit default), so the model starts answering before the turn is confirmed |
| Text to speech | ElevenLabs `eleven_v3_conversational`, voice id `eXpIbVcVbLo8ZJQDlDnl` (Amelia), 24 kHz PCM |
| Prompt | `prompts/<persona>/system.j2` rendered with the lead's details (business name, context, current local time, follow-up notes, contact name) plus `prompts/knowledge/Knowledge-base.md` |

### 7.3 What the prompt and tools do

One system prompt (Steps A to F) drives the whole call; there is no state machine. Three tools: `end_call` (ends the call after the closing line; guarded so the model must speak first), `skip_turn` (stay silent while the callee fetches someone), and `voicemail_detection` (kind `voicemail` or `menu`). A do-not-call request makes the code speak the exact apology if the model did not.

### 7.4 The life of a call inside the agent (current behaviour)

1. **Wait for the phone participant** and watch `sip.callStatus` (75 s limit; a callee who leaves while ringing ends the call at once as `no_answer`).
2. **Pickup:** start the call clock, start the recording, start the greeting check, record `timing_pickup` (with ring time).
3. **Opener, about 1 s after pickup:** wait up to 1 s more only if the callee is mid-sentence, then speak "Hi, this is Amelia from Clover Posivex. Am I speaking with the owner of …?" The opener cannot be interrupted. The model's replies are held until then so it never double-introduces.
4. **Greeting check (`greeting_detector.py`):** a small Gemini call (`gemini-3.5-flash-lite`, about 0.9 s) reads what the callee has said so far as it streams in and classifies it as person, voicemail, menu or unclear, asking again as words arrive for up to 25 s. At confidence 0.8 or more on voicemail or menu it interrupts, hangs up and labels the call `voicemail` or `automated_menu`. The `voicemail_detection` tool is the fallback.
5. **Overlap:** if the callee answered over the opener, the agent replies to a real answer right after it (not to a bare "Hello?", an echo, or an unconfirmed greeting).
6. **Conversation:** replies follow the prompt. Backchannel words ("mm-hmm") are ignored while the agent speaks. If one caller turn runs 8 s, it is committed so the agent answers. A goodbye without `end_call` is rescued 2.5 s later.
7. **Silence:** no words for 15 s ends the call, after one "Hello?" nudge if the callee never spoke (the nudge now re-arms the timer). A 40 s no-speech watchdog also ends calls where the line is not silent but no words are heard. `skip_turn` holds for 30 s. The call is capped at 900 s.
8. **End:** the outcome is chosen, timing events stored, and one report is sent to the voice API.

Time-based decisions use the call clock from pickup, not from job start.

### 7.5 How the outcome is chosen

In order: a forced outcome (voicemail or menu detected, unanswered, no one ever spoke); a callee who only spoke before our opener and never replied is `hangup_no_speak`; otherwise **one structured Gemini call** over the transcript picks one of 12 outcomes using the ordered rules in `prompts/analysis/call_outcome.md` (the same rules the ElevenLabs agent used), returning outcome, rationale and a summary. If the classifier fails the result is `other`, or `do_not_call` when the apology was spoken. Each call also records the cached share of LLM input tokens.

### 7.6 Recording the call without writing to the server

On LiveKit Cloud the agent cannot (and should not) reach the Pakistan Postgres. It uses `RemoteRecorder`, which keeps events, transcript, usage and recording info in memory during the call and sends them in one `POST /internal/calls/{id}/result` at the end, with up to 4 attempts. The API stores it and stages the webhook. The server-side worker, when it was used, wrote to Postgres directly through `CallRecorder`.

### 7.7 Per-call timing events

Stored with each call in `call_events`: `timing_pickup`, `timing_first_callee_speech`, `timing_greeting_check`, `timing_machine_detected`, `timing_opener_started`, `timing_opener_finished`, `timing_replying_to_overlap`, `timing_silence_nudge`, `timing_no_speech_watchdog`, `timing_call_end`, each with `since_pickup_secs`. Read them on the server with:
`SELECT event_type, payload FROM call_events WHERE call_id = 'lk_…' AND event_type LIKE 'timing_%' ORDER BY timestamp;`

---

## 8. Telephony and SIP

```
Cloud agent ── LiveKit room ── LiveKit SIP (outbound trunk ST_Sf8PZnHSN76S, destination_country us)
                                  │  SIP over the internet, username/password auth
                                  ▼
                           Telnyx  sip.telnyx.com   (caller ID +1 470 600 0407)
                                  ▼
                              phone network ► the business
```

- **Carrier:** Telnyx. The trunk authenticates with a Telnyx SIP username and password. Outbound US calls cost about $0.005 a minute (check the Telnyx call report for the true rate).
- **Region of the phone leg:** LiveKit starts an outbound SIP leg in the region of whoever makes the dial request. That request comes from Pakistan, so `destination_country = us` was set on the trunk (9 Nov) to start it on a US server (Ashburn, Chicago or Phoenix). Whether the LiveKit **room** is also in the US is not documented by LiveKit and cannot be read after a call; the dashboard session page may show it.
- **The same number is also registered in the ElevenLabs workspace** (as `phnum_0301m4trff2te4zbjxyaxne8y621`, "Telnyx No 4") for the legacy path. Lead-gen should use `phnum_posivex_trunk` for LiveKit calls. The voice API ignores any calling-number id that is not a LiveKit trunk id and dials through its own trunk either way.
- **Failure codes** surface in lead-gen: SIP 404, 604, 410 and 484 mark a number invalid after one refusal; 486, 603, 480, 408 and 600 are calls that reached nobody; 401, 403 and 407 mean the carrier refused our account (circuit breaker).

---

## 9. Audio storage

| Item | Detail |
|---|---|
| Recorder | LiveKit **room-composite egress**, audio only, started at pickup, stops when the room closes |
| Where | AWS S3 bucket `posivex-bucket`, region `us-east-1` |
| Key | `recordings/<conversation id>.ogg` (e.g. `recordings/lk_65ffa21de3214a.ogg`) |
| Credentials | AWS access key and secret in `RECORDING_*` settings: given to LiveKit with each egress request, and used by the voice API to read the files. They are root-account keys by choice, which is the weakest option |
| Location is stored | The egress id, bucket and key are saved in the call's `metadata_json.recording` |
| Playback | `GET /v1/convai/conversations/{id}/audio` streams the file from S3. Lead-gen plays it on the call page. The 404 message says why there is nothing to play (turned off, still processing, missing from the bucket) |
| Checked at start | Both processes test-write and delete one object at startup and log a clear message if the bucket, region or keys are wrong |
| Retention | None configured: files stay until deleted |
| Quota | Build includes 60 recording minutes a month; about 45 were used by 10 Nov |

---

## 10. Data, in plain terms

| Data | Lives in | Written by | Read by |
|---|---|---|---|
| Leads, campaigns, enrolment, calls (lead-gen view), emails, suppressions | MongoDB Atlas | Lead-gen | Lead-gen |
| Raw webhooks | MongoDB `provider_events` | Lead-gen webhook route | Lead-gen jobs |
| Call record, transcript, events, usage, outbox | Postgres on the server | Voice API (from the agent's report) | Voice API, lead-gen through `/v1/convai/conversations` |
| Audio | S3 `posivex-bucket` | LiveKit egress | Voice API streaming |
| Live call state | Agent memory on LiveKit Cloud | Cloud agent | Cloud agent only; gone when the call ends |
| Session traces, logs, metrics | LiveKit Cloud observability | LiveKit | You, in the dashboard |

A call is identified by one id (`lk_xxxxxxxxxxxxxx`) everywhere: the Postgres `calls.id`, the LiveKit room `call_<id>`, the S3 key, and lead-gen's `provider.conversationId`.

---

## 11. Security, secrets and known gaps

### 11.1 Who proves what to whom

| Link | Mechanism |
|---|---|
| Lead-gen to voice API | Header `xi-api-key` = `VOICE_API_KEY` in lead-gen = `POSIVEX_API_KEY` on the API (constant-time compare) |
| Cloud agent to voice API | Header `X-Posivex-Callback-Secret` = `RESULTS_CALLBACK_SECRET` (the endpoint answers 503 if unset). The deploy script checks this before it deploys |
| Voice API to lead-gen | HMAC-SHA256 signature `t=…,v0=…` with `ELEVENLABS_WEBHOOK_SECRET` on the API, accepted by lead-gen as `VOICE_WEBHOOK_SECRET` |
| Voice API to LiveKit | LiveKit API key and secret |
| LiveKit to Telnyx | SIP username and password in the trunk |
| LiveKit and the API to S3 | AWS access key and secret |
| People to lead-gen | NextAuth credentials and roles (admin, member) |

### 11.2 Where the secrets are (names only)

- **Voice server `.env`:** `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `GEMINI_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, `POSIVEX_API_KEY`, `ELEVENLABS_WEBHOOK_SECRET`, `LEAD_GEN_WEBHOOK_URL`, `RESULTS_CALLBACK_URL`, `RESULTS_CALLBACK_SECRET`, `SIP_OUTBOUND_TRUNK_ID`, `VOICE_PHONE_NUMBER(_ID)`, `RECORDING_*`, `POSTGRES_*`, `DATABASE_URL`.
- **Cloud agent secrets** (filtered from `.env.prod` by the deploy script): only `GEMINI_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, `RESULTS_CALLBACK_URL`, `RESULTS_CALLBACK_SECRET`, `RECORDING_*`, `TURN_DETECTOR_MODE`, `LIVEKIT_AGENT_NAME`. LiveKit injects its own URL and keys. The database URL and webhook secrets never leave the server.
- **Lead-gen `.env`:** MongoDB URI, NextAuth secret, admin credentials, ElevenLabs key and webhook/initiation secrets, SMTP credentials, Gemini key, Inngest settings, agent tool secret, and the voice-API variables in 4.3.

### 11.3 Known gaps (as of 10 Nov 2026)

1. **Credentials were pasted into chat** during development (MongoDB, ElevenLabs, Gemini, SMTP, LiveKit, AWS, webhook secrets). Rotate them.
2. **`ALLOW_UNSIGNED_WEBHOOKS=true`** on lead-gen: unsigned webhooks are accepted. Set it to `false`.
3. **AWS root keys** are used for the bucket. Create a limited key for one bucket.
4. **`/docs` is public** on the API hostname, and every `/v1/convai/*` route is reachable on the internet (key-protected). Only `/internal/` and `/health` need to be public; an nginx lockdown is recommended.
5. **The compose file's Postgres default password** (`posivex_secure_pass`) applies if `POSTGRES_PASSWORD` is not set.
6. **No retention policy** for recordings, transcripts or webhook payloads.

---

## 12. Operations

### 12.1 Deploying the voice API (server)

Pull the repo on the server, then `docker compose up -d --build posivex_api`. When a release adds a migration, apply it afterwards with `docker compose exec posivex_api alembic upgrade head` (see the runbook, section 4). Verify `GET /health/ready`.

### 12.2 Lead-gen

Normal Next.js build and restart; run `npm run db:indexes` after a deploy that adds indexes. Changing `VOICE_API_AGENT_IDS` needs a restart.

### 12.3 The Cloud agent

`uv run python scripts/deploy_agent.py` (add `--dry-run` first): reads `.env.prod`, sends only the secrets the agent needs, checks that the voice API's results endpoint accepts the callback secret before building, then runs `lk agent create` the first time or `lk agent deploy` afterwards (`livekit.toml` holds the agent id). The deploy is built from the working tree. Check with `lk agent status` and `lk agent versions`. The Build plan has no rollback; redeploy the previous commit instead.

### 12.4 Looking at a call

- **Lead-gen:** the call page (transcript, outcome, recording).
- **LiveKit dashboard:** the session page, with traces, logs, metrics and audio; export as a zip for analysis.
- **Voice server:** the `call_events` timing query in 7.7; `webhook_outbox` rows with `status = 'dead_letter'` need attention.
- **Logs:** `lk agent logs` only while the agent is awake; structured JSON logs and `docker compose logs posivex_api` on the server.

### 12.5 Tests

About 240 automated tests (`uv run pytest`) cover the agent logic, the greeting detector, outcome rules, API and lead-gen compatibility, the webhook, persistence and the deploy script. Lint is `uv run ruff check src tests`. No live-call tests exist: new call behaviour is checked by a test round and the session export.

---

## 13. Settings reference

Defaults live in `config/defaults.yaml` (and `src/config.py`); environment variables override.

| Area | Setting | Default |
|---|---|---|
| Calls | `telephony.max_concurrent_calls` | 50 (API-side limit) |
| Call length | `conversation.max_duration_seconds` | 900 |
| Ringing | `conversation.ring_timeout_seconds` | 75 |
| Start of call | `opener_delay_seconds` / `opener_quiet_wait_seconds` | 1.0 / 1.0 |
| Machine check | `machine_detection_enabled` / `_model` / `_window_seconds` / `_confidence` | true / `gemini-3.5-flash-lite` / 25 / 0.8 |
| Turns | `min_endpointing_delay` / `max_endpointing_delay` | 0.5 / 1.5 |
| Silence | `silence_end_call_seconds` / `no_speech_watchdog_seconds` | 15 / 40 |
| Hold | `hold_duration_seconds` | 30 |
| Long caller turn | `max_user_speech_seconds` | 8 |
| LLM | `llm.default_model` / `fallback_model` (`LLM_DEFAULT_MODEL`, `LLM_FALLBACK_MODEL`) | `gemini-3.5-flash` / `gemini-3.5-flash-lite` |
| Turn detector | `TURN_DETECTOR_MODE` | `vad` in code, `v1` in production |
| Webhook | `webhook.max_retries`, `retry_backoff_base` | 10, 2.0 |
| Recording | `RECORDING_ENABLED`, `_BUCKET`, `_REGION`, `_ACCESS_KEY`, `_SECRET`, `_PREFIX` | off by default, on in production |

---

## 14. Regions, latency and cost

**Regions.** Lead-gen, the voice API, Postgres and the nginx proxy are in Pakistan. The Cloud agent is us-east (Virginia), the S3 bucket us-east-1, and the phone leg is pinned to a US region. Deepgram, Gemini and ElevenLabs are called from the agent (us-east) on their default endpoints. Nothing in Pakistan is in the audio path: only the dial request before the call and the report and webhook after it.

**Latency (one real test call, 11 reply turns).** Median time from the callee stopping to the agent's audio: 3.6 s before the latest changes (end-of-turn wait reaching 4 s on short replies, Gemini time to first token median 1.5 s with a 6.9 s spike, speech-to-text 0.2 to 0.3 s, voice first audio 0.3 s). The end-of-turn cap is now 1.5 s. Gemini time to first token is the largest remaining delay.

**Cost.** About $0.08 per answered minute on the custom stack against about $0.11 to $0.13 on ElevenLabs, with the break-even near 2,900 dials a month and an estimated saving of about $190 a month at 20,000 dials. Prompt caching currently serves about 68% of input tokens. Details and assumptions are in `USAGE_COST_REPORT.md`.

---

## 15. Not built, not known, and next steps

**Not built on the voice API** (these stay on ElevenLabs agents): the inbound-call initiation webhook, team briefing, live transfer, demo-ready mode and the demo-callback agent. A booked demo on a LiveKit call therefore does not notify the team, and the agent promises a calendar invite nothing sends. Real leads should not be sent for demo bookings until these are ported.

**Not known from here:** the nginx configuration, Telnyx portal settings, which region hosts the LiveKit room, and the live false-positive rate of the voicemail check (it needs a round of real calls).

**Planned or recommended:** make Gemini faster and cheaper (shorter prompt, reorder it so repeated calls share a cached prefix, test Flash-Lite as the main model); upgrade LiveKit to a paid plan before running real volume (recording quota, concurrency 5, scale to zero); fix the security gaps in 11.3; port the missing tools; consider a retention policy; update `src/metrics/costs.py` rates.

---

## 16. File map (this repo)

| Path | Purpose |
|---|---|
| `src/main.py` | Entry points: `agent`, `api`, `outbox`, `dev`; startup checks |
| `src/api/` | FastAPI app, dependencies (auth) and routes (`calls`, `convai_emulation`, `convai_read`, `internal`, `health`) |
| `src/agent/session_handler.py` | The per-call flow: pickup, opener, silence, hold, shutdown and reporting |
| `src/agent/persona_agent.py` | The agent class, its three tools and the call state |
| `src/agent/greeting_detector.py` | The voicemail and phone-menu check |
| `src/agent/opener.py`, `silence.py`, `call_clock.py` | Overlap replies, the nudge and watchdog, the pickup clock |
| `src/analysis/outcome.py` | The post-call outcome classifier and rules |
| `src/persistence/` | `CallRecorder` (Postgres) and `RemoteRecorder` (HTTPS report from the Cloud agent) |
| `src/webhook/` | Payload builder, HMAC signer, outbox worker |
| `src/telephony/` | LiveKit SIP dialer and the recording egress |
| `src/storage.py` | S3 access for playback and the startup check |
| `src/providers/` | Deepgram, Gemini, ElevenLabs and VAD factories |
| `src/prompts/` and `prompts/` | Prompt rendering and the persona prompts, knowledge base and outcome rules |
| `src/db/` | SQLAlchemy models and Alembic migrations |
| `scripts/deploy_agent.py`, `setup_telnyx_trunk.py` | Deploy to LiveKit Cloud; create the SIP trunk |
| `config/defaults.yaml` | Default settings, agents and phone numbers |
| `docs/` | `RUNBOOK.md` (operating guide), `USAGE_COST_REPORT.md`, `DECISIONS.md`, and this file |
