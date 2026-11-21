"""
vira-client — entry point.
Run: python main.py
"""

import asyncio
import os
import sys
import threading
import uuid
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv
load_dotenv()

from PyQt6.QtWidgets import QApplication
import avatar_window as aw
import ws_client
import agent

ACCESS_ID = os.environ.get("VEERA_ACCESS_ID", "vira-desktop")

_window: aw.AvatarWindow | None = None
_agent_id: str | None = None
_local_web_server: ThreadingHTTPServer | None = None


def _start_local_web_server() -> str:
    global _local_web_server

    client_dir = os.path.dirname(os.path.abspath(__file__))
    host = "127.0.0.1"
    port = int(os.environ.get("VIRA_LOCAL_PORT", "8021"))
    handler = partial(SimpleHTTPRequestHandler, directory=client_dir)
    _local_web_server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=_local_web_server.serve_forever, daemon=True).start()
    return f"http://{host}:{port}"


# ── WS callbacks → avatar state ───────────────────────────────────────────────

async def on_say(text: str, priority: str):
    # Speak through the Agora agent so the voice matches; the web fallback needs a TTS endpoint that no longer exists.
    if _agent_id:
        try:
            await agent.say(_agent_id, text, priority)
            return
        except Exception as exc:
            print(f"[vira-client] agent say failed, using fallback: {exc}", flush=True)
    if _window:
        _window.play_say(text)


async def on_ws_connect(ws):
    if _window:
        _window.state_requested.emit("idle")


async def on_avatar_state(state: str):
    if _window:
        _window.state_requested.emit(state)


async def on_transcript(speaker: str, text: str):
    if _window:
        _window.transcript_requested.emit(speaker, text)


# ── Async loop in background thread ───────────────────────────────────────────

def _run_async(loop: asyncio.AbstractEventLoop, client_id: str, rtc_config: dict, access_id: str, user_name: str):
    asyncio.set_event_loop(loop)

    async def run_client():
        global _agent_id
        agent_id = await agent.start(
            channel=rtc_config["channel"],
            user_uid=rtc_config["uid"],
            agent_uid=rtc_config["agent_uid"],
            access_id=access_id,
            user_name=user_name,
        )
        _agent_id = agent_id
        print(f"[vira-client] Agora agent started: {agent_id}", flush=True)
        await ws_client.connect(
            client_id,
            on_say=on_say,
            on_connect=on_ws_connect,
            on_state=on_avatar_state,
            on_transcript=on_transcript,
        )

    loop.run_until_complete(run_client())


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _window

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)

    local_url = _start_local_web_server()
    os.environ["VIRA_LOCAL_URL"] = local_url

    _window = aw.AvatarWindow()
    _window.show()

    rtc_config = agent.make_channel()
    access_id = os.environ.get("VEERA_ACCESS_ID", "vira-desktop")
    user_name = os.environ.get("VEERA_USER_NAME", "User")

    # run WS client and start the client-owned Agora agent in a background thread
    client_id = str(uuid.uuid4())
    _window.load_audio(client_id, rtc_config, access_id, user_name)
    loop = asyncio.new_event_loop()
    t = threading.Thread(
        target=_run_async,
        args=(loop, client_id, rtc_config, access_id, user_name),
        daemon=True,
    )
    t.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
