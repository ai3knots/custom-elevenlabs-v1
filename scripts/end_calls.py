"""Utility to terminate all active LiveKit rooms and hang up any ongoing SIP calls."""

import asyncio
import os
import sys
from livekit import api
from dotenv import load_dotenv

load_dotenv()

async def end_all_calls() -> None:
    livekit_url = os.getenv("LIVEKIT_URL", "")
    api_key = os.getenv("LIVEKIT_API_KEY", "")
    api_secret = os.getenv("LIVEKIT_API_SECRET", "")

    if not livekit_url or not api_key or not api_secret:
        print("Error: LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set in .env")
        sys.exit(1)

    print(f"Connecting to LiveKit server at: {livekit_url}")
    lkapi = api.LiveKitAPI(url=livekit_url, api_key=api_key, api_secret=api_secret)

    try:
        response = await lkapi.room.list_rooms(api.ListRoomsRequest())
        if not response.rooms:
            print("No active rooms or ongoing calls found.")
            return

        print(f"Found {len(response.rooms)} active room(s). Terminating...")
        for room in response.rooms:
            print(f"Ending call in room: {room.name} ({room.num_participants} participants)")
            try:
                await lkapi.room.delete_room(api.DeleteRoomRequest(room=room.name))
                print(f"  ✓ Terminated room {room.name}")
            except Exception as e:
                print(f"  ℹ Room {room.name} was already closed or inactive: {e}")

        print("All active calls and rooms have been terminated.")
    finally:
        await lkapi.aclose()

if __name__ == "__main__":
    asyncio.run(end_all_calls())

