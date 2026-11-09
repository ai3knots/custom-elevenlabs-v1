"""Deploy the voice agent worker to LiveKit Cloud using the secrets in `.env.prod`.

    uv run python scripts/deploy_agent.py --dry-run     # validate, show what would be sent (names only)
    uv run python scripts/deploy_agent.py               # create the agent the first time, deploy after that
    uv run python scripts/deploy_agent.py --region us-east

Only the variables the agent needs are sent. LIVEKIT_URL/KEY/SECRET are injected by LiveKit Cloud itself, and
the database and webhook settings belong to the API on your server, so none of those leave this machine.
The secrets go to LiveKit in a temporary file that is deleted afterwards; the image never contains them
(`.dockerignore` excludes `.env.*`).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent

# What the agent worker reads at run time.
REQUIRED = ("GEMINI_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY", "RESULTS_CALLBACK_URL", "RESULTS_CALLBACK_SECRET")
OPTIONAL = (
    "TURN_DETECTOR_MODE",
    "LLM_DEFAULT_MODEL",
    "LLM_FALLBACK_MODEL",
    "LOG_LEVEL",
    "RECORDING_ENABLED",
    "RECORDING_BUCKET",
    "RECORDING_REGION",
    "RECORDING_ENDPOINT",
    "RECORDING_ACCESS_KEY",
    "RECORDING_SECRET",
    "RECORDING_PREFIX",
    "RECORDING_FORCE_PATH_STYLE",
    "LIVEKIT_AGENT_NAME",
)
# Deliberately never sent, with the reason (shown in the summary so nothing is silently dropped).
NOT_SENT = {
    "LIVEKIT_URL": "injected by LiveKit Cloud",
    "LIVEKIT_API_KEY": "injected by LiveKit Cloud",
    "LIVEKIT_API_SECRET": "injected by LiveKit Cloud",
    "DATABASE_URL": "the agent reports results through the API instead of the database",
    "POSTGRES_USER": "database lives on your server",
    "POSTGRES_PASSWORD": "database lives on your server",
    "POSTGRES_DB": "database lives on your server",
    "REDIS_URL": "not used",
    "ELEVENLABS_WEBHOOK_SECRET": "only the API signs webhooks",
    "LEAD_GEN_WEBHOOK_URL": "only the API sends webhooks",
    "POSIVEX_API_KEY": "only the API checks it",
    "SIP_OUTBOUND_TRUNK_ID": "the API dials, not the agent",
    "SIP_PHONE_NUMBER_ID": "the API dials, not the agent",
    "TELEPHONY_PROVIDER": "the API dials, not the agent",
    "VOICE_PHONE_NUMBER_ID": "only the API lists numbers",
    "VOICE_PHONE_NUMBER": "only the API lists numbers",
    "PUBLIC_LIVEKIT_URL": "old self-hosted LiveKit",
    "AGENT_ID": "old ElevenLabs agent",
}
DEFAULT_AGENT_NAME = "posivex-agent"


class DeployError(Exception):
    pass


def load_secrets(env_file: Path) -> dict[str, str]:
    """The filtered KEY=VALUE set for the agent, validated. Raises DeployError with what to fix."""
    if not env_file.exists():
        raise DeployError(f"{env_file} not found.")
    values = {k: (v or "").strip() for k, v in dotenv_values(env_file).items()}  # later duplicates win, as in Docker

    missing = [k for k in REQUIRED if not values.get(k)]
    if missing:
        hint = ""
        if any(k.startswith("RESULTS_CALLBACK") for k in missing):
            hint = (
                "\nRESULTS_CALLBACK_URL is the public https address of the voice API (the one lead-gen reaches at "
                "127.0.0.1:8000), and RESULTS_CALLBACK_SECRET is a long random value that must be the same in the "
                "server's .env."
            )
        raise DeployError(f"{env_file.name} is missing: {', '.join(missing)}.{hint}")

    secrets = {k: values[k] for k in (*REQUIRED, *OPTIONAL) if values.get(k)}
    secrets.setdefault("LIVEKIT_AGENT_NAME", DEFAULT_AGENT_NAME)

    url = urlparse(secrets["RESULTS_CALLBACK_URL"])
    if url.scheme != "https" or (url.hostname or "") in {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"}:
        raise DeployError(
            f"RESULTS_CALLBACK_URL must be a public https address (got {secrets['RESULTS_CALLBACK_URL']!r}); an agent on "
            "LiveKit Cloud cannot reach localhost."
        )
    if secrets.get("RECORDING_ENABLED", "").lower() == "true":
        absent = [k for k in ("RECORDING_BUCKET", "RECORDING_ACCESS_KEY", "RECORDING_SECRET") if not secrets.get(k)]
        if absent:
            raise DeployError(f"RECORDING_ENABLED is true but {', '.join(absent)} is missing.")
    for key, value in secrets.items():
        if any(c in value for c in "\r\n") or value != value.strip("\"'"):
            raise DeployError(f"{key} has a newline or stray quotes; fix it in {env_file.name}.")
    return secrets


def livekit_subdomain(env_file: Path) -> str:
    """The project the env file points at, so a deploy cannot land in a different LiveKit project."""
    host = urlparse(dotenv_values(env_file).get("LIVEKIT_URL") or "").hostname or ""
    if not host.endswith(".livekit.cloud"):
        raise DeployError(f"LIVEKIT_URL in {env_file.name} must be a wss://<project>.livekit.cloud address.")
    return host.removesuffix(".livekit.cloud")


def preflight_callback(url: str, secret: str, timeout: float = 15.0) -> str:
    """Check the API's results endpoint is reachable and accepts the secret, without storing anything.

    A deliberately malformed body is sent: a wrong secret is rejected first (401), and a right one then fails
    validation (400), so no call row is ever created. Returns a short status line; raises on a bad answer.
    """
    endpoint = f"{url.rstrip('/')}/internal/calls/preflight-check/result"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps({"transcript": "not-a-list"}).encode(),
        headers={"Content-Type": "application/json", "X-Posivex-Callback-Secret": secret},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=timeout)  # noqa: S310
    except urllib.error.HTTPError as e:
        if e.code in (400, 422):
            return "reachable, secret accepted"
        explain = {
            401: "the server rejected RESULTS_CALLBACK_SECRET; it must match the voice API's .env exactly",
            503: "the voice API has no RESULTS_CALLBACK_SECRET set; add it to the server's .env and recreate the API",
            404: "the URL answers but has no /internal/ endpoint; is it the voice API, and is it up to date (git pull)?",
        }.get(e.code, f"unexpected HTTP {e.code}")
        raise DeployError(f"Results endpoint check failed: {explain}.") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise DeployError(f"Cannot reach {endpoint}: {e}. Is the public URL set up and the API running?") from e
    raise DeployError("Results endpoint accepted a malformed body, so it is not the expected API.")


def run_lk(args: list[str]) -> int:
    lk = shutil.which("lk")
    if not lk:
        raise DeployError("The LiveKit CLI (lk) is not installed. https://docs.livekit.io/home/cli/")
    return subprocess.call([lk, *args], cwd=ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=".env.prod", help="secrets to read (default: .env.prod)")
    parser.add_argument("--region", default="us-east", help="LiveKit Cloud region for the agent (default: us-east)")
    parser.add_argument("--dry-run", action="store_true", help="validate and show what would be sent, deploy nothing")
    parser.add_argument("--skip-preflight", action="store_true", help="do not call the results endpoint first")
    args = parser.parse_args()

    env_file = (ROOT / args.env_file) if not os.path.isabs(args.env_file) else Path(args.env_file)
    try:
        secrets = load_secrets(env_file)
        subdomain = livekit_subdomain(env_file)

        print(f"Env file     : {env_file.name}")
        print(f"LiveKit      : {subdomain}.livekit.cloud   region: {args.region}")
        print(f"Agent name   : {secrets['LIVEKIT_AGENT_NAME']}")
        print(f"Results URL  : {secrets['RESULTS_CALLBACK_URL']}")
        print(f"Recording    : {'on, bucket ' + secrets.get('RECORDING_BUCKET', '') if secrets.get('RECORDING_ENABLED', '').lower() == 'true' else 'off'}")
        print(f"Secrets sent : {', '.join(sorted(secrets))}")
        present = dotenv_values(env_file)
        skipped = [f"{k} ({why})" for k, why in NOT_SENT.items() if k in present]
        if skipped:
            print("Not sent     : " + "; ".join(skipped))

        if not args.skip_preflight:
            print(f"Preflight    : {preflight_callback(secrets['RESULTS_CALLBACK_URL'], secrets['RESULTS_CALLBACK_SECRET'])}")
        if args.dry_run:
            print("\nDry run: nothing was deployed.")
            return 0

        first_time = not (ROOT / "livekit.toml").exists()
        fd, tmp_name = tempfile.mkstemp(prefix="agent-secrets-", suffix=".env")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("".join(f"{k}={v}\n" for k, v in secrets.items()))
            os.chmod(tmp_name, 0o600)
            command = "create" if first_time else "deploy"
            print(f"\nRunning: lk agent {command} (this builds the image on LiveKit Cloud; allow a few minutes)\n")
            code = run_lk(
                [
                    "agent",
                    command,
                    "--subdomain",
                    subdomain,
                    "--region",
                    args.region,
                    "--secrets-file",
                    tmp_name,
                    "--yes",
                ]
            )
        finally:
            Path(tmp_name).unlink(missing_ok=True)
        if code != 0:
            print(f"\nlk agent {command} failed (exit {code}). Nothing else was changed.", file=sys.stderr)
            return code
        print("\nDeployed. Check it with:  lk agent status   |   lk agent logs")
        return 0
    except DeployError as e:
        print(f"\nStopped: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
