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
    return parser.parse_args()


async def register_trunk(args: argparse.Namespace) -> None:
    livekit_url = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
    api_key = os.getenv("LIVEKIT_API_KEY", "")
    api_secret = os.getenv("LIVEKIT_API_SECRET", "")

    if not api_key or not api_secret:
        print("❌ Error: LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set in your .env")
        sys.exit(1)

    print(f"\n📡 Connecting to LiveKit Server at: {livekit_url}")
    print(f"📞 Registering Telnyx Outbound Trunk: {args.name}")
    print(f"   • Phone Number: {args.phone}")
    print(f"   • Gateway Host: {args.sip_host}")
    print(f"   • SIP User:     {args.sip_user}\n")

    lkapi = api.LiveKitAPI(url=livekit_url, api_key=api_key, api_secret=api_secret)

    trunk_info = api.SIPOutboundTrunkInfo(
        name=args.name,
        address=args.sip_host,
        numbers=[args.phone],
        auth_username=args.sip_user,
        auth_password=args.sip_pass,
    )

    request = api.CreateSIPOutboundTrunkRequest(trunk=trunk_info)

    try:
        res = await lkapi.sip.create_sip_outbound_trunk(request)
        trunk_id = res.sip_trunk_id
        print("🎉 Telnyx SIP Outbound Trunk Created Successfully!")
        print("=" * 60)
        print(f"   SIP_OUTBOUND_TRUNK_ID = {trunk_id}")
        print("=" * 60)
        print("\nNext step: Add this Trunk ID to your server's .env file:")
        print(f"TELEPHONY_PROVIDER=livekit_sip")
        print(f"SIP_OUTBOUND_TRUNK_ID={trunk_id}")
        print(f"SIP_PHONE_NUMBER_ID={args.phone}\n")
    except Exception as e:
        print(f"❌ Failed to create SIP Outbound Trunk: {e}")
        sys.exit(1)
    finally:
        await lkapi.aclose()


def main() -> None:
    args = parse_args()
    asyncio.run(register_trunk(args))


if __name__ == "__main__":
    main()

