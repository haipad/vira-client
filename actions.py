"""
Desktop action executor.
Add new action types here as the feature set grows.

The model-driven actions (open_browser, take_screenshot, open_file via run_action) are the
authoritative safety boundary: the backend is only advisory, so everything is re-validated here.
"""

import ipaddress
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

BROWSER_HOME = os.environ.get("VIRA_BROWSER_HOME", "https://www.google.com")
MAX_MESSAGE_CHARS = 200

# Documents and media only: opening a file runs its default handler, so executables and scripts stay out.
OPENABLE_EXTENSIONS = frozenset(
    ".pdf .txt .md .rtf .csv .doc .docx .xls .xlsx .ppt .pptx .odt .ods .odp "
    ".png .jpg .jpeg .gif .bmp .webp .svg .mp3 .wav .m4a .flac .mp4 .mkv .mov .avi .webm".split()
)
_SKIP_DIRS = frozenset({"node_modules", "__pycache__", "venv", "appdata", "$recycle.bin", "site-packages"})
EXPLAINABLE_EXTENSIONS = frozenset({".pdf", ".docx", ".txt", ".md"})
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_FULL_PDF_PAGES = 20
MAX_SELECTED_PAGES = 5
DOCUMENT_REF_TTL_SECONDS = 600
_DOCUMENT_REFS: dict[str, tuple[Path, int, float, float]] = {}
_DOCUMENT_REFS_LOCK = threading.Lock()
_SHELL_FOLDER_KEYS = {
    "desktop": "Desktop",
    "documents": "Personal",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "pictures": "My Pictures",
    "videos": "My Video",
    "music": "My Music",
}


def known_folder(name: str) -> Path:
    """Real location of a user folder; Desktop/Documents/Pictures are often redirected into OneDrive."""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            ) as key:
                return Path(winreg.QueryValueEx(key, _SHELL_FOLDER_KEYS[name])[0])
        except OSError:
            pass
    return Path.home() / name.capitalize()


def search_roots() -> list[Path]:
    roots: list[Path] = []
    for name in _SHELL_FOLDER_KEYS:
        folder = known_folder(name)
        if folder.is_dir() and folder not in roots:
            roots.append(folder)
    return roots


def check_url(raw: str | None) -> str:
    """Return a safe http(s) URL or raise ValueError; no local/private targets, no embedded credentials."""
    text = (raw or "").strip()
    if not text:
        return BROWSER_HOME
    if "://" not in text:
        text = f"https://{text}"
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise ValueError("Only http and https websites can be opened.")
    if parsed.username or parsed.password:
        raise ValueError("Web addresses with embedded credentials are not allowed.")
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise ValueError("Local addresses are not allowed.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("That is not a full website address.")
    else:
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("Private network addresses are not allowed.")
    return text


def open_site(url: str | None) -> dict:
    try:
        target = check_url(url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not webbrowser.open(target):
        return {"ok": False, "error": "No browser could be launched."}
    return {"ok": True, "url": target}


def take_screenshot() -> dict:
    try:
        from PIL import ImageGrab
    except ImportError:
        return {"ok": False, "error": "Pillow is not installed."}
    folder = known_folder("pictures") / "Screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"
    ImageGrab.grab(all_screens=True).save(path)
    return {"ok": True, "file": path.name, "path": str(path)}


def find_file(query: str, roots: list[Path] | None = None, max_depth: int = 4, budget: float = 3.0) -> Path | None:
    """Best openable file whose name matches: exact name, then prefix, then all words; newest wins ties."""
    text = (query or "").strip()
    stem, ext = os.path.splitext(text)
    wanted_ext = ext.lower() if ext.lower() in OPENABLE_EXTENSIONS else None
    words = re.findall(r"[a-z0-9]+", (stem if wanted_ext else text).lower())
    if not words:
        return None
    wanted = "".join(words)
    deadline = time.monotonic() + budget
    best: tuple[int, float, Path] | None = None

    for root in roots if roots is not None else search_roots():
        stack = [(root, 0)]
        while stack and time.monotonic() < deadline:
            folder, depth = stack.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        name = entry.name.lower()
                        if depth < max_depth and not name.startswith(".") and name not in _SKIP_DIRS:
                            stack.append((Path(entry.path), depth + 1))
                        continue
                    suffix = os.path.splitext(entry.name)[1].lower()
                    if suffix not in OPENABLE_EXTENSIONS or (wanted_ext and suffix != wanted_ext):
                        continue
                    name_key = re.sub(r"[^a-z0-9]+", "", os.path.splitext(entry.name)[0].lower())
                    if name_key == wanted:
                        score = 3
                    elif name_key.startswith(wanted):
                        score = 2
                    elif all(word in name_key for word in words):
                        score = 1
                    else:
                        continue
                    candidate = (score, entry.stat().st_mtime, Path(entry.path))
                except OSError:
                    continue
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
    return best[2] if best else None


def _launch(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - extension allowlist enforced by find_file
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def open_file_by_name(name: str) -> dict:
    path = find_file(name)
    if path is None:
        return {"ok": False, "error": f'No document or media file matching "{name}" was found.'}
    _launch(path)
    return {"ok": True, "file": path.name, "path": str(path)}


def _document_too_large(path: Path, page_count: int | None = None) -> dict:
    result = {
        "ok": False,
        "status": "too_large",
        "file": path.name,
        "message": (
            "This document is too large to send for explanation all at once. Ask the user which pages to explain; "
            f"suggest the first {MAX_SELECTED_PAGES} pages."
        ),
    }
    if page_count is not None:
        result["page_count"] = page_count
        result["max_pages_per_request"] = MAX_SELECTED_PAGES
    else:
        result["message"] += " For this non-paginated file, ask for a smaller file or a specific section."
    return result


def inspect_document_by_name(name: str) -> dict:
    """Return metadata only; no document text is extracted or sent."""
    path = find_file(name)
    if path is None or path.suffix.lower() not in EXPLAINABLE_EXTENSIONS:
        return {"ok": False, "status": "not_found", "error": f'No PDF, DOCX, TXT, or Markdown file matching "{name}" was found.'}
    try:
        stat = path.stat()
        if stat.st_size > MAX_DOCUMENT_BYTES:
            return {"ok": False, "status": "too_large", "file": path.name,
                    "size_bytes": stat.st_size, "max_bytes": MAX_DOCUMENT_BYTES}
        page_count = None
        if path.suffix.lower() == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            page_count = len(reader.pages)
        ref = secrets.token_urlsafe(24)
        expires_at = time.monotonic() + DOCUMENT_REF_TTL_SECONDS
        with _DOCUMENT_REFS_LOCK:
            for old_ref, value in list(_DOCUMENT_REFS.items()):
                if value[3] <= time.monotonic():
                    _DOCUMENT_REFS.pop(old_ref, None)
            _DOCUMENT_REFS[ref] = (path, stat.st_size, stat.st_mtime, expires_at)
        return {
            "ok": True,
            "status": "inspected",
            "document_ref": ref,
            "file": path.name,
            "size_bytes": stat.st_size,
            "page_count": page_count,
            "requires_page_selection": page_count is not None and page_count > MAX_FULL_PDF_PAGES,
        }
    except Exception as exc:
        return {"ok": False, "status": "inspect_error", "file": path.name, "error": str(exc)}


def read_document_ref(
    document_ref: str,
) -> dict:
    """Return bytes for a previously inspected file, after revalidating its identity and size."""
    with _DOCUMENT_REFS_LOCK:
        entry = _DOCUMENT_REFS.get(document_ref)
    if not entry:
        return {"ok": False, "status": "expired", "error": "The document reference expired. Inspect it again."}
    path, inspected_size, inspected_mtime, expires_at = entry
    try:
        stat = path.stat()
    except OSError:
        return {"ok": False, "status": "changed", "error": "The inspected file is no longer available."}
    if time.monotonic() > expires_at or stat.st_size != inspected_size or stat.st_mtime != inspected_mtime:
        with _DOCUMENT_REFS_LOCK:
            _DOCUMENT_REFS.pop(document_ref, None)
        return {"ok": False, "status": "changed", "error": "The file changed or the reference expired. Inspect it again."}
    if stat.st_size > MAX_DOCUMENT_BYTES:
        return _document_too_large(path)

    try:
        with path.open("rb") as source:
            data = source.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) > MAX_DOCUMENT_BYTES:
            return _document_too_large(path)
        return {"ok": True, "file": path.name, "path": path, "data": data}
    except Exception as exc:
        return {"ok": False, "status": "read_error", "file": path.name, "error": str(exc)}


def finish_document_ref(document_ref: str, path: Path, read_succeeded: bool, open_after_read: bool) -> None:
    if read_succeeded and open_after_read:
        _launch(path)
    if read_succeeded:
        with _DOCUMENT_REFS_LOCK:
            _DOCUMENT_REFS.pop(document_ref, None)


def run_action(action: dict) -> dict:
    """Run one model-requested action; never raises."""
    kind = action.get("action") if isinstance(action, dict) else None
    try:
        if kind == "open_browser":
            return open_site(action.get("url"))
        if kind == "take_screenshot":
            return take_screenshot()
        if kind == "open_file":
            return open_file_by_name(str(action.get("name") or ""))
        return {"ok": False, "error": f"Unknown action: {kind}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def outcome_text(action: dict, result: dict) -> str:
    """The sentence to speak once the action finished; {file} becomes the matched file's name."""
    ok = bool(result.get("ok"))
    text = str(action.get("success_message" if ok else "failure_message") or "").strip()
    if not text:
        text = "Done." if ok else "Sorry, that didn't work."
    return text.replace("{file}", str(result.get("file") or action.get("name") or "the file"))[:MAX_MESSAGE_CHARS]


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
