"""
WebSocket client — persistent connection to veera-backend.
Handles push messages: announcements, reminders, desktop commands.
Edit this file to change message routing or add new push types.
"""

import asyncio
import json
import os

import websockets

from dotenv import load_dotenv
from actions import execute

load_dotenv()

VEERA_URL = os.environ.get(
    "VEERA_BACKEND_URL",
    os.environ.get("VEERA_PUBLIC_URL", "http://127.0.0.1:8000"),
).rstrip("/")
WS_URL = VEERA_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws"


async def _handle(message: str, on_say=None, on_state=None):
    try:
        msg = json.loads(message)
    except json.JSONDecodeError:
        return

    msg_type = msg.get("type")

    if msg_type == "avatar_state":
        if on_state:
            await on_state(msg.get("state", "idle"))

    if msg_type == "say":
        # Backend wants to speak something — delegate to caller (main.py)
        if on_say:
            await on_say(msg.get("text", ""), msg.get("priority", "NORMAL"))

    elif msg_type in ("open_browser", "read_file", "run_command"):
        result = execute(msg_type, msg.get("payload", {}))
        # Result sent back via the same WS connection — caller handles the send
        return {"id": msg.get("id"), "type": "response", **result}

    return None


async def connect(client_id: str, on_say=None, on_connect=None, on_state=None):
    """
    Maintain a persistent WS connection with auto-reconnect.
    on_say(text, priority) — called when backend pushes a say message.
    on_connect() — called each time connection is established.
    """
    url = f"{WS_URL}?client_id={client_id}"
    backoff = 1

    while True:
        try:
            async with websockets.connect(url) as ws:
                backoff = 1
                if on_connect:
                    await on_connect(ws)

                async for raw in ws:
                    result = await _handle(raw, on_say=on_say, on_state=on_state)
                    if result:
                        await ws.send(json.dumps(result))

        except (websockets.ConnectionClosed, OSError):
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
