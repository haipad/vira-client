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
from actions import (
    execute,
    finish_document_ref,
    inspect_document_by_name,
    open_file_candidate,
    outcome_text,
    read_document_ref,
    run_action,
    search_file_candidates,
)

load_dotenv()

DESKTOP_KEY = os.environ.get("VEERA_DESKTOP_KEY", "")
MAX_ACTIONS = 5

VEERA_URL = os.environ.get(
    "VEERA_BACKEND_URL",
    os.environ.get("VEERA_PUBLIC_URL", "http://127.0.0.1:8000"),
).rstrip("/")
WS_URL = VEERA_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws"


def _upload_document(request_id: str, data: bytes) -> dict:
    from urllib.request import Request, urlopen

    url = f"{VEERA_URL}/desktop/document-upload/{request_id}"
    request = Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {DESKTOP_KEY}",
            "Content-Type": "application/octet-stream",
        },
        method="POST",
    )
    with urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


async def _handle(message: str, on_say=None, on_state=None, on_transcript=None):
    try:
        msg = json.loads(message)
    except json.JSONDecodeError:
        return

    msg_type = msg.get("type")

    if msg_type == "transcript":
        if on_transcript and msg.get("speaker") in ("user", "assistant") and isinstance(msg.get("text"), str):
            await on_transcript(msg["speaker"], msg["text"])
        return None

    if msg_type == "execute_action":
        result = await asyncio.to_thread(run_action, msg.get("action"))
        result.pop("path", None)
        return {"type": "document_result", "id": msg.get("id"), "result": result}

    if msg_type == "search_files":
        result = await asyncio.to_thread(
            search_file_candidates,
            msg.get("query", ""),
            msg.get("owner_hint", ""),
            msg.get("folder", ""),
            msg.get("filename_pattern", ""),
        )
        return {"type": "document_result", "id": msg.get("id"), "result": result}

    if msg_type == "open_selected_file":
        result = await asyncio.to_thread(open_file_candidate, msg.get("candidate_id", ""))
        return {"type": "document_result", "id": msg.get("id"), "result": result}

    if msg_type == "inspect_document":
        result = await asyncio.to_thread(inspect_document_by_name, msg.get("name", ""))
        return {"type": "document_result", "id": msg.get("id"), "result": result}

    if msg_type == "read_document":
        reference = msg.get("document_ref", "")
        local = await asyncio.to_thread(read_document_ref, reference)
        if not local.get("ok"):
            return {"type": "document_result", "id": msg.get("id"), "result": local}
        try:
            result = await asyncio.to_thread(_upload_document, msg.get("id", ""), local["data"])
            finish_document_ref(
                reference,
                local["path"],
                bool(result.get("ok")),
                bool(msg.get("open_after_read", False)),
            )
            return None
        except Exception as exc:
            return {
                "type": "document_result",
                "id": msg.get("id"),
                "result": {"ok": False, "status": "upload_error", "error": str(exc)},
            }

    if msg_type == "actions":
        await _run_actions(msg.get("actions"), on_say)
        return None

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


async def _run_actions(actions, on_say) -> None:
    """Run the backend's action list in order, speaking each one's own success or failure message."""
    if not isinstance(actions, list):
        return
    for action in actions[:MAX_ACTIONS]:
        # Blocking work (file search, screenshots) must not stall the WebSocket loop.
        result = await asyncio.to_thread(run_action, action)
        text = outcome_text(action if isinstance(action, dict) else {}, result)
        print(f"[vira-client] action {action.get('action') if isinstance(action, dict) else action}: "
              f"{'ok' if result.get('ok') else 'failed'} - "
              f"{result.get('error') or result.get('path') or result.get('file') or result.get('url') or ''}", flush=True)
        if on_say:
            await on_say(text, "APPEND")


async def connect(client_id: str, on_say=None, on_connect=None, on_state=None, on_transcript=None):
    """
    Maintain a persistent WS connection with auto-reconnect.
    on_say(text, priority) — called when backend pushes a say message.
    on_connect() — called each time connection is established.
    """
    url = f"{WS_URL}?client_id={client_id}"
    headers = {"Authorization": f"Bearer {DESKTOP_KEY}"} if DESKTOP_KEY else None
    backoff = 1

    while True:
        try:
            async with websockets.connect(url, additional_headers=headers) as ws:
                backoff = 1
                if on_connect:
                    await on_connect(ws)

                async for raw in ws:
                    result = await _handle(raw, on_say=on_say, on_state=on_state, on_transcript=on_transcript)
                    if result:
                        await ws.send(json.dumps(result))

        except (websockets.ConnectionClosed, OSError):
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
