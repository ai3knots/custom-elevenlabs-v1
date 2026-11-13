"""Unified command-line entrypoint for Posivex Voice Calling service."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING

import uvicorn
from dotenv import load_dotenv

from src.config import Settings, load_settings
from src.logging import configure_logging
from src.startup_checks import (
    StartupCheckError,
    check_phone_numbers,
    check_recording_storage,
    check_webhook_config,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

    from src.webhook.worker import WebhookOutboxWorker

load_dotenv()
logger = logging.getLogger("posivex.main")


def run_startup_checks(settings: Settings, *, webhook: bool) -> None:
    """Report bad settings at boot. A wrong webhook secret stops the service; a bad bucket only logs."""
    try:
        if webhook:
            check_webhook_config(settings)
    except StartupCheckError as e:
        logger.critical("Refusing to start: %s", e)
        sys.exit(1)
    check_recording_storage(settings)
    check_phone_numbers(settings)


def build_dev_lifespan(
    base_app: FastAPI, worker: WebhookOutboxWorker, engine: AsyncEngine, interval: float
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """App lifespan that also runs the outbox worker.

    It must wrap the app's own lifespan rather than replace it: that one creates the shared telephony
    provider. Without it every dial built its own LiveKit client and never closed it.
    """
    original = base_app.router.lifespan_context

    @asynccontextmanager
    async def dev_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        async with original(app):
            stop_event = asyncio.Event()
            worker_task = asyncio.create_task(worker.run(poll_interval_secs=interval, stop_event=stop_event))
            logger.info("Dev background outbox worker task started.")
            try:
                yield
            finally:
                logger.info("Stopping dev background outbox worker task...")
                stop_event.set()
                await worker_task
                await engine.dispose()

    return dev_lifespan


def run_agent_command(args: argparse.Namespace) -> None:
    """Run the LiveKit Voice Agent worker."""
    from livekit.agents import cli

    from src.agent.session_handler import create_agent_server

    settings = load_settings(persona=args.persona)
    run_startup_checks(settings, webhook=False)
    if settings.livekit_url:
        os.environ["LIVEKIT_URL"] = settings.livekit_url
    if settings.livekit_api_key:
        os.environ["LIVEKIT_API_KEY"] = settings.livekit_api_key
    if settings.livekit_api_secret:
        os.environ["LIVEKIT_API_SECRET"] = settings.livekit_api_secret

    logger.info(
        "Starting LiveKit Voice Agent worker (%s) for persona: %s (url=%s)",
        args.action,
        settings.persona,
        settings.livekit_url or "[NOT SET]",
    )
    server = create_agent_server(settings=settings)

    # Bridge CLI subaction (start, dev, console) into LiveKit's Typer runner
    sys.argv = [sys.argv[0], args.action]
    cli.run_app(server)


def run_api_command(args: argparse.Namespace) -> None:
    """Run the FastAPI calling and emulation HTTP service."""
    from src.api.app import create_app

    settings = load_settings()
    run_startup_checks(settings, webhook=True)
    logger.info("Starting Posivex API service on %s:%d", args.host, args.port)
    app = create_app(settings=settings)
    uvicorn.run(app, host=args.host, port=args.port, log_level=settings.log_level.lower())


def run_outbox_command(args: argparse.Namespace) -> None:
    """Run the transactional webhook outbox worker."""
    from src.db.base import get_engine, get_sessionmaker
    from src.webhook.worker import WebhookOutboxWorker

    settings = load_settings()
    run_startup_checks(settings, webhook=True)
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
    from src.api.app import create_app
    from src.db.base import get_engine, get_sessionmaker
    from src.webhook.worker import WebhookOutboxWorker

    settings = load_settings()
    run_startup_checks(settings, webhook=True)
    logger.info("Starting Posivex in DEV mode (API + background outbox worker)")

    engine = get_engine(settings.database_url)
    session_factory = get_sessionmaker(engine)
    worker = WebhookOutboxWorker(session_factory=session_factory, settings=settings)

    base_app = create_app(settings=settings, session_factory=session_factory)
    base_app.router.lifespan_context = build_dev_lifespan(base_app, worker, engine, args.interval)
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
