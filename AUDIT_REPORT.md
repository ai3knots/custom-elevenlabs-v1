# Audit report: Posivex voice agent (Phase 1 and 2)

Status: **audit only. No code, config, or Docker file has been changed.** This file is the only thing added.
Waiting for approval before any removal or restructuring (see section G for the decisions I need from you).

Baseline measured before writing this (so every later step can be compared against it):

| Check | Result |
|---|---|
| `pytest` | 194 passed, 10.5 s |
| `ruff check .` | 2 errors, both in `scripts/` (`end_calls.py` import order, `setup_telnyx_trunk.py:112` stray `f`) |
| `pyright` (basic mode) | 49 errors: 4 in `src/`, 45 in `tests/` (all one kind: indexing `TurnHandlingOptions`, a `TypedDict` with optional keys) |
| Deprecation warnings during tests | none from LiveKit; one `ResourceWarning` from `aiosqlite` in tests |
| Installed versions | livekit-agents 1.8.10 (all plugins pinned to it), fastapi 0.162.0, pydantic 2.14.2, sqlalchemy 2.1.6, uvicorn 0.54.0, google-genai 2.34.0 |

Method limits: static reading of every file in `src/`, `scripts/`, `config/`, `alembic/`, Docker files; grep and `vulture` (run once via `uvx`, not added to the project) for unused code; installed-package source for LiveKit behaviour; LiveKit docs pages listed in section C. **I did not build the Docker image, start the API, or place a call**, so Docker findings are from reading the files, and "dead" findings are static (see rule 4: dynamic use is flagged "uncertain", not removed).

---

## Phase 1. What exists, and what must be preserved

### Layout

| Area | Where | Notes |
|---|---|---|
| Entrypoint (single CLI) | `src/main.py`, `python -m src.main {agent,api,outbox,dev}` | Docker `ENTRYPOINT`; also what LiveKit Cloud runs (`agent start --persona amelia`) |
| FastAPI app | `src/api/` (`app.py` factory, `deps.py`, `routes/{health,calls,convai_emulation,convai_read,internal}.py`) | |
| LiveKit agent worker | `src/agent/` (`session_handler.py` 891 lines, `persona_agent.py`, `greeting_detector.py`, `opener.py`, `silence.py`, `call_clock.py`, `interruption.py`) | |
| Providers | `src/providers/` (llm, stt, tts, vad, mock) | Factory functions around the LiveKit plugins |
| Persistence | `src/db/` (models, base), `src/persistence/` (`recorder.py` DB, `remote.py` HTTP), `alembic/` | Postgres (asyncpg) |
| Webhooks | `src/webhook/` (payload builder, HMAC signer, outbox worker) | |
| Telephony | `src/telephony/` (livekit_sip, mock, console, recording egress) | |
| Other | `src/config.py` (470 lines), `src/storage.py` (S3), `src/analysis/outcome.py`, `src/metrics/`, `src/prompts/`, `src/startup_checks.py`, `src/logging.py` | |
| Config | `config/defaults.yaml`, `config/profiles/{amelia,michael}.yaml`, `prompts/`, `livekit.toml` | |
| Docker | `Dockerfile` (2-stage, uv), `docker-compose.yml` (postgres, posivex_agent, posivex_api), `.dockerignore` | The same image serves the API, the local agent, and the LiveKit Cloud agent |
| Scripts | `scripts/{call,end_calls,deploy_agent,setup_telnyx_trunk,smoke_test}.py` | |
| Not code | `reference/` (202 files, 2.6 MB), `spikes/` (2 files), `docs/` (13 files), `CONTEXT_NOTES.md` (819 lines) | |
| Untracked, local only | `testing-frontend/`, `docker-compose.local-test.yml`, `scripts/*.ps1` | Left out of everything below |

Tracked files: 318. Of those, 202 are under `reference/` (vendored LiveKit examples and ElevenLabs config backups); ruff, Docker, and the code never touch them.

### Flow to preserve

```
lead-gen (Next.js) --xi-api-key--> POST /v1/convai/sip-trunk/outbound-call   (FastAPI, your server)
   FastAPI: concurrency check -> insert `calls` row (queued) -> LiveKit agent_dispatch.create_dispatch (metadata = CallRequest JSON)
                                                              -> sip.create_sip_participant (dials via the Telnyx trunk)
   LiveKit Cloud: starts the agent job (Cloud agent CA_NQ7cB3ZyKoWg, scale-to-zero) in room call_<id>
   Agent job (run_agent_session): connect -> parse metadata -> build STT/VAD/TTS/LLM -> AgentSession.start
        -> wait for the SIP callee -> wait for sip.callStatus=active -> pickup: clock, recording egress, greeting check
        -> opener (uninterruptible) -> conversation (one prompt, 3 tools: end_call, skip_turn, voicemail_detection)
        -> session close -> shutdown callback: outcome classification, cost, then
             RemoteRecorder POST /internal/calls/{id}/result (X-Posivex-Callback-Secret)   [or direct DB when no callback URL]
   FastAPI: stores transcript/events/usage, finalizes the call, writes a webhook_outbox row in the same transaction
   Outbox worker (same process in `dev` mode): signed POST (t=...,v0=...) to lead-gen /api/webhooks/elevenlabs, 10 attempts, backoff
   lead-gen also reads GET /v1/convai/{agents,phone-numbers,conversations,conversations/{id},conversations/{id}/audio}
```

Contracts that a refactor must not touch: every route path, status code and response shape above (including `400` instead of FastAPI's `422` for validation, `lk_` conversation ids, the ElevenLabs-shaped payload), the env var names, the agent ids (`agent_posivex_amelia/michael`), the prompts in `prompts/`, the three tool signatures (the model sees their docstrings and parameter names), `agent_name` `posivex-agent`, the DB schema, and the HMAC/webhook format.

---

## A. Dead code and removal candidates

Confidence: **high** = no reference anywhere except its own definition or a re-export; **medium** = used only by tests/scripts, or by a config the system might set; **low** = may be used dynamically or by the other repo.
"Verified by" is grep over `src scripts tests config alembic docker-compose.yml`, plus `vulture` for the list of candidates.

### A1. Code

| # | Where | What | Why it looks unused | Conf. | Verified by |
|---|---|---|---|---|---|
| 1 | `src/db/base.py:41` `get_db_session` (re-exported in `src/db/__init__.py`) | transactional session generator | no caller; the API uses `CallRecorder`, which opens its own sessions | high | grep: definition and re-export only |
| 2 | `src/logging.py:128` `get_logger` | wrapper over `logging.getLogger` | zero references | high | grep |
| 3 | `src/metrics/costs.py:18` `RATE_ELEVENLABS_TURBO_PER_CHAR` | rate constant | zero references | high | grep |
| 4 | `src/metrics/prometheus.py` `POSIVEX_CALL_OUTCOMES_TOTAL`, `POSIVEX_CALL_DURATION_SECONDS`, `POSIVEX_COST_DOLLARS_TOTAL`, `POSIVEX_ACTIVE_CALLS` (and their re-export in `src/metrics/__init__.py`) | metrics that are never updated | only `POSIVEX_CALLS_TOTAL` (API) and `POSIVEX_WEBHOOK_DELIVERIES_TOTAL` (worker) are incremented; `POSIVEX_ACTIVE_CALLS` is set only in `tests/test_metrics.py`. The agent runs in a different process, so it could not feed the API's `/metrics` anyway | high (never incremented) | grep |
| 5 | `src/db/models.py:207,220` `AgentConfigVersion`, `Experiment` (tables `agent_config_versions`, `experiments`) | tables nobody reads or writes | only the initial migration and the re-export. Removing the models needs a migration, so this is a **decision**, not a cleanup | high (code) / needs decision (tables) | grep |
| 6 | `src/agent/persona_agent.py:115,129` `_test_session`, `attach_test_session`, and the `active_session` branch for it | test hook | no test calls `attach_test_session` (tests build their own mocks) | high | grep: 1 definition, 0 callers |
| 7 | `src/telephony/livekit_sip.py:38` `call_id = request.call_id or f"conv_..."` (same in `console.py`, `mock.py`) | fallback id | both API routes set `request.call_id` before dialing, so the fallback never runs. It also uses a different prefix (`conv_`) than the real path (`lk_`) | medium | reading both routes |
| 8 | `src/persistence/recorder.py:112` `record_transcript` (single-row) | | used only by `scripts/smoke_test.py` and `tests/test_persistence.py`; production uses the batch method | medium | grep |
| 9 | `src/telephony/base.py` `hangup` (all 3 providers) | | only called by `scripts/smoke_test.py` and tests; production ends calls by deleting the room | medium | grep |
| 10 | `src/api/routes/calls.py` (`POST /api/v1/calls`, `GET /api/v1/calls/{id}`) | the native, non-ElevenLabs dial API | lead-gen uses `/v1/convai/*`; only `scripts/call.py` uses it. **No authentication** (see B-sec) | medium (manual tool still uses it) | grep, `scripts/call.py:75,112` |
| 11 | `src/api/routes/convai_emulation.py:166` `/api/webhooks/elevenlabs/initiation` + `InboundInitiationRequest` | inbound-call variables lookup | no caller in this repo; `/v1/convai/phone-numbers` says `supports_inbound: False`. Could be called by lead-gen or ElevenLabs | **low** | grep; cannot see the other repo's runtime |
| 12 | `src/api/routes/convai_emulation.py:151` `/v1/convai/twilio/outbound-call` | second dial route, identical to the SIP one | lead-gen may use either | **low** | grep |
| 13 | `src/providers/stt.py:52` `provider == "assemblyai"` branch | raises `NotImplementedError` | also in the `STTConfig` Literal | medium | reading |
| 14 | `src/config.py:82` `TurnDetectorConfig.mode` values `"multimodal"`, `"eou"` | accepted, then logged and replaced with VAD in `session_handler.py:228` | | medium | reading |
| 15 | `src/providers/mock.py` (152 lines) | test doubles shipped in `src/`, imported by `llm.py`, `stt.py`, `tts.py`, `vad.py` | selectable by config (`provider: mock`), used by tests; the prod image carries them | low (keep, see D/E) | grep |
| 16 | `src/__init__.py` `__version__`, `src/api/__init__.py` re-exports `app` | | `app` is imported nowhere except `src/api/__init__`; see B-14 for why that matters | medium | grep |

### A2. Config keys and env vars

| # | Where | What | Conf. | Verified by |
|---|---|---|---|---|
| 1 | `Settings.redis_url`, env `REDIS_URL`, `.env.example`, `README` ("Postgres & Redis") | no Redis client anywhere; `scripts/deploy_agent.py:57` itself says "not used" | high | grep, no `redis` dependency |
| 2 | `TelephonyConfig.sip_phone_number_id`, env `SIP_PHONE_NUMBER_ID` | read into settings, never used; `setup_telnyx_trunk.py:114` prints it | high | grep |
| 3 | `TTSConfig.encoding`, `TTSConfig.expressive` (and the YAML values) | never read; `get_tts` passes neither | high | grep, vulture |
| 4 | `WebhookConfig.max_retries` (`config/defaults.yaml`) | never read; the real limit is `WebhookOutbox.max_attempts` (default 10, `db/models.py:195`). Two knobs that look like the same thing | high | grep |
| 5 | `Settings.environment` | never read | high | vulture and grep |
| 6 | `Settings.gender`, `Settings.display_name` | only asserted in tests; `PromptManager.display_name` derives the name from the persona itself | medium | grep |
| 7 | `.env.example`: `ASSEMBLY_AI_API_KEY`, `AGENT_ID`, `REDIS_URL`, `SIP_PHONE_NUMBER_ID` | nothing reads them (`AGENT_ID` is an "old ElevenLabs agent" per `deploy_agent.py:63`) | high | grep |
| 8 | **Missing** from `.env.example`, though the code reads them: `LLM_DEFAULT_MODEL`, `LLM_FALLBACK_MODEL`, `TURN_DETECTOR_MODE`, `LIVEKIT_AGENT_NAME`, `ALLOW_DEFAULT_CALL_REQUEST`, `LOG_LEVEL`, `RECORDING_PREFIX`, `RECORDING_FORCE_PATH_STYLE`, `POSTGRES_*` (compose) | documentation gap, not dead code | n/a | grep of `os.getenv` against the file |
| 9 | `docker-compose.yml` `posivex_agent` service | unused in production now (the agent runs on LiveKit Cloud, and you stopped this one). Keeping it is a risk, see E-5 | **needs decision** | compose + `docs/ARCHITECTURE.md` |

### A3. Files, modules, scripts, assets

| # | Path | Assessment | Conf. |
|---|---|---|---|
| 1 | `reference/` (202 files, 2.6 MB: 173 vendored LiveKit examples, ElevenLabs agent exports incl. 11 `live-agent-backup-before-*.json`) | Not imported by code; excluded from ruff and from Docker. Only a test comment mentions the path. Is reference material you chose to keep, so **not** a removal candidate unless you say so | low |
| 2 | `spikes/` (`check_gemini.py`, `inspect_deep.py`, 102 lines) | One-off experiments, excluded from ruff/Docker, no references | medium |
| 3 | Stale docs: `CONTEXT_NOTES.md` (describes `lk_inspect_venv`, an ElevenLabs agent as current), `docs/PROGRESS.md` (says "7-state machine" and "the LLM has no hangup tool"; the code now has a one-prompt agent with an `end_call` tool), `docs/ARCHITECTURE_PLAN.md`, `docs/SINGLE_PROMPT_MIGRATION_PLAN.md`, `docs/SPIKES.md`, `docs/CALL_ANALYSIS_2026-11-04.md` | Superseded by `docs/ARCHITECTURE.md`, `RUNBOOK.md`, `DECISIONS.md`. Archive or delete; does not touch runtime | medium |
| 4 | `README.md` | Says Docker is for "Postgres & Redis"; lists no run steps for the API/agent split | medium |
| 5 | `scripts/end_calls.py`, `scripts/call.py`, `scripts/smoke_test.py`, `scripts/setup_telnyx_trunk.py`, `scripts/deploy_agent.py` | Manual tools. All still reference live code. `deploy_agent.py` is used for every deploy | keep |
| 6 | `.dockerignore` | Missing `livekit-recordings/`, `docs/`, `tests/`, caches (see E-3). Be careful: **do not add `*.md`**, the knowledge base and classifier prompt are `.md` files inside `prompts/` that the image needs | n/a |
| 7 | `.gitignore` entries `lk_inspect_venv/` | the venv no longer exists | low |

### A4. Dependencies (`pyproject.toml`)

| Dependency | Finding |
|---|---|
| All 19 runtime dependencies | each is imported somewhere (checked by reading imports). `greenlet` is redundant with `sqlalchemy[asyncio]` but harmless; `asyncpg` is only imported by `prewarm_process` and used through the DB URL |
| **`google-genai`** | imported directly (`from google import genai` in `greeting_detector.py:73`, `analysis/outcome.py:97`, `google.genai.types` in `providers/llm.py`) but **not declared**; it only arrives transitively through `livekit-plugins-google`. Pyright also flags `genai` as an "unknown import symbol" on both lines. Proposal: declare it (needs your approval, section G) |
| `pre-commit` (dev) | declared, but there is no `.pre-commit-config.yaml` and no `.github/` |
| `aiosqlite` (dev) | needed by the test suite (SQLite in memory); keep |
| `redis` | not a dependency (consistent with A2-1) |

### A5. Docker and compose

| Item | Finding | Conf. |
|---|---|---|
| `Dockerfile:34` `RUN uv run python -m livekit.agents download-files` | **No-op today.** `download-files` calls `download_files()` on each installed plugin; none of the installed plugins (deepgram, elevenlabs, google, silero) override it (checked in `.venv/.../livekit/plugins` and `agents/plugin.py:39`). It costs build time (LiveKit Cloud has a 10-minute build limit) and, as a `uv run`, can re-resolve the environment. It would also download to `/root/.cache` in the builder stage, which is not copied into the runtime stage, so if a plugin that needs model files is added later, the files would still be missing at runtime | high |
| `gcc g++ python3-dev libglib2.0-0` in the builder | Probably only needed if a wheel has to compile; cannot verify without a build, so left alone | low |
| Compose ports | postgres has no published port (good); API is bound to `127.0.0.1:8000` (good) | n/a |

---

## B. Code quality issues

### B1. Structure

1. **`run_agent_session` is a 610-line function (`session_handler.py:241-849`) with 26 nested functions.** All its state is mutable dicts captured by closures: `activity`, `opener_state`, `first_speech`, `termination`, `call_flags`, `closing_task`, `long_turn_task`, `watchdog_task`, `event_tasks`. Several closures use names assigned *later* in the function (`handle_machine` at line 579 uses `signals`, assigned at 658; `PersonaAgent(on_end_call=lambda...)` at 408 uses `end_call` defined at 481). It works because of late binding, but it makes order matter and hides bugs. It has **no direct test**: no file in `tests/` references `run_agent_session` or `create_agent_server`. This is the highest-value and highest-risk item in the report.
2. `src/config.py` (470 lines) mixes schemas, the YAML/env loader, call-request validation, and 70 lines of hand-written env mapping.
3. `session_handler.py` also holds metadata parsing, VAD prewarm, turn-handling options, cost folding, and the server factory.

### B2. Duplicated logic

| Where | What |
|---|---|
| `api/routes/calls.py:22-109` vs `api/routes/convai_emulation.py:35-133` | the dial flow (concurrency check, `create_call`, `call_requested` event, dial, failure handling, status update, `call_dispatched`) is about 90% identical. Differences that matter: error `502` vs `500`, event payload keys, response body, `lk_` vs `conv_` id prefix |
| `session_handler.py:329-333` and `providers/llm.py:76-81` | the shared `_thought_signatures` dict is set up twice, both times through `object.__setattr__` on a private attribute |
| `storage.py:23` `_client` and `storage.py:52` inline client | the same `boto3.client(...)` arguments written twice (the second adds timeouts) |
| `convai_read.py:114` and `webhook/payload.py:47` | two copies of `_utc` |
| `session_handler.py:639` `call_state.user_turns >= 3` and `persona_agent.py:55` `MACHINE_GUARD_MAX_USER_TURNS = 3` | the same threshold written twice |
| `"+14045550000"` | placeholder number written twice in `parse_call_request_from_room` |
| three `TelephonyProvider` implementations | each builds its own `DispatchResult` ids |

### B3. Magic numbers and strings

Scattered literals that are behaviour-relevant and not in config: `0.25` (opener quiet window, `session_handler.py:819`), `0.05` poll step, `0.5` answer poll, `30.0` shutdown timeout, `CLOSING_GRACE_SECS=2.5`, `4`/`1.0`/`10.0` in `RemoteRecorder`, `STALE_ACTIVE_CALL_SECS`, `1800` webhook tolerance, the 3-turn guard, `"3.8" in model` (`providers/llm.py:47`, a substring hack on the model name), and the opener sentence `"Hi, this is {name} from Clover Posivex..."` hard-coded in `prompts/manager.py:45` rather than in `prompts/`.

### B4. Error handling and logging

| Finding | Detail |
|---|---|
| 25 `except Exception` in `src/`, 0 bare `except:`, 0 `print` | Most are deliberate "never break a live call" guards and they log. Silent ones worth reviewing: `session_handler.py:131` (`_prewarmed_vad` returns `None` with no log) and `providers/llm.py:42` (falls back to a substring check) |
| Private LiveKit APIs | `providers/llm.py:39` imports `livekit.plugins.google.llm._supports_thinking_level`; two `object.__setattr__(llm, "_thought_signatures", ...)`. The exact pin (`==1.8.10`) contains the risk, but an upgrade can break them silently (the `try/except` hides it) |
| PII in logs | `session_handler.py:77` logs up to 500 chars of raw dispatch metadata (phone, business, notes) on a parse error; `session_handler.py:260` logs the phone number on every call. `logging.py` redacts keys (`api_key`, `secret`, `token`, ...) and 13-19 digit numbers, not phone numbers or names |
| Log context | the agent uses a `contextvar` (`set_call_id`); LiveKit's documented way is `ctx.log_context_fields`. Whether our JSON formatter even applies inside the job subprocess (LiveKit sets up its own logging there) is **not verified** |
| `type: ignore` | 6 in `src/`; the one in `persona_agent.py:222` hides `context: RunContext = None` (see C) |
| Static checks | 4 pyright errors in `src/` (2x `genai` import, `RunContext = None`, `convai_read.py:148` passes `str \| None` to `open_recording`); 45 in `tests/` (one pattern); 2 ruff errors in `scripts/` |

### B5. Async correctness and resources

| Finding | Where | Severity |
|---|---|---|
| The agent's end-of-call report can outlive its shutdown budget. `shutdown_process_timeout` is 30 s (`session_handler.py:869`). The shutdown callback runs the outcome classifier (a Gemini call), then `RemoteRecorder.finalize_call` posts up to 4 times with a 10 s timeout and 1+2+4 s backoff, so a down API takes about 47 s in the posting step alone. After the timeout the job is killed and **the call result is lost** (there is no durable queue on the agent side; the Postgres outbox starts only after the API accepts the post) | `persistence/remote.py:19-116`, `session_handler.py:666-736` | medium (data loss when the API is unreachable) |
| `dial` dispatches the agent **before** creating the SIP participant and does not clean up if the second call fails. The dispatched job then waits in an empty room (taking one of the Build plan's concurrent slots) until the room is closed (the empty-room timeout; exact length not verified) | `telephony/livekit_sip.py:44-80` | medium |
| `get_active_calls_count` loads every row of the window and takes `len()` instead of `COUNT(*)` | `persistence/recorder.py:270` | low |
| A boto3 client is created on every audio request | `storage.py:23` | low |
| Synchronous file I/O on the event loop at call start: `load_settings` (YAML) when persona or overrides differ, `PromptManager` (Jinja env, KB file) | `session_handler.py:234`, `prompts/` | low (small files) |
| Fire-and-forget tasks are tracked and cancelled correctly (`spawn`, `event_tasks`, `background_tasks`, guard and watchdog cancelled at shutdown) | | ok |
| `WebhookOutboxWorker` has no row locking (`SELECT` then update to `processing`); two worker processes would deliver the same row twice, and a worker killed mid-delivery leaves a `processing` row that is picked up again (acceptable, at-least-once). Safe today only because exactly one worker runs (`dev` mode) | `webhook/worker.py:140-166` | low (today) |

### B6. Configuration and secrets

| Finding | Detail |
|---|---|
| `Settings` is a `BaseSettings` **and** has a hand-written env map. I verified that the field names themselves are also read from the environment: `PERSONA=michael LANGUAGE=es` changed `Settings.model_validate({})`. Generic names (`LANGUAGE`, `PERSONA`, `ENVIRONMENT`, `GENDER`) can collide with OS variables (`LANGUAGE=en_US:en` is a standard Linux gettext variable and would fail the `Literal["en","es"]` check). Not seen in production, so uncertain whether a container sets it |
| `load_dotenv()` runs at import in `main.py` and `session_handler.py` (twice), in scripts, and `Settings` also reads `.env` | harmless, redundant |
| Defaults that fail "open" or silently | `telephony.provider` defaults to `mock` in `defaults.yaml`: a server missing `TELEPHONY_PROVIDER` returns `success: true` and dials nobody. `verify_xi_api_key` accepts **any non-empty key** when neither `POSIVEX_API_KEY` nor `ELEVENLABS_API_KEY` is set. Compose falls back to the Postgres password `posivex_secure_pass`. Webhook secret defaults to `dev_webhook_secret` (guarded by `startup_checks` for non-local URLs, good) |
| Secrets | none hard-coded in `src/`. `.env`, `.env.prod` are git-ignored (verified with `git check-ignore`). A pattern scan of `reference/`, `spikes/`, `docs/`, `prompts/` and `config/` for key-shaped strings (ElevenLabs, Google, AWS, private keys) found nothing; it is a pattern scan, not a line-by-line read |
| `config_overrides` | any caller with the API key can pass arbitrary settings per call (deep-merged into `Settings` in the agent, e.g. model names or recording settings). Acceptable for a trusted caller; mentioned so you decide |

### B7. Security observations (report only, no change proposed without approval)

These are exposures on the public API (`api.thebestdevsolutions.space`, whatever the nginx rules allow, which I cannot see):

1. `POST /api/v1/calls` **dials a phone number with no authentication** (`routes/calls.py`, no `verify_xi_api_key`). `GET /api/v1/calls/{id}` returns call details to anyone.
2. `/api/webhooks/elevenlabs/initiation` returns a past caller's business and contact name for any phone number, unauthenticated.
3. `/metrics`, `/docs`, `/openapi.json` are open; `/health/ready` returns `str(e)` of a database error to the caller (can include host and user).
4. `/internal/*` fails closed (503 if unset, constant-time compare), which is good.

---

## C. LiveKit-specific review

Compared against: docs.livekit.io pages on the server lifecycle, job lifecycle, builds and Dockerfiles, and testing (fetched today), the installed 1.8.10 source, and the vendored examples in `reference/examples` (`voice_agents/basic_agent.py`, `telephony/amd.py`, `drive_thru`, `frontdesk`). The docs pages for prewarm/`setup_fnc` and for unit-test code were not available in what I could fetch, so those two comparisons rest on the examples.

| Topic | This project | Verdict |
|---|---|---|
| Server and entrypoint | `AgentServer(...)`, `server.rtc_session(fn, agent_name=...)` called as a function, `setup_fnc = prewarm_process` | Same as the examples' `@server.rtc_session()` decorator; equivalent |
| Prewarm | `prewarm_process` loads Silero VAD into `proc.userdata`, imports heavy modules; the VAD is reused only when its config matches (`_prewarmed_vad`) | Idiomatic (examples use `userdata`) |
| Job context | calls `ctx.connect()` first, then reads metadata, then `AgentSession.start` | The docs say `AgentSession` connects by itself and that listeners should be registered before connecting. We register session events before waiting for the callee, and `wait_for_participant` covers a callee already in the room, so there is no observed bug; the early `connect()` is just not needed |
| Shutdown | `ctx.add_shutdown_callback(on_session_shutdown)`, `shutdown_process_timeout=30` | Docs: callbacks "should finish quickly", default 10 s. Ours does an LLM call and an HTTP report; see B5 for the budget problem |
| Ending the call | `RoomOptions(delete_room_on_close=True)`, `session.shutdown(drain=...)` | Matches the docs and the `amd.py` example's hang-up |
| Tools | `@function_tool` on methods; `context: RunContext = None  # type: ignore` | The examples declare `context: RunContext` with no default (the framework injects it). The `= None` exists so unit tests can call the tool without a session; it forces the `type: ignore` and a `None` branch in production code |
| `Agent.default.stt_node` / `llm_node` overrides | used in `persona_agent.py` | Idiomatic |
| Turn handling | `TurnHandlingOptions` dict with `endpointing`, `interruption`, `turn_detection="vad"` | Current API; no deprecation warnings were produced. The default is VAD-only turn detection; LiveKit's turn-detector models are available via `mode: v1/v1-mini` (a trade-off you already evaluated, so left as is) |
| Telephony | explicit dispatch + `CreateSIPParticipant`, `set_participant(callee)`, wait for `sip.callStatus` by polling every 0.5 s | Works. The examples use `participant_attributes_changed` events; polling is fine at this scale |
| Plugins | Deepgram, ElevenLabs, Google through factory functions with mock alternatives | Fine. The only non-idiomatic parts are the private-API uses in B4 |
| Recording | room-composite egress to S3, audio only | Matches LiveKit's egress API |
| Tokens | none are minted anywhere in `src/`; the server-side API key/secret is used only through `LiveKitAPI` | Nothing to review; the untracked `testing-frontend/` is outside this audit |
| Observability | own `timing_*` call events + stored usage; no `metrics_collected` handler, no OpenTelemetry (the `otel_trace.py` example shows the option) | Adequate for now |
| Testing | 194 unit tests with mocks; **none** run a real `AgentSession` (LiveKit recommends text-mode `session.run()` tests for tools and turns) and none touch `run_agent_session` | A gap; see G step 1 |
| Deprecated APIs | none flagged by LiveKit at import or in tests | The unit tests do not exercise the live session path, so this is "none seen", not "none exist" |
| Concurrency/scaling | no `load_threshold` / `num_idle_processes` set (Cloud defaults); `max_concurrent_calls: 50` in config while the Build plan allows 5 concurrent sessions | Informational |

Things the official examples do that this project does not (and might): `ctx.log_context_fields`; `session.on("metrics_collected")` or OpenTelemetry; text-mode `AgentSession` tests with scripted conversations (`reference/examples/frontdesk/test_agent.py`, `homepage/tests`).

---

## D. FastAPI review

| Area | Finding |
|---|---|
| Structure | Good: app factory (`create_app`), routers per concern, dependency functions in `deps.py`, lifespan manages the engine and the shared `LiveKitAPI` client |
| Lifespan | `main.py:42-66` replaces `router.lifespan_context` after the app is built to attach the outbox worker. It works (and the comment explains why), but it patches a private-ish attribute; passing an optional "background task" hook into `create_app` would be the clean form. Also, `dev` mode (API + worker in one process) is what production runs, which the name "dev" does not say |
| Import side effect | `src/api/app.py:98` runs `app = create_app()` at import, and `src/api/__init__.py` imports it, so `import src.api.<anything>` loads settings and YAML. Nothing uses `app` (the compose file runs `dev`, not `uvicorn src.api.app:app`) |
| Dependencies | `get_settings` and `get_telephony` have lazy fallbacks that rebuild state per request if `app.state` is empty; state is always set, so they are dead branches. Two functions named `get_sessionmaker` (in `db/base.py` and `api/deps.py`) mean different things |
| Models | Request bodies are Pydantic (`CallRequest`, with good normalisation of phone numbers); **no `response_model`** on any route, all return `dict[str, Any]`, so the contract lives only in code and `docs/BACKEND_CONTRACT.md` |
| Errors | One custom handler (validation to `400`, intentional for the lead-gen contract). No generic handler, no request-id or access logging middleware; `502` vs `500` inconsistency between the two dial routes (B2) |
| Auth | Inconsistent: convai routes use `xi-api-key`, `/internal` uses a secret header, `/api/v1/calls*`, `/api/webhooks/.../initiation`, `/metrics`, `/health*` use nothing (B7) |
| Health | `/health`, `/health/live`, `/health/ready` exist, but no Docker or compose healthcheck uses them. Readiness leaks the exception text |
| CORS | none configured; fine for a server-to-server API |
| Async | SQLAlchemy async throughout; blocking boto3 calls go through `to_thread` in `open_recording`; startup bucket check is blocking but runs once at boot |
| Migrations | Alembic exists with one revision; the compose and Dockerfile have no migration step, and the runtime image has no `uv`. **How the production schema is applied is not visible from the repo; needs your confirmation** |

---

## E. Docker review

| # | Finding | Detail | Risk to change |
|---|---|---|---|
| 1 | No-op `download-files` step | See A5 | low |
| 2 | No `HEALTHCHECK` | Neither the Dockerfile nor the compose services define one, although `/health/live` exists for the API. The agent has no HTTP port in this image, so a check there needs a different method | low |
| 3 | `.dockerignore` gaps | It excludes `.env.*`, `.git`, `reference/`, `spikes/`, `testing-frontend/`, `recent-calls/`, but not `livekit-recordings/`, `docs/`, `tests/`, `.ruff_cache/`, `docker-compose*.yml`, `scripts/*.ps1`. The LiveKit CLI also skips anything matched by `.gitignore`; `livekit-recordings/` was added to `.gitignore` only in commit `5336c77`. **If a deploy ran while the folder held the 56 MB of call exports (it did exist then, and I cannot see what the CLI uploaded), phone numbers and call audio may have been in the uploaded build context.** Worth checking in the LiveKit dashboard's build files, if it shows them. Never exclude `*.md` (the KB and classifier prompt are needed at runtime) | low |
| 4 | Floating base tags | `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` and `python:3.12-slim-bookworm`; LiveKit's guidance says pin versions. `uv.lock` is committed and `--frozen` is used (good) | low |
| 5 | Two agents with one name | Compose `posivex_agent` registers as `posivex-agent`, the same name as the Cloud agent. If both run, LiveKit can dispatch a call to either, and the copy on your Pakistan server would take calls that were meant to stay in the US. It is stopped now, but one `docker compose up -d` brings it back | low to remove, but **your decision** |
| 6 | Signals | Exec-form `ENTRYPOINT` (python is PID 1, so SIGTERM arrives). Compose has no `stop_grace_period`; the default 10 s is shorter than the agent's 30 s drain | low |
| 7 | Image reuse | `posivex_agent` and `posivex_api` each have a `build:` and no `image:`, so the same Dockerfile builds twice | low |
| 8 | Good practices already present | multi-stage, `-slim`, non-root `appuser` (UID 10001), lockfile before source, cache mounts, `PYTHONUNBUFFERED`, Postgres `16-alpine` pinned, API bound to `127.0.0.1`, Postgres healthcheck with `depends_on: service_healthy`, `restart: unless-stopped` | n/a |
| 9 | Secrets | env via `env_file`; none baked in. The default Postgres password fallback in compose is only reachable inside the compose network | low |

---

## F. Proposed target structure

The package layout is already close to what was suggested; the real problems are three oversized units, not the folder tree. I recommend an **incremental** layout rather than moving everything. Moving every package would change imports in most of the 22 test files, `alembic/env.py`, the Dockerfile entrypoint and `livekit.toml` for no behaviour gain.

```
src/
  main.py                      (kept, thin: argument parsing only; Docker ENTRYPOINT unchanged)
  core/                        (new, optional)   config models, loader, logging, startup_checks
    config/  models.py  loader.py  call_request.py     <- split of src/config.py
  api/                         (kept)
    app.py  deps.py  routes/...
    services/dispatch.py       (new)  the one shared dial flow used by calls.py and convai_emulation.py
  agent/
    session_handler.py         (kept as the public entrypoint; shrinks to ~150 lines of wiring)
    call_session/              (new)  pieces cut out of run_agent_session, each a small class holding its own state
      lifecycle.py   termination, end_call, closing-line watch, duration guard
      silence.py     (moved from agent/silence.py) away handling + no-speech watchdog
      opener.py      (moved) opener scheduling + overlap reply
      detection.py   (moved greeting_detector.py) greeting check wiring
      timing.py      mark(), CallClock (moved call_clock.py)
      finalize.py    on_session_shutdown: outcome, usage, persist
      telemetry.py   data-channel broadcasts
    persona_agent.py  interruption.py  (kept)
  persistence/ webhook/ telephony/ providers/ db/ analysis/ metrics/ prompts/ storage.py   (kept)
tests/                         (kept) + tests/support/mock_providers.py (moved from src/providers/mock.py, if you approve)
docs/archive/                  stale docs (if you approve)
```

Mapping of the files that move or split:

| Current | New | Why |
|---|---|---|
| `src/agent/session_handler.py::run_agent_session` | `agent/call_session/{lifecycle,silence,opener,detection,timing,finalize,telemetry}.py`, orchestrated from `session_handler.py` | one 610-line closure to testable units |
| `src/agent/{silence,opener,call_clock,greeting_detector}.py` | `agent/call_session/` | same feature area; **old import paths kept as re-exports** so tests keep passing |
| `src/config.py` | `src/core/config/{models,loader,call_request}.py` with `src/config.py` kept as `from src.core.config import *` | 470 lines, three jobs; shim keeps every import |
| `api/routes/calls.py` + `convai_emulation.py` | shared `api/services/dispatch.py` | removes the 90% duplicate; status codes stay per route |
| `src/providers/mock.py` | `tests/support/` (optional) | keep test doubles out of the production image; they are config-selectable, so this needs your OK |
| `src/api/app.py:98` `app = create_app()` | removed or lazy (optional) | removes the import side effect |

Not proposed: renaming `src/` or splitting out a separate package; both change the Docker/LiveKit entrypoint for no gain.

---

## G. Prioritised action plan

Risk = chance of changing behaviour. Value = benefit to you. **Core flow** means it touches the live call path and needs extra caution (marked CF).

### Decisions I need from you (each can change behaviour or contracts)

1. **Unauthenticated routes (B7):** do you want `/api/v1/calls*`, `/api/webhooks/elevenlabs/initiation`, `/metrics`, `/docs` protected or removed? That changes public behaviour, so I will not do it as part of the cleanup.
2. **Remove `calls.py` and the `initiation` route** (A1-10, A1-11)? Only you know whether `scripts/call.py` or lead-gen still use them.
3. **The compose `posivex_agent` service** (A2-9, E-5): remove it from `docker-compose.yml`, or keep it for local tests?
4. **DB tables `agent_config_versions` and `experiments`** (A1-5): drop with a migration, or keep the tables and remove only the unused models? (Recommendation: keep both for now.)
5. **Declare `google-genai`** in `pyproject.toml` (A4) and regenerate `uv.lock` (a dependency change, so I am asking first).
6. **Move `providers/mock.py` to tests** (A1-15)?
7. **`reference/`, `spikes/`, and the stale docs** (A3): delete, move to `docs/archive/`, or leave?
8. **Result-loss risk (B5-1) and the dispatch leak (B5-2):** these need small behaviour changes (a shorter post budget or a local retry file; cleanup on a failed dial). They are real bugs, not cleanup; do you want them handled in this task or separately?

### Ordered steps (lowest risk first)

| Step | Change | Risk | Value | CF? |
|---|---|---|---|---|
| 1 | **Safety net.** Record the baseline above; add characterization tests for `run_agent_session` using the existing mock providers and a fake `JobContext` (startup, opener timing, machine hang-up, silence end, shutdown report), plus API route smoke tests that pin each route's status code and body shape | low | **high** (nothing covers the core flow today) | tests only |
| 2 | **Docs and hygiene.** `.dockerignore` additions, `.env.example` completed (A2-8) and cleaned (A2-7), README fixed, two ruff errors fixed, stale docs handled per decision 7 | low | medium | no |
| 3 | **Dead code removal (high confidence only):** A1-1, 2, 3, 4, 6; config keys A2-1 to A2-5; the `download-files` step (A5). Medium/low items stay unless you approve them | low | medium | A2 items touch config loading, so CF-adjacent |
| 4 | **Quality fixes:** the 4 `src` pyright errors, name the magic numbers (B3), one thresholds constant, one `_utc`, one S3 client builder, one `thought_signatures` setup, silent `except` get a debug log. No behaviour change | low | medium | partly CF |
| 5 | **FastAPI/Docker tidy:** shared dial service (D, B2) keeping each route's status code and body; `HEALTHCHECK` for the API; `stop_grace_period`; pinned base tags; lazy `app`; readiness stops echoing exceptions (this one changes a response body, so decision 1) | medium | medium | no |
| 6 | **Modularize `run_agent_session`** per section F, one extracted unit per commit, tests from step 1 run after each | **medium-high** | high | **CF, extra caution** |
| 7 | **LiveKit alignment:** drop the early `ctx.connect()` only if tests and a test call confirm identical behaviour; `ctx.log_context_fields`; `context: RunContext` signature with a test helper instead of the `None` default (the tool schema must stay byte-identical, which I would check) | medium | low-medium | CF |
| 8 | **Tooling (asks first):** `pre-commit` config (already a dev dependency) and a pyright baseline so the 45 test-side `TypedDict` errors are fixed or scoped | low | low | no |

Each step is a separate commit (or batch), reviewable and revertable. After each: `ruff`, `pyright`, `pytest`, a check that the API app and the agent worker import and start, and a before/after comparison of the route table (`app.routes`), the env var names read, and the exported function signatures. **Docker build and container start** can only be fully verified on your machine or server (I can run `docker build` locally if Docker is available here; I have not tried).

### What I could not verify

- The Docker image build and container start; what the LiveKit CLI actually uploaded in past deploys (E-3).
- nginx rules on the server (which paths are public), how migrations are applied in production, whether the job subprocess uses our JSON log formatter, the room empty-timeout that bounds B5-2.
- Whether lead-gen calls any of the routes marked "low confidence" (A1-11, A1-12).
- Real-call behaviour. All behaviour claims are from reading code and tests.

---

**STOP. Nothing will be removed or moved until you approve the plan and answer the eight decisions in section G.**
