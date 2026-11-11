"""Utility script to create and configure a Telnyx SIP Outbound Trunk in LiveKit.

Usage:
    uv run python scripts/setup_telnyx_trunk.py \
        --phone "+1XXXXXXXXXX" \
        --sip-user "your_telnyx_sip_username" \
        --sip-pass "your_telnyx_sip_password"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from livekit import api

load_dotenv()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register a Telnyx SIP Outbound Trunk in LiveKit Server."
    )
    parser.add_argument(
        "--phone",
        "-p",
        required=True,
        help="Your Telnyx DID Phone Number in E.164 format (e.g. +14155552671)",
    )
    parser.add_argument(
        "--sip-user",
        "-u",
        required=True,
        help="Telnyx SIP Connection Username (from Telnyx Portal -> SIP Connections)",
    )
    parser.add_argument(
        "--sip-pass",
        "-w",
        required=True,
        help="Telnyx SIP Connection Password",
    )
    parser.add_argument(
        "--sip-host",
        default="sip.telnyx.com",
        help="Telnyx SIP Host (default: sip.telnyx.com)",
    )
    parser.add_argument(
        "--name",
        default="Telnyx-Outbound",
        help="Human-readable trunk name (default: Telnyx-Outbound)",
    )
    parser.add_argument(
        "--destination-country",
        default="us",
        help=(
            "ISO country code of the people being called (default: us). LiveKit then places the phone leg "
            "in a server in that country; without it the call starts in the region of whoever makes the "
            "dial request, which is far from the callee when the API runs elsewhere."
        ),
    )
    parser.add_argument(
        "--username-header",
        action="store_true",
        help=(
            "Also send the SIP username in an X-Telnyx-Username header on the first INVITE. Off by default: the "
            "trunk that is live and working (Telnyx-NoHeader) sends no header."
        ),
    )
    return parser.parse_args()


def build_trunk(args: argparse.Namespace) -> api.SIPOutboundTrunkInfo:
    """The trunk to create: the same shape as the live one (address, number, US destination, credentials)."""
    trunk = api.SIPOutboundTrunkInfo(
        name=args.name,
        address=args.sip_host,
        numbers=[args.phone],
        destination_country=args.destination_country,
        auth_username=args.sip_user,
        auth_password=args.sip_pass,
    )
    if args.username_header:
        # Telnyx docs: send the username in the first INVITE so Telnyx always answers with a 407 challenge instead
        # of matching a connection by source IP.
        trunk.headers["X-Telnyx-Username"] = args.sip_user
        trunk.headers_to_attributes["X-Telnyx-Username"] = args.sip_user
    return trunk


async def register_trunk(args: argparse.Namespace) -> None:
    livekit_url = os.getenv("LIVEKIT_URL", "")
    api_key = os.getenv("LIVEKIT_API_KEY", "")
    api_secret = os.getenv("LIVEKIT_API_SECRET", "")

    if not livekit_url or not api_key or not api_secret:
        print(
            "❌ Error: LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set "
            "in your .env (use your LiveKit Cloud project values)"
        )
        sys.exit(1)

    print(f"\n📡 Connecting to LiveKit at: {livekit_url}")
    print(f"📞 Registering Telnyx Outbound Trunk: {args.name}")
    print(f"   • Phone Number: {args.phone}")
    print(f"   • Gateway Host: {args.sip_host}")
    print(f"   • SIP User:     {args.sip_user}\n")

    lkapi = api.LiveKitAPI(url=livekit_url, api_key=api_key, api_secret=api_secret)

    trunk_info = build_trunk(args)

    request = api.CreateSIPOutboundTrunkRequest(trunk=trunk_info)

    try:
        res = await lkapi.sip.create_sip_outbound_trunk(request)
        trunk_id = res.sip_trunk_id
        print("🎉 Telnyx SIP Outbound Trunk Created Successfully!")
        print("=" * 60)
        print(f"   SIP_OUTBOUND_TRUNK_ID = {trunk_id}")
        print("=" * 60)
        print("\nNext step: Add this Trunk ID to your server's .env file:")
        print("TELEPHONY_PROVIDER=livekit_sip")
        print(f"SIP_OUTBOUND_TRUNK_ID={trunk_id}")
        print()
    except Exception as e:
        print(f"❌ Failed to create SIP Outbound Trunk: {e}")
        sys.exit(1)
    finally:
        await lkapi.aclose()


def main() -> None:
    # The banner has emoji; a Windows console (cp1252) would crash on them before the first API call.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    asyncio.run(register_trunk(args))


if __name__ == "__main__":
    main()

