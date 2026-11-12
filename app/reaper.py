"""Kill processes orphaned inside the container.

A uvicorn worker that dies (OOM kill, crash, restart) leaves its Playwright
driver and the Chromium trees it started behind. Those become children of the
container's PID 1 (docker-init), which only reaps *dead* children: a live
orphan keeps running forever, holding task slots and memory, and nobody has a
handle left to call close() on it.

This sweeps them: every process whose parent is PID 1 and which is not part of
the supervision tree we are running under is killed, children first.

Safe by construction: a browser that belongs to a live worker is a descendant
of that worker (uvicorn -> worker -> node driver -> chromium), never a direct
child of PID 1. Only the uvicorn supervisor (and the container init) legitimately
sits under PID 1, and both are excluded explicitly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import threading
import time
from typing import Dict, Iterable, List, Set

from . import metrics

logger = logging.getLogger(__name__)

# The uvicorn supervisor runs this very module; never kill it.
_SUPERVISOR_MARKERS = ("app.main", "uvicorn")

# Chromium's crashpad handler double-forks itself onto PID 1 on purpose: it is
# an orphan by design, tiny, and it exits on its own when its browser dies.
# Killing it only makes Chromium restart it on the next navigation.
_SKIP_COMM = ("chrome_crashpad", "chrome_crashpad_handler")

# The X display is shared by every worker and is started by the supervisor, so
# it is never an orphan we may kill. Killing it would take headful search down
# for the workers that are still alive.
_SKIP_CMDLINE = ("Xvfb",)


def _read_stat(pid: int) -> tuple[str, int] | None:
    try:
        raw = open("/proc/%d/stat" % pid).read()
    except OSError:
        return None
    # comm can contain spaces and parentheses: split on the LAST ')'.
    comm = raw.split("(", 1)[1].rsplit(")", 1)[0]
    rest = raw.rsplit(")", 1)[1].split()
    try:
        ppid = int(rest[1])
    except (IndexError, ValueError):
        return None
    return comm, ppid


def _cmdline(pid: int) -> str:
    try:
        return open("/proc/%d/cmdline" % pid).read().replace("\x00", " ")
    except OSError:
        return ""


_CRASHPAD_CLAIM = re.compile(r"--crashpad-handler-pid=(\d+)")


def claimed_crashpads() -> Set[int]:
    """Crashpad handler PIDs that a living Chromium still points at.

    A crashpad handler is an orphan on purpose (it double-forks onto PID 1), but
    it belongs to a browser: every Chromium is launched with
    ``--crashpad-handler-pid=<pid>``. A handler no live browser claims is a real
    orphan and has to die; a claimed one is doing its job.
    """
    claimed: Set[int] = set()
    try:
        entries = [int(e) for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return claimed
    for pid in entries:
        stat = _read_stat(pid)
        if stat is None or not stat[0].startswith("chrome"):
            continue
        for match in _CRASHPAD_CLAIM.finditer(_cmdline(pid)):
            claimed.add(int(match.group(1)))
    return claimed


def _protected(pid: int, claimed: Set[int]) -> bool:
    """Processes that must survive a sweep, whatever their parent is."""
    if pid in (0, 1, os.getpid(), os.getppid()):
        return True
    stat = _read_stat(pid)
    if stat is not None and stat[0].startswith(_SKIP_COMM):
        # A crashpad handler lives under PID 1 by design. Spare it only while a
        # live browser claims it; otherwise it is a leftover from a dead browser
        # and gets killed like any other orphan.
        return pid in claimed
    cmd = _cmdline(pid)
    if not cmd:
        return False
    lowered = cmd.lower()
    if any(marker in lowered for marker in _SUPERVISOR_MARKERS):
        return True
    return any(needle in cmd for needle in _SKIP_CMDLINE)


def find_orphans(claimed: Set[int] | None = None) -> List[int]:
    """PIDs whose parent is PID 1 and that are not our supervision tree."""
    if claimed is None:
        claimed = claimed_crashpads()
    try:
        entries = [int(e) for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return []
    orphans = []
    for pid in entries:
        if _protected(pid, claimed):
            continue
        stat = _read_stat(pid)
        if stat is None:
            continue
        _, ppid = stat
        if ppid == 1:
            orphans.append(pid)
    return orphans


def _children_map(pids: Iterable[int]) -> Dict[int, List[int]]:
    children: Dict[int, List[int]] = {}
    for pid in pids:
        stat = _read_stat(pid)
        if stat is None:
            continue
        children.setdefault(stat[1], []).append(pid)
    return children


def _subtree(root: int, children: Dict[int, List[int]]) -> List[int]:
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


def kill_orphans() -> List[dict]:
    """SIGKILL every orphan tree. Returns what was killed, for logging/tests."""
    claimed = claimed_crashpads()
    orphans = find_orphans(claimed)
    if not orphans:
        # A sweep that finds nothing still ran: counting it matters, otherwise
        # the metric can only ever report sweeps that killed something.
        metrics.observe_reaper(sweeps=1)
        return []
    try:
        all_pids = [int(e) for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        all_pids = list(orphans)
    children = _children_map(all_pids)

    killed: List[dict] = []
    # Deepest first: a parent that dies takes its children down with it, and a
    # child reparented mid-sweep would need a second pass otherwise.
    order = []
    for root in orphans:
        order.extend(_subtree(root, children))
    for pid in reversed(order):
        if pid in (0, 1, os.getpid()):
            continue
        if _protected(pid, claimed):
            continue
        stat = _read_stat(pid)
        comm = stat[0] if stat else "?"
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append({"pid": pid, "comm": comm})
        except ProcessLookupError:
            continue
        except OSError as exc:
            logger.warning("Orphan sweep: could not kill %d (%s): %s", pid, comm, exc)
    metrics.observe_reaper(kills=len(killed))
    if killed:
        logger.warning(
            "Orphan sweep killed %d process(es): %s",
            len(killed),
            ", ".join("%d/%s" % (k["pid"], k["comm"]) for k in killed[:12]),
        )
    return killed


async def orphan_reaper(interval: float = 30.0) -> None:
    """Sweep once at startup, then forever at `interval` seconds."""
    while True:
        try:
            kill_orphans()
        except Exception:  # noqa: BLE001 (a failed sweep must not kill the app)
            logger.exception("Orphan sweep failed")
        await asyncio.sleep(max(interval, 5.0))


def start_reaper_thread(interval: float = 30.0) -> None:
    """Run the sweep from the supervisor process, in a daemon thread.

    The supervisor outlives every worker, so it also covers the window where a
    worker died and uvicorn has not respawned it yet. Daemon: it dies with the
    process and never keeps the container alive.
    """

    def _loop() -> None:
        while True:
            try:
                kill_orphans()
            except Exception:  # noqa: BLE001
                logger.exception("Orphan sweep failed")
            time.sleep(max(interval, 5.0))

    thread = threading.Thread(target=_loop, name="forage-orphan-reaper", daemon=True)
    thread.start()
