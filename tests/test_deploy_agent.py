"""The LiveKit Cloud deploy script: what it sends, what it refuses, and how it checks the results endpoint."""

from __future__ import annotations

import importlib.util
import io
import urllib.error
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "deploy_agent.py"


@pytest.fixture(scope="module")
def deploy() -> ModuleType:
    spec = importlib.util.spec_from_file_location("deploy_agent", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENV = """\
LIVEKIT_URL=wss://project-demo-abc123.livekit.cloud
LIVEKIT_API_KEY=lk-key
LIVEKIT_API_SECRET=lk-secret
GEMINI_API_KEY="gem-key"
DEEPGRAM_API_KEY=dg-key
ELEVENLABS_API_KEY=el-key
DATABASE_URL=postgresql+asyncpg://u:p@postgres:5432/db
POSTGRES_PASSWORD=pw
# ELEVENLABS_WEBHOOK_SECRET=old
ELEVENLABS_WEBHOOK_SECRET=new-webhook
POSIVEX_API_KEY=api-key
TURN_DETECTOR_MODE=v1
RESULTS_CALLBACK_URL=https://voice.example.com
RESULTS_CALLBACK_SECRET=cb-secret
RECORDING_ENABLED=true
RECORDING_BUCKET=calls
RECORDING_REGION=us-east-1
RECORDING_ACCESS_KEY=ak
RECORDING_SECRET=sk
"""


def env_file(tmp_path: Path, text: str = ENV) -> Path:
    path = tmp_path / ".env.prod"
    path.write_text(text, encoding="utf-8")
    return path


def test_only_what_the_agent_needs_is_sent(deploy: ModuleType, tmp_path: Path) -> None:
    secrets = deploy.load_secrets(env_file(tmp_path))
    assert secrets["GEMINI_API_KEY"] == "gem-key"  # quotes stripped
    assert secrets["RESULTS_CALLBACK_URL"] == "https://voice.example.com"
    assert secrets["LIVEKIT_AGENT_NAME"] == "posivex-agent"  # defaulted so it matches the API's dispatch
    for never in (
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
        "ELEVENLABS_WEBHOOK_SECRET",
        "POSIVEX_API_KEY",
    ):
        assert never not in secrets


def test_missing_results_callback_explains_what_it_is(deploy: ModuleType, tmp_path: Path) -> None:
    text = "\n".join(line for line in ENV.splitlines() if not line.startswith("RESULTS_CALLBACK"))
    with pytest.raises(deploy.DeployError, match="RESULTS_CALLBACK_URL.*public https"):
        deploy.load_secrets(env_file(tmp_path, text))


@pytest.mark.parametrize("url", ["http://voice.example.com", "https://localhost:8000", "https://127.0.0.1"])
def test_results_url_must_be_public_https(deploy: ModuleType, tmp_path: Path, url: str) -> None:
    text = ENV.replace("https://voice.example.com", url)
    with pytest.raises(deploy.DeployError, match="public https"):
        deploy.load_secrets(env_file(tmp_path, text))


def test_recording_on_needs_its_bucket_settings(deploy: ModuleType, tmp_path: Path) -> None:
    text = ENV.replace("RECORDING_SECRET=sk\n", "")
    with pytest.raises(deploy.DeployError, match="RECORDING_SECRET"):
        deploy.load_secrets(env_file(tmp_path, text))


def test_project_comes_from_livekit_url(deploy: ModuleType, tmp_path: Path) -> None:
    assert deploy.livekit_subdomain(env_file(tmp_path)) == "project-demo-abc123"
    with pytest.raises(deploy.DeployError, match="livekit.cloud"):
        deploy.livekit_subdomain(env_file(tmp_path, "LIVEKIT_URL=wss://agent.example.com\n"))


def fake_urlopen(monkeypatch: pytest.MonkeyPatch, code: int | Exception) -> list[Any]:
    sent: list[Any] = []

    def opener(request: Any, timeout: float = 0) -> Any:
        sent.append(request)
        if isinstance(code, Exception):
            raise code
        raise urllib.error.HTTPError(request.full_url, code, "x", {}, io.BytesIO(b""))  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", opener)
    return sent


def test_preflight_accepts_a_400_because_it_means_the_secret_was_accepted(
    deploy: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = fake_urlopen(monkeypatch, 400)
    assert "secret accepted" in deploy.preflight_callback("https://voice.example.com/", "cb-secret")
    request = sent[0]
    assert request.full_url == "https://voice.example.com/internal/calls/preflight-check/result"
    assert request.get_header("X-posivex-callback-secret") == "cb-secret"


@pytest.mark.parametrize(
    ("code", "words"), [(401, "must match"), (503, "no RESULTS_CALLBACK_SECRET"), (404, "no /internal/"), (500, "HTTP 500")]
)
def test_preflight_names_the_fix(deploy: ModuleType, monkeypatch: pytest.MonkeyPatch, code: int, words: str) -> None:
    fake_urlopen(monkeypatch, code)
    with pytest.raises(deploy.DeployError, match=words):
        deploy.preflight_callback("https://voice.example.com", "s")


def test_preflight_reports_an_unreachable_url(deploy: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_urlopen(monkeypatch, urllib.error.URLError("name not resolved"))
    with pytest.raises(deploy.DeployError, match="Cannot reach"):
        deploy.preflight_callback("https://voice.example.com", "s")


# ---- the deploy is pinned to the project the env file names -----------------------------------------------------
SAVED = [
    {"name": "old", "url": "wss://project-old-xyz.livekit.cloud", "key": "old-key"},
    {"name": "demo", "url": "wss://project-demo-abc123.livekit.cloud", "key": "lk-key"},
]


def test_the_project_is_found_by_url_and_key(deploy: ModuleType, tmp_path: Path) -> None:
    assert deploy.resolve_cli_project(env_file(tmp_path), SAVED) == "demo"


def test_a_project_the_cli_does_not_know_is_refused_with_the_command_to_add_it(
    deploy: ModuleType, tmp_path: Path
) -> None:
    """lk would fall back to its default project (the old account) instead."""
    with pytest.raises(deploy.DeployError, match=r"lk project add NAME --url wss://project-demo-abc123\.livekit\.cloud"):
        deploy.resolve_cli_project(env_file(tmp_path), SAVED[:1])


def test_a_project_saved_under_another_key_is_refused(deploy: ModuleType, tmp_path: Path) -> None:
    other = [{"name": "demo", "url": "wss://project-demo-abc123.livekit.cloud", "key": "different"}]
    with pytest.raises(deploy.DeployError, match="different API key"):
        deploy.resolve_cli_project(env_file(tmp_path), other)


def test_livekit_toml_of_another_project_must_be_moved_aside(deploy: ModuleType, tmp_path: Path) -> None:
    toml = tmp_path / "livekit.toml"
    toml.write_text('[project]\n  subdomain = "project-old-xyz"\n\n[agent]\n  id = "CA_old"\n', encoding="utf-8")
    with pytest.raises(deploy.DeployError, match=r"mv livekit\.toml livekit\.project-old-xyz\.toml"):
        deploy.check_livekit_toml("project-demo-abc123", toml)
    deploy.check_livekit_toml("project-old-xyz", toml)  # the same project: fine
    deploy.check_livekit_toml("project-demo-abc123", tmp_path / "missing.toml")  # no file: fine


def test_the_deploy_names_the_project_instead_of_a_subdomain(
    deploy: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: list[list[str]] = []
    monkeypatch.setattr(deploy, "cli_projects", lambda: SAVED)
    monkeypatch.setattr(deploy, "run_lk", lambda args: sent.append(args) or 0)
    monkeypatch.setattr(deploy, "ROOT", tmp_path)  # no livekit.toml here: the first deploy creates the agent
    monkeypatch.setattr("sys.argv", ["deploy_agent.py", "--env-file", str(env_file(tmp_path)), "--skip-preflight"])
    assert deploy.main() == 0
    args = sent[0]
    assert args[:2] == ["agent", "create"]
    assert args[args.index("--project") + 1] == "demo" and "--subdomain" not in args
    assert "lk project: demo" in capsys.readouterr().out
