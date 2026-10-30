# Posivex LiveKit Voice Agent

Production LiveKit Voice Agent for Clover and NRS outbound B2B sales (migrated from ElevenLabs Conversational AI v5).

## Quickstart

### 1. Prerequisites
- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Docker Desktop (for Postgres & Redis)

### 2. Setup Environment
```bash
# Copy example env file
cp .env.example .env

# Install dependencies and sync lockfile
uv sync
```

### 3. Development Workflow
```bash
# Run linter
uv run ruff check

# Run type checker
uv run pyright

# Run tests
uv run pytest
```

