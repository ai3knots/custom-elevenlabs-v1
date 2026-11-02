"""Interactive CLI script to dispatch outbound calls to any phone number.

Usage:
    uv run python scripts/call.py --to +14155552671 --name "Dr. Smith" --business "Apex Dental"
    python scripts/call.py -t +14155552671 -n "John" -b "Acme Services" --wait
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

DEFAULT_API_URL = os.getenv("API_BASE_URL", "https://api.thebestdevsolutions.space")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dispatch an outbound voice call via Posivex Voice Calling API."
    )
    parser.add_argument(
        "--to",
        "-t",
        required=True,
        help="Destination phone number in E.164 format (e.g. +14155552671)",
    )
    parser.add_argument(
        "--name",
        "-n",
        default="Valued Client",
        help="Callee contact name (default: Valued Client)",
    )
    parser.add_argument(
        "--business",
        "-b",
        default="Apex Solutions",
        help="Business or practice name for persona context (default: Apex Solutions)",
    )
    parser.add_argument(
        "--persona",
        "-p",
        choices=["amelia", "michael"],
        default="amelia",
        help="Voice agent persona (amelia or michael, default: amelia)",
    )
    parser.add_argument(
        "--language",
        "-l",
        choices=["en", "es"],
        default="en",
        help="Call language (en or es, default: en)",
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help=f"Base API URL (default: {DEFAULT_API_URL})",
    )
    parser.add_argument(
        "--wait",
        "-w",
        action="store_true",
        help="Poll and stream live call status until the call ends",
    )
    return parser.parse_args()


def dispatch_call(args: argparse.Namespace) -> dict[str, Any]:
    url = f"{args.api_url.rstrip('/')}/api/v1/calls"
    payload = {
        "phone": args.to,
        "persona": args.persona,
        "language": args.language,
        "dynamic_variables": {
            "business_name": args.business,
            "contact_name": args.name,
            "business_context": f"Consultation inquiry for {args.business}",
        },
    }

    print(f"\n📞 Dispatching call to: {args.to}")
    print(f"👤 Contact Name:       {args.name}")
    print(f"🏢 Business Name:      {args.business}")
    print(f"🤖 Persona:            {args.persona.capitalize()} ({args.language.upper()})")
    print(f"🌐 API Endpoint:       {url}\n")

    try:
        response = httpx.post(url, json=payload, timeout=15.0)
    except httpx.ConnectError as e:
        print(f"❌ Connection failed: Unable to reach {args.api_url}: {e}")
        sys.exit(1)

    if response.status_code != 200:
        print(f"❌ Dispatch failed [{response.status_code}]: {response.text}")
        sys.exit(1)

    data = response.json()
    print("✅ Call Dispatched Successfully!")
    print(f"   • Call ID:   {data.get('call_id')}")
    print(f"   • Room Name: {data.get('room_name')}")
    print(f"   • Status:    {data.get('status')}\n")
    return data


def poll_call_status(api_url: str, call_id: str) -> None:
    url = f"{api_url.rstrip('/')}/api/v1/calls/{call_id}"
    print(f"⏳ Monitoring call [{call_id}]... (Ctrl+C to stop monitoring)\n")

    terminal_statuses = {"completed", "failed", "busy", "no_answer"}
    last_status = None

    try:
        while True:
            try:
                res = httpx.get(url, timeout=10.0)
                if res.status_code == 200:
                    data = res.json()
                    status = data.get("status", "unknown")
                    duration = data.get("duration_secs", 0)
                    outcome = data.get("call_outcome", "pending")
                    cost = data.get("total_cost", 0.0)

                    if status != last_status:
                        print(f"   • Status changed: [{status.upper()}]")
                        last_status = status

                    if status in terminal_statuses:
                        print(f"\n🏁 Call ended with status: {status.upper()}")
                        print(f"   • Duration:  {duration:.1f}s")
                        print(f"   • Outcome:   {outcome}")
                        print(f"   • Est. Cost: ${cost:.4f}")
                        break
            except Exception as e:
                print(f"   ⚠️  Warning checking status: {e}")

            time.sleep(2)
    except KeyboardInterrupt:
        print("\n👋 Monitoring stopped by user. The call remains active on the server.")


def main() -> None:
    args = parse_args()
    result = dispatch_call(args)
    if args.wait and result.get("call_id"):
        poll_call_status(args.api_url, result["call_id"])


if __name__ == "__main__":
    main()

