"""Unified command-line entrypoint for Posivex Voice Calling service."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import uvicorn

from src.config import load_settings
from src.logging import configure_logging

logger = logging.getLogger("posivex.main")


def run_agent_command(args: argparse.Namespace) -> None:
    """Run the LiveKit Voice Agent worker."""
    from livekit.agents import cli

    from src.agent.session_handler import create_agent_server

    settings = load_settings(persona=args.persona)
    logger.info(
        "Starting LiveKit Voice Agent worker (%s) for persona: %s",
        args.action,
        settings.persona,
    )
    server = create_agent_server(settings=settings)

    # Bridge CLI subaction (start, dev, console) into LiveKit's Typer runner
    sys.argv = [sys.argv[0], args.action]
    cli.run_app(server)


def run_api_command(args: argparse.Namespace) -> None:
    """Run the FastAPI calling and emulation HTTP service."""
    from src.api.app import create_app

    settings = load_settings()
    logger.info("Starting Posivex API service on %s:%d", args.host, args.port)
    app = create_app(settings=settings)
    uvicorn.run(app, host=args.host, port=args.port, log_level=settings.log_level.lower())


def run_outbox_command(args: argparse.Namespace) -> None:
    """Run the transactional webhook outbox worker."""
    from src.db.base import get_engine, get_sessionmaker
    from src.webhook.worker import WebhookOutboxWorker

    settings = load_settings()
    logger.info(
        "Starting Transactional Webhook Outbox Worker (poll_interval=%.1fs)",
        args.interval,
    )
    engine = get_engine(settings.database_url)
    session_factory = get_sessionmaker(engine)
    worker = WebhookOutboxWorker(session_factory=session_factory, settings=settings)

    stop_event = asyncio.Event()
    try:
        asyncio.run(worker.run(poll_interval_secs=args.interval, stop_event=stop_event))
    except (KeyboardInterrupt, SystemExit):
        logger.info("Outbox worker interrupted by user.")
    finally:
        asyncio.run(engine.dispose())


def run_dev_command(args: argparse.Namespace) -> None:
    """Run API and outbox worker concurrently for local development."""
    from collections.abc import AsyncGenerator
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from src.api.app import create_app
    from src.db.base import get_engine, get_sessionmaker
    from src.webhook.worker import WebhookOutboxWorker

    settings = load_settings()
    logger.info("Starting Posivex in DEV mode (API + background outbox worker)")

    engine = get_engine(settings.database_url)
    session_factory = get_sessionmaker(engine)
    worker = WebhookOutboxWorker(session_factory=session_factory, settings=settings)

    base_app = create_app(settings=settings, session_factory=session_factory)

    # Wrap app lifespan to run outbox background task
    @asynccontextmanager
    async def dev_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        stop_event = asyncio.Event()
        worker_task = asyncio.create_task(
            worker.run(poll_interval_secs=args.interval, stop_event=stop_event)
        )
        logger.info("Dev background outbox worker task started.")
        yield
        logger.info("Stopping dev background outbox worker task...")
        stop_event.set()
        await worker_task
        await engine.dispose()

    base_app.router.lifespan_context = dev_lifespan
    uvicorn.run(base_app, host=args.host, port=args.port, log_level=settings.log_level.lower())


def build_parser() -> argparse.ArgumentParser:
    """Construct command-line argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="posivex",
        description="Posivex Outbound Voice Agent Service & Telephony Emulation",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. Agent subcommand
    agent_p = subparsers.add_parser("agent", help="Start the LiveKit Voice Agent worker")
    agent_p.add_argument(
        "action",
        nargs="?",
        choices=["start", "dev", "console"],
        default="start",
        help="Worker lifecycle action: start (production), dev (live reload), or console (interactive)",
    )
    agent_p.add_argument(
        "--persona",
        choices=["amelia", "michael"],
        default="amelia",
        help="Default persona profile to launch (default: amelia)",
    )

    # 2. API subcommand
    api_p = subparsers.add_parser("api", help="Start the FastAPI calling service")
    api_p.add_argument("--host", default="0.0.0.0", help="HTTP server bind host")
    api_p.add_argument("--port", type=int, default=8000, help="HTTP server bind port")

    # 3. Outbox subcommand
    outbox_p = subparsers.add_parser(
        "outbox", help="Start the transactional webhook outbox worker"
    )
    outbox_p.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Polling interval in seconds between batches",
    )

    # 4. Dev subcommand
    dev_p = subparsers.add_parser(
        "dev", help="Start API and outbox worker concurrently for local development"
    )
    dev_p.add_argument("--host", default="0.0.0.0", help="HTTP server bind host")
    dev_p.add_argument("--port", type=int, default=8000, help="HTTP server bind port")
    dev_p.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Outbox polling interval in seconds",
    )

    return parser


def main() -> None:
    """Main CLI entrypoint."""
    configure_logging()
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "agent":
        run_agent_command(args)
    elif args.command == "api":
        run_api_command(args)
    elif args.command == "outbox":
        run_outbox_command(args)
    elif args.command == "dev":
        run_dev_command(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
