# Posivex LiveKit Voice Agent

Production LiveKit voice agent for Clover and NRS outbound B2B sales (migrated from ElevenLabs Conversational AI v5).

Two pieces run in different places:

- **The agent** (`python -m src.main agent start`) handles the live call. It is deployed to LiveKit Cloud with
  `scripts/deploy_agent.py` and reports each finished call to the API over HTTPS.
- **The API** (`python -m src.main dev`) runs on your server with Postgres. It emulates the ElevenLabs ConvAI API
  for the lead-gen app, dials through LiveKit, stores results, and delivers the signed post-call webhook.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how everything fits together and
[docs/RUNBOOK.md](docs/RUNBOOK.md) for operations.

## Quickstart

### 1. Prerequisites
- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Docker Desktop (for Postgres, and to run the API the way the server does)

### 2. Setup environment
```bash
# Copy example env file and fill it in
cp .env.example .env

# Install dependencies from the lockfile
uv sync
```

### 3. Development workflow
```bash
uv run ruff check          # lint
uv run pyright             # type check
uv run pytest              # unit tests (no live provider calls)
uv run pre-commit install  # once: runs ruff and pyright on every commit
```

### 4. Run
```bash
docker compose up -d --build posivex_api   # Postgres + API with the outbox worker, on 127.0.0.1:8000
uv run python -m src.main agent dev        # a local agent worker (the production agent runs on LiveKit Cloud)
uv run python scripts/deploy_agent.py      # deploy the agent to LiveKit Cloud using .env.prod
```
