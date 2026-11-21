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
_GENERIC_FILE_WORDS = frozenset({"file", "document", "documents", "pdf", "doc", "docx"})
EXPLAINABLE_EXTENSIONS = frozenset({".pdf", ".docx", ".txt", ".md"})
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_FULL_PDF_PAGES = 20
MAX_SELECTED_PAGES = 5
DOCUMENT_REF_TTL_SECONDS = 600
_DOCUMENT_REFS: dict[str, tuple[Path, int, float, float]] = {}
_DOCUMENT_REFS_LOCK = threading.Lock()
_FILE_CHOICES: dict[str, tuple[Path, int, int, float]] = {}
_FILE_CHOICES_LOCK = threading.Lock()
FILE_CHOICE_TTL_SECONDS = 600
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


def compile_filename_pattern(pattern: str | None):
    """Compile a small, bounded filename-only regex; paths, groups, alternation and nested quantifiers are rejected."""
    if pattern is None or not str(pattern).strip():
        return None
    pattern = str(pattern).strip()
    if len(pattern) > 100:
        raise ValueError("Filename pattern must be at most 100 characters.")
    if '/' in pattern or re.search(r'\\(?!\.)', pattern):
        raise ValueError("Filename patterns may match names only, not paths.")
    if not re.fullmatch(r'[A-Za-z0-9 _\.\-\^$*?\\]+', pattern):
        raise ValueError("Pattern supports filename text, ., *, ?, ^, $, and escaped dots only.")
    if re.search(r'[*?]{2,}', pattern):
        raise ValueError("Consecutive quantifiers are not allowed in filename patterns.")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"Invalid filename pattern: {exc}")


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
    return {"ok": True, "file": path.name, "path": str(path), "location": "Pictures/Screenshots"}


def find_file_matches(
    query: str,
    roots: list[Path] | None = None,
    max_depth: int = 4,
    budget: float = 3.0,
    limit: int = 8,
    filename_pattern: str | None = None,
    owner_hint: str = "",
) -> list[tuple[int, float, Path]]:
    """Rank filename matches without substring false positives such as 're' inside 'resume'."""
    text = (query or "").strip()
    stem, ext = os.path.splitext(text)
    wanted_ext = ext.lower() if ext.lower() in OPENABLE_EXTENSIONS else None
    words = re.findall(r"[a-z0-9]+", (stem if wanted_ext else text).lower())
    if not wanted_ext and words and f".{words[-1]}" in OPENABLE_EXTENSIONS:
        wanted_ext = f".{words.pop()}"
    words = [word for word in words if (len(word) >= 3 or word == "cv") and word not in _GENERIC_FILE_WORDS]
    pattern = compile_filename_pattern(filename_pattern)
    if not words and pattern is None:
        return []
    wanted = "".join(words)
    hint_words = set(re.findall(r"[a-z0-9]+", owner_hint.lower()))
    deadline = time.monotonic() + budget
    matches: list[tuple[int, float, Path]] = []

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
                    if pattern and not pattern.search(entry.name):
                        continue
                    name_words = re.findall(r"[a-z0-9]+", os.path.splitext(entry.name)[0].lower())
                    name_key = "".join(name_words)
                    owner_priority = int(bool(hint_words) and hint_words.issubset(name_words))
                    if name_key == wanted:
                        score = 4
                    elif len(wanted) >= 4 and name_key.startswith(wanted):
                        score = 3
                    elif all(word in name_words for word in words):
                        score = 2
                    else:
                        continue
                    candidate = (score + owner_priority * 10, entry.stat().st_mtime, Path(entry.path))
                except OSError:
                    continue
                matches.append(candidate)
    matches.sort(key=lambda candidate: (candidate[0], candidate[1]), reverse=True)
    return matches[:limit]


def find_file(query: str, roots: list[Path] | None = None, max_depth: int = 4, budget: float = 3.0) -> Path | None:
    matches = find_file_matches(query, roots, max_depth, budget, limit=1)
    return matches[0][2] if matches else None


def _launch(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - extension allowlist enforced by find_file
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def search_file_candidates(query: str, owner_hint: str = "", folder: str = "", filename_pattern: str = "") -> dict:
    roots = search_roots()
    if folder:
        normalized_folder = folder.strip().lower()
        if normalized_folder not in _SHELL_FOLDER_KEYS:
            return {"ok": False, "error": "Unsupported search folder."}
        roots = [known_folder(normalized_folder)]
    try:
        matches = find_file_matches(query, roots=roots, limit=5, filename_pattern=filename_pattern, owner_hint=owner_hint)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    candidates = []
    now = time.monotonic()
    with _FILE_CHOICES_LOCK:
        for token, entry in list(_FILE_CHOICES.items()):
            if entry[3] <= now:
                _FILE_CHOICES.pop(token, None)
        for _, _, path in matches[:5]:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            token = secrets.token_urlsafe(18)
            _FILE_CHOICES[token] = (path, stat.st_size, stat.st_mtime_ns, now + FILE_CHOICE_TTL_SECONDS)
            location = path.parent.name
            for root in roots:
                try:
                    relative = path.parent.relative_to(root)
                    location = root.name if str(relative) == '.' else f"{root.name}/{relative.as_posix()}"
                    break
                except ValueError:
                    continue
            candidates.append({"id": token, "name": path.name, "folder": location})
    return {"ok": True, "candidates": candidates}


def open_file_candidate(candidate_id: str) -> dict:
    with _FILE_CHOICES_LOCK:
        entry = _FILE_CHOICES.pop(candidate_id, None)
    if not entry or entry[3] <= time.monotonic():
        return {"ok": False, "error": "That choice expired. Please search again."}
    path, size, modified, _ = entry
    try:
        stat = path.stat()
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in OPENABLE_EXTENSIONS or stat.st_size != size or stat.st_mtime_ns != modified:
            return {"ok": False, "error": "That file changed or is no longer available. Please search again."}
        _launch(path)
    except OSError:
        return {"ok": False, "error": "Could not open the selected file."}
    result = {"ok": True, "file": path.name, "message": "Sent the file to its default application."}
    if path.suffix.lower() in EXPLAINABLE_EXTENSIONS:
        result["document"] = inspect_document_path(path)
    return result


def open_file_by_name(name: str, folder: str | None = None, filename_pattern: str | None = None) -> dict:
    roots = None
    if folder:
        normalized_folder = folder.strip().lower()
        if normalized_folder not in _SHELL_FOLDER_KEYS:
            return {"ok": False, "error": f'Unsupported folder "{folder}".'}
        root = known_folder(normalized_folder)
        if not root.is_dir():
            return {"ok": False, "error": f'The {folder} folder is unavailable.'}
        roots = [root]
    try:
        matches = find_file_matches(name, roots=roots, filename_pattern=filename_pattern)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not matches:
        return {"ok": False, "error": f'No document or media file matching "{name}" was found.'}
    best_score = matches[0][0]
    equally_good = [path for score, _, path in matches if score == best_score]
    if len(equally_good) > 1:
        choices = equally_good[:5]
        name_counts: dict[str, int] = {}
        for path in equally_good:
            name_counts[path.name.casefold()] = name_counts.get(path.name.casefold(), 0) + 1
        return {
            "ok": False,
            "status": "ambiguous",
            "matches": [
                f"{path.name} in {path.parent.name}" if name_counts[path.name.casefold()] > 1 else path.name
                for path in choices
            ],
            "error": "More than one file matches that name.",
        }
    path = equally_good[0]
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
    return inspect_document_path(path)


def inspect_document_path(path: Path) -> dict:
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


def run_action(action: dict) -> dict:
    """Run one model-requested action; never raises."""
    kind = action.get("action") if isinstance(action, dict) else None
    try:
        if kind == "open_browser":
            return open_site(action.get("url"))
        if kind == "take_screenshot":
            return take_screenshot()
        if kind == "open_file":
            return open_file_by_name(
                str(action.get("name") or ""),
                action.get("folder"),
                action.get("filename_pattern"),
            )
        return {"ok": False, "error": f"Unknown action: {kind}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def outcome_text(action: dict, result: dict) -> str:
    """The sentence to speak once the action finished; {file} becomes the matched file's name."""
    if action.get("action") == "take_screenshot" and result.get("ok"):
        return "The screenshot was saved in your Pictures/Screenshots folder."
    if action.get("action") == "open_file":
        if result.get("status") == "ambiguous":
            names = [str(name) for name in (result.get("matches") or [])[:5]]
            suffix = ". Say which to open, or give more detail."
            while names and len(f"Several files match: {', '.join(names)}{suffix}") > MAX_MESSAGE_CHARS:
                names.pop()
            if not names:
                return "I found several files. Please say the full filename you want me to open."
            return f"Several files match: {', '.join(names)}{suffix}"
        if not result.get("ok"):
            requested = ' '.join(str(action.get("name") or "").split())
            if requested:
                return f'I could not find a file matching "{requested}". Please say more of its filename or its folder.'[:MAX_MESSAGE_CHARS]
            return "I could not find that file. Please say more of its filename or its folder."
        filename = str(result.get("file") or action.get("name") or "the file")
        return f"Opened {filename}."
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
