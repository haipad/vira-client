"""
vira-client — entry point.
Run: python main.py
"""

import asyncio
import os
import sys
import threading
import uuid

from dotenv import load_dotenv
load_dotenv()

from PyQt6.QtWidgets import QApplication
import avatar_window as aw
import ws_client

ACCESS_ID = os.environ.get("VEERA_ACCESS_ID", "vira-desktop")

_window: aw.AvatarWindow | None = None


# ── WS callbacks → avatar state ───────────────────────────────────────────────

async def on_say(text: str, priority: str):
    # Actual listening/speaking/idle state comes from Agora volume events.
    # This callback is reserved for side effects triggered by backend pushes.
    pass


async def on_ws_connect(ws):
    if _window:
        _window.set_state("idle")


async def on_avatar_state(state: str):
    if _window:
        _window.set_state(state)


# ── Async loop in background thread ───────────────────────────────────────────

def _run_async(loop: asyncio.AbstractEventLoop, client_id: str):
    asyncio.set_event_loop(loop)
    loop.run_until_complete(
        ws_client.connect(
            client_id,
            on_say=on_say,
            on_connect=on_ws_connect,
            on_state=on_avatar_state,
        )
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _window

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)

    _window = aw.AvatarWindow()
    _window.show()

    # run WS client in a background thread
    client_id = str(uuid.uuid4())
    _window.load_audio(client_id)
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=_run_async, args=(loop, client_id), daemon=True)
    t.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
