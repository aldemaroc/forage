"""Kill a browser tree by session, so nothing is left to guess about.

Chromium is not a child tree that inherits one session: it calls setsid() itself,
so the browser process becomes a session leader and its renderers and utilities
share that session, while the crashpad handler double-forks into a third session.
A Playwright/Patchright driver therefore owns several sessions, not one.

Two rules, in this order:

1. The owner kills its own tree. The teardown collects the sessions while the tree
   is still alive (`tree_sessions`), closes the browser gracefully, then kills the
   collected sessions (`kill_sessions`). Collecting first is what makes it work: a
   graceful close takes the driver down, and a dead driver has no subtree to walk.

2. The supervisor's janitor cleans what the owner could not. It touches a session
   only when the registry says the owner is gone, or when a browser has no live
   driver above it, is not a zombie, is not claimed by a live browser as its
   crashpad handler, and carries a Playwright path marker. The marker check is
   what keeps it from killing a desktop Chrome when Forage runs outside a
   container.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import json
import logging
import os
import re
import signal
import tempfile
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Set

from . import metrics

logger = logging.getLogger(__name__)

# Only a Playwright/Patchright driver is spawned with this argument, in every
# engine we use (playwright, patchright, scrapling through patchright), so it is
# the filter that keeps the patch from touching any other subprocess.
_DRIVER_ARG = "run-driver"

# Playwright installs its browsers under this path and names its temporary
# profiles like this. A browser carrying one of these markers is ours to clean; a
# desktop Chrome does not, which is what makes the janitor safe outside a
# container.
_BROWSER_MARKERS = ("ms-playwright", "playwright_chromiumdev_profile")

_COMM_PREFIXES = ("chrome", "chromium")

_CRASHPAD_CLAIM = re.compile(r"--crashpad-handler-pid=(\d+)")

_capture: contextvars.ContextVar[Optional[List[int]]] = contextvars.ContextVar(
    "forage_session_capture", default=None
)

_installed = False
_session_dir = os.path.join(tempfile.gettempdir(), "forage-sessions")


# -- /proc helpers -----------------------------------------------------------


def _stat(pid: int) -> Optional[Dict[str, Any]]:
    try:
        with open("/proc/%d/stat" % pid) as handle:
            raw = handle.read()
    except OSError:
        return None
    comm = raw.split("(", 1)[1].rsplit(")", 1)[0]
    fields = raw.rsplit(")", 1)[1].split()
    try:
        return {
            "comm": comm,
            "state": fields[0],
            "ppid": int(fields[1]),
            "sid": int(fields[3]),
            "start": int(fields[19]),
        }
    except (IndexError, ValueError):
        return None


def _cmdline(pid: int) -> str:
    try:
        with open("/proc/%d/cmdline" % pid) as handle:
            return handle.read().replace("\x00", " ")
    except OSError:
        return ""


def _snapshot() -> Dict[int, Dict[str, Any]]:
    snap: Dict[int, Dict[str, Any]] = {}
    try:
        entries = [int(e) for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return snap
    for pid in entries:
        row = _stat(pid)
        if row is None:
            continue
        row["cmd"] = _cmdline(pid)
        snap[pid] = row
    return snap


def _is_chrome(row: Dict[str, Any]) -> bool:
    return row["comm"].startswith(_COMM_PREFIXES)


def _is_playwright_browser(row: Dict[str, Any]) -> bool:
    return any(marker in row["cmd"] for marker in _BROWSER_MARKERS)


def _subtree(root: int, snap: Dict[int, Dict[str, Any]]) -> List[int]:
    children: Dict[int, List[int]] = {}
    for pid, row in snap.items():
        children.setdefault(row["ppid"], []).append(pid)
    out: List[int] = []
    stack = [root]
    seen: Set[int] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        out.append(current)
        stack.extend(children.get(current, ()))
    return out


def _has_driver_ancestor(pid: int, snap: Dict[int, Dict[str, Any]]) -> bool:
    """True unless we are sure no live driver is above this process.

    Conservative on purpose: a chain we cannot read completely (a pid that
    vanished mid-scan, an empty cmdline) counts as alive, because killing a live
    browser is worse than killing an orphan one sweep later.
    """
    current = snap.get(pid)
    seen: Set[int] = set()
    while current is not None and current["ppid"] not in (0,) and current["ppid"] not in seen:
        parent = current["ppid"]
        seen.add(parent)
        row = snap.get(parent)
        if row is None:
            return True
        if _DRIVER_ARG in row["cmd"]:
            return True
        if not row["cmd"]:
            return True
        current = row
    return False


def _claimed_crashpads(pids: Iterable[int], snap: Dict[int, Dict[str, Any]]) -> Set[int]:
    """Crashpad handlers named by a LIVE Chromium on its own command line."""
    claimed: Set[int] = set()
    for pid in pids:
        row = snap.get(pid)
        if row is None or not _is_chrome(row) or row["state"] in ("Z", "X"):
            continue
        for match in _CRASHPAD_CLAIM.finditer(row["cmd"]):
            claimed.add(int(match.group(1)))
    return claimed


def _own_sessions() -> Set[int]:
    """Our session and process group: never a kill target, whatever happens."""
    try:
        return {os.getsid(0), os.getpgrp()}
    except OSError:
        return {os.getpgrp()}


# -- registry ----------------------------------------------------------------


def configure(port: int) -> None:
    """One registry per instance, so two Forage on a host do not collide."""
    global _session_dir
    _session_dir = os.path.join(tempfile.gettempdir(), "forage-sessions-%d" % int(port))


def _registry_path(driver_pid: int) -> str:
    return os.path.join(_session_dir, "%d.json" % driver_pid)


def _proc_start(pid: int) -> Optional[int]:
    row = _stat(pid)
    return row["start"] if row else None


def register(driver_pid: int) -> None:
    """Record a live driver, its owner, and both start times."""
    entry = {
        "owner_pid": os.getpid(),
        "owner_start": _proc_start(os.getpid()),
        "driver_start": _proc_start(driver_pid),
        "created": time.time(),
    }
    try:
        os.makedirs(_session_dir, exist_ok=True)
        tmp = _registry_path(driver_pid) + ".tmp"
        with open(tmp, "w") as handle:
            json.dump(entry, handle)
        os.replace(tmp, _registry_path(driver_pid))
    except OSError as exc:
        logger.warning("Could not record driver %d: %s", driver_pid, exc)


def _unregister(driver_pid: int) -> None:
    try:
        os.unlink(_registry_path(driver_pid))
    except OSError:
        pass


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _entries() -> List[Dict[str, Any]]:
    try:
        names = [n for n in os.listdir(_session_dir) if n.endswith(".json")]
    except OSError:
        return []
    out = []
    for name in names:
        path = os.path.join(_session_dir, name)
        try:
            with open(path) as handle:
                entry = json.load(handle)
            entry["driver_pid"] = int(name[: -len(".json")])
        except (OSError, ValueError):
            _unlink(path)
            continue
        out.append(entry)
    return out


def reset_registry() -> None:
    """Drop entries from a previous run of this instance. Supervisor only."""
    try:
        for name in os.listdir(_session_dir):
            if name.endswith(".json") or name.endswith(".tmp"):
                _unlink(os.path.join(_session_dir, name))
    except OSError:
        pass


def _owner_alive(entry: Dict[str, Any]) -> bool:
    owner_pid = int(entry.get("owner_pid") or 0)
    owner_start = entry.get("owner_start")
    if owner_pid <= 1 or owner_start is None:
        return False
    return _proc_start(owner_pid) == owner_start


def _driver_alive(entry: Dict[str, Any]) -> bool:
    """Alive AND still the same process: a reused pid is not our driver."""
    driver_pid = entry["driver_pid"]
    current = _proc_start(driver_pid)
    if current is None:
        return False
    recorded = entry.get("driver_start")
    return recorded is None or current == recorded


# -- killing ----------------------------------------------------------------


def tree_sessions(driver_pids: Iterable[int]) -> List[int]:
    """Session ids of the live browser trees under these drivers.

    Call it BEFORE closing anything: the driver is the root of the subtree, and a
    graceful close takes it down.
    """
    snap = _snapshot()
    sessions: Set[int] = set()
    for driver_pid in driver_pids:
        if driver_pid not in snap:
            continue
        sessions.add(driver_pid)
        subtree = _subtree(driver_pid, snap)
        for pid in subtree:
            row = snap.get(pid)
            if row is not None and row["sid"] == pid:
                sessions.add(pid)  # a leader: the browser, a crashpad handler
        for handler in _claimed_crashpads(subtree, snap):
            row = snap.get(handler)
            sessions.add(row["sid"] if row else handler)
    return sorted(sessions)


def kill_sessions(
    session_ids: Iterable[int], drivers: Iterable[int] = (), reason: str = "close"
) -> int:
    """SIGKILL each session. Returns how many sessions it managed to kill."""
    guard = _own_sessions()
    killed = 0
    for session in session_ids:
        if session <= 1 or session in guard:
            continue
        try:
            os.killpg(session, signal.SIGKILL)
            killed += 1
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            logger.warning("Could not kill session %d: %s", session, exc)
    for driver_pid in drivers:
        _unregister(driver_pid)
    if killed:
        logger.info("Killed %d browser session(s) on %s", killed, reason)
    return killed


def kill_trees(driver_pids: Iterable[int], reason: str = "close") -> int:
    """Collect the sessions of live trees and kill them right now."""
    drivers = list(driver_pids)
    if not drivers:
        return 0
    killed = kill_sessions(tree_sessions(drivers), drivers, reason)
    # Anything still standing appeared between the snapshot and the kill.
    snap = _snapshot()
    for driver_pid in drivers:
        for pid in _subtree(driver_pid, snap):
            row = snap.get(pid)
            if pid in (0, 1, os.getpid()) or row is None:
                continue
            if row["state"] in ("Z", "X"):
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                continue
    return killed


def sweep() -> int:
    """Clean after owners that died first. Returns how many sessions were killed."""
    metrics.observe_janitor(sweeps=1)
    killed = 0

    # 1) Registry: the owner is gone, so its driver has no client left.
    for entry in _entries():
        if _owner_alive(entry):
            continue
        driver_pid = entry["driver_pid"]
        if not _driver_alive(entry):
            # The driver is gone too, or the pid now belongs to something else.
            _unregister(driver_pid)
            continue
        logger.warning("Janitor: driver %d lost its owner, killing its tree", driver_pid)
        killed += kill_trees([driver_pid], reason="owner gone")

    # 2) Independent of the registry: a Playwright browser with no live driver
    #    above it has nobody driving it. A session is killed only when its leader
    #    is not a driven browser either, so one orphan inside a live session
    #    cannot drag that session down. Zombies are skipped: without an init they
    #    are never reaped and they carry the session of the browser they belonged
    #    to.
    snap = _snapshot()
    claimed = _claimed_crashpads(snap.keys(), snap)
    orphan_sessions: Dict[int, List[int]] = {}
    for pid, row in snap.items():
        if not _is_chrome(row) or row["state"] in ("Z", "X"):
            continue
        if not _is_playwright_browser(row):
            continue
        if pid in claimed or _has_driver_ancestor(pid, snap):
            continue
        orphan_sessions.setdefault(row["sid"], []).append(pid)
    killable: List[int] = []
    for session, members in orphan_sessions.items():
        leader = snap.get(session)
        if leader is not None and _has_driver_ancestor(session, snap):
            continue  # the leader is a browser that is still being driven
        killable.append(session)
        logger.warning(
            "Janitor: orphan browser session %d (%s)",
            session,
            ", ".join("%d/%s" % (pid, snap[pid]["comm"]) for pid in members[:4]),
        )
    if killable:
        killed += kill_sessions(killable, reason="orphan sweep")
    if killed:
        metrics.observe_janitor(kills=killed)
    return killed


# -- interception ------------------------------------------------------------


def install() -> None:
    """Make every driver spawned in this process a session leader.

    Idempotent, per process on purpose: the patch lives in memory, so it runs
    again in each uvicorn worker.
    """
    global _installed
    if _installed:
        return
    original = asyncio.create_subprocess_exec

    @functools.wraps(original)
    async def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
        is_driver = _DRIVER_ARG in args
        if is_driver:
            kwargs.setdefault("start_new_session", True)
        proc = await original(*args, **kwargs)
        if is_driver:
            register(proc.pid)
            scope = _capture.get()
            if scope is not None:
                scope.append(proc.pid)
        return proc

    asyncio.create_subprocess_exec = wrapper  # type: ignore[assignment]
    _installed = True
    logger.debug("Driver sessions enabled (pid %d)", os.getpid())


@contextlib.asynccontextmanager
async def capture():
    """Collect the driver pids spawned inside the block.

    The caller has to keep the list even when the block raises: a driver spawned
    and then orphaned by a failed start still needs to be killed.
    """
    driver_pids: List[int] = []
    token = _capture.set(driver_pids)
    try:
        yield driver_pids
    finally:
        _capture.reset(token)


def start_janitor_thread(interval: float = 30.0) -> None:
    """Sweep from the supervisor, the one process that outlives every worker."""

    def _loop() -> None:
        while True:
            try:
                sweep()
            except Exception:  # noqa: BLE001 (a failed sweep must not kill the app)
                logger.exception("Session janitor sweep failed")
            time.sleep(max(interval, 5.0))

    thread = threading.Thread(target=_loop, name="forage-session-janitor", daemon=True)
    thread.start()
