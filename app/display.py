"""Virtual display (Xvfb) management.

A headful Chromium needs an X display. The container ships Xvfb (Playwright's
system dependencies install it), but nothing starts it: Dockerfile images have
no init and no X session. This module starts a display on demand, at process
startup, when the configuration asks for a headful browser.

Only used when a headful browser is configured (``browser.search.headless:
false`` or ``browser.headless: false``). If DISPLAY is already set by the
environment, nothing is started: an external X server (or an operator running
the container with -e DISPLAY) wins.

Notes learned the hard way:

- Xvfb leaves ``/tmp/.X<n>-lock`` behind when it is killed (container restart).
  The next start then fails with "Server is already active for display n".
  A stale lock (no Xvfb process behind it) is removed before starting again.
- ``xvfb-run`` is NOT used: it requires ``xauth``, which is not installed in
  the image, and it wraps a single command instead of a long running server.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)

SCREEN = "1366x768x24"
FIRST_DISPLAY = 99
LAST_DISPLAY = 109


def _xvfb_running(display: int) -> bool:
    """True when a live Xvfb process holds this display number."""
    marker = f":{display}"
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                cmdline = handle.read().decode("utf-8", errors="ignore")
        except OSError:
            continue
        if "Xvfb" in cmdline and marker in cmdline:
            return True
    return False


def _clean_stale(display: int) -> None:
    """Remove the lock/socket left behind by a dead Xvfb."""
    for path in (f"/tmp/.X{display}-lock", f"/tmp/.X11-unix/X{display}"):
        try:
            os.unlink(path)
            logger.warning("Removed stale X lock %s", path)
        except FileNotFoundError:
            pass
        except OSError as exc:  # noqa: BLE001
            logger.warning("Could not remove %s: %s", path, exc)


def _try_start(display: int) -> Optional[subprocess.Popen]:
    """Start Xvfb on ``display`` and return the process, or None on failure."""
    if os.path.exists(f"/tmp/.X{display}-lock") and not _xvfb_running(display):
        _clean_stale(display)
    try:
        proc = subprocess.Popen(
            ["Xvfb", f":{display}", "-screen", "0", SCREEN, "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        logger.error("Xvfb not found in PATH: cannot start a headful browser")
        return None
    deadline = time.monotonic() + 5
    socket = f"/tmp/.X11-unix/X{display}"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return None  # died on startup (display in use)
        if os.path.exists(socket):
            return proc
        time.sleep(0.1)
    proc.terminate()
    return None


def _find_running_xvfb() -> Optional[int]:
    """Display number of an Xvfb already running, if any.

    An X display serves multiple clients, so a second uvicorn worker (or a
    restarted app) reuses it instead of starting another server.
    """
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                parts = handle.read().decode("utf-8", errors="ignore").split("\0")
        except OSError:
            continue
        if not any("Xvfb" in part for part in parts):
            continue
        for part in parts:
            if part.startswith(":"):
                try:
                    return int(part[1:].split(".")[0])
                except ValueError:
                    continue
    return None


def ensure_display() -> Optional[subprocess.Popen]:
    """Make sure a usable X display exists for a headful browser.

    Returns the Xvfb process that was started (to terminate on shutdown) or
    None when an existing display is reused (DISPLAY already set, or an Xvfb
    left running by another worker).
    """
    if os.environ.get("DISPLAY"):
        logger.info("DISPLAY already set to %s: using the existing display", os.environ["DISPLAY"])
        return None

    running = _find_running_xvfb()
    if running is not None:
        os.environ["DISPLAY"] = f":{running}"
        logger.info("Reusing the Xvfb already running on DISPLAY=:%d", running)
        return None

    for display in range(FIRST_DISPLAY, LAST_DISPLAY + 1):
        proc = _try_start(display)
        if proc is not None:
            os.environ["DISPLAY"] = f":{display}"
            logger.info("Xvfb started on DISPLAY=:%d (headful browser)", display)
            return proc
    logger.error("Could not start Xvfb on any display from %d to %d", FIRST_DISPLAY, LAST_DISPLAY)
    return None


def stop_display(proc: Optional[subprocess.Popen]) -> None:
    """Terminate an Xvfb process started by ensure_display()."""
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
