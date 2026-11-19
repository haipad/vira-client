"""
Desktop action executor.
Add new action types here as the feature set grows.
"""

import subprocess
import webbrowser
from pathlib import Path


def open_browser(url: str) -> dict:
    webbrowser.open(url)
    return {"ok": True, "url": url}


def read_file(path: str) -> dict:
    try:
        content = Path(path).read_text(errors="replace")
        return {"ok": True, "content": content}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_command(cmd: str) -> dict:
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=10
        )
        return {"ok": True, "stdout": result.stdout, "stderr": result.stderr}
    except Exception as e:
        return {"ok": False, "error": str(e)}


HANDLERS = {
    "open_browser": lambda p: open_browser(p["url"]),
    "read_file": lambda p: read_file(p["path"]),
    "run_command": lambda p: run_command(p["cmd"]),
}


def execute(action_type: str, payload: dict) -> dict:
    handler = HANDLERS.get(action_type)
    if not handler:
        return {"ok": False, "error": f"Unknown action: {action_type}"}
    return handler(payload)
