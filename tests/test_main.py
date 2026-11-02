"""Unit tests for unified CLI entrypoint parser and dispatch routing."""

from __future__ import annotations

import pytest

from src.main import build_parser


def test_cli_parser_agent_subcommand() -> None:
    """Test argument parsing for 'agent' subcommand."""
    parser = build_parser()
    args = parser.parse_args(["agent", "--persona", "michael"])
    assert args.command == "agent"
    assert args.persona == "michael"


def test_cli_parser_api_subcommand() -> None:
    """Test argument parsing for 'api' subcommand."""
    parser = build_parser()
    args = parser.parse_args(["api", "--host", "127.0.0.1", "--port", "9000"])
    assert args.command == "api"
    assert args.host == "127.0.0.1"
    assert args.port == 9000


def test_cli_parser_outbox_subcommand() -> None:
    """Test argument parsing for 'outbox' subcommand."""
    parser = build_parser()
    args = parser.parse_args(["outbox", "--interval", "10.5"])
    assert args.command == "outbox"
    assert args.interval == 10.5


def test_cli_parser_dev_subcommand() -> None:
    """Test argument parsing for 'dev' subcommand."""
    parser = build_parser()
    args = parser.parse_args(["dev", "--port", "8080", "--interval", "3.0"])
    assert args.command == "dev"
    assert args.port == 8080
    assert args.interval == 3.0


def test_cli_parser_missing_command() -> None:
    """Test that omitting command raises SystemExit."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
