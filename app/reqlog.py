"""Recent-request log: one JSON line per search or extraction.

Why a file and not a deque in process memory: the service runs with
``server.workers > 1`` (uvicorn forks), so an in-memory ring buffer would only
hold the requests that happened to land on the worker answering the query, and
the numbers would look random. A file is shared by every worker, survives a
config reload and can be read tail-first cheaply.

The log is append-only, capped by size (one rotation), and never allowed to
break a request: every failure here is swallowed.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

PATH = Path(os.environ.get("FORAGE_REQLOG", "/var/lib/forage/requests.jsonl"))
MAX_BYTES = int(os.environ.get("FORAGE_REQLOG_MAX_BYTES", str(4 * 1024 * 1024)))
MAX_FIELD = int(os.environ.get("FORAGE_REQLOG_FIELD", "800"))
ENABLED = os.environ.get("FORAGE_REQLOG", "1").strip().lower() not in ("0", "false", "off", "no", "")

_lock = threading.Lock()


def _trim(value: Any) -> Any:
    """Keep one record small: long queries/URLs are cut, never the whole line."""
    if isinstance(value, str):
        return value if len(value) <= MAX_FIELD else value[:MAX_FIELD] + "…"
    if isinstance(value, (list, tuple)):
        return [_trim(v) for v in list(value)[:20]]
    if isinstance(value, dict):
        return {k: _trim(v) for k, v in list(value.items())[:20]}
    return value


def _rotate_if_needed() -> None:
    try:
        if PATH.stat().st_size < MAX_BYTES:
            return
    except FileNotFoundError:
        return
    except OSError:
        return
    # os.replace is atomic: a second worker rotating at the same instant just
    # replaces the archive with the same content.
    try:
        os.replace(PATH, PATH.with_name(PATH.name + ".1"))
    except OSError:
        pass


def record(kind: str, **fields: Any) -> None:
    """Append one event. Silently does nothing on any I/O problem."""
    if not ENABLED:
        return
    entry: Dict[str, Any] = {"ts": round(time.time(), 3), "kind": kind}
    entry.update({k: _trim(v) for k, v in fields.items() if v is not None})
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    try:
        with _lock:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed()
            # One write() per line under the lock: no interleaved records even
            # with several workers appending at once.
            with PATH.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        pass


def _read_lines(path: Path, limit: int) -> List[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    # Only the tail matters; splitting a 4 MB file is cheap even so.
    return data.decode("utf-8", "replace").splitlines()[-limit:]


def tail(limit: int = 100, kind: Any = None, status: Any = None) -> List[Dict[str, Any]]:
    """Newest-first slice of the log, optionally filtered by kind/status."""
    limit = max(1, min(1000, int(limit)))
    out: List[Dict[str, Any]] = []
    # Read a generous window so a filter still fills ``limit``.
    window = limit if not (kind or status) else min(5000, limit * 10)
    for raw in reversed(_read_lines(PATH, window)):
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        if kind and item.get("kind") != kind:
            continue
        if status and item.get("status") != status:
            continue
        out.append(item)
        if len(out) >= limit:
            break
    if len(out) < limit:
        # The archive holds the previous rotation; only worth reading when the
        # live file did not fill the request.
        archived = PATH.with_name(PATH.name + ".1")
        for raw in reversed(_read_lines(archived, window)):
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            if kind and item.get("kind") != kind:
                continue
            if status and item.get("status") != status:
                continue
            out.append(item)
            if len(out) >= limit:
                break
    return out


def stats() -> Dict[str, Any]:
    """Cheap health of the log itself, for the admin endpoint."""
    info: Dict[str, Any] = {"enabled": ENABLED, "path": str(PATH), "max_bytes": MAX_BYTES}
    try:
        st = PATH.stat()
        info.update({"bytes": st.st_size, "mtime": round(st.st_mtime, 3)})
    except OSError:
        info.update({"bytes": 0, "mtime": None})
    return info
