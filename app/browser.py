"""In-process Playwright browser pool for Forage.

Config-driven: browser.min_idle, browser.max_instances, browser.idle_timeout.
Browsers are Chromium instances launched by Playwright inside the container
(no external CDP). The pool keeps idle instances warm and reaps them after
idle_timeout, down to min_idle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Deque, Optional

logger = logging.getLogger(__name__)

# Chrome desktop UA for the browser context (a bot UA would be a giveaway).
DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Hides headless/automation signals from basic anti-bot (Cloudflare etc.).
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR', 'pt', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""


class BrowserPool:
    def __init__(self, browser_config: Any, user_agent: Optional[str] = None) -> None:
        self.engine = browser_config.engine
        self.min_idle = browser_config.min_idle
        self.max_instances = browser_config.max_instances
        self.idle_timeout = browser_config.idle_timeout
        self.launch_timeout = browser_config.launch_timeout
        self.network_idle_timeout = browser_config.network_idle_timeout
        self.scroll_steps = browser_config.scroll_steps
        self.headless = browser_config.headless
        self.stealth = browser_config.stealth
        # Explicit browser UA wins; otherwise fall back to a real Chrome UA
        # (a bot UA would be a giveaway against anti-bot systems).
        self.user_agent = user_agent or DEFAULT_BROWSER_UA
        self._idle: Deque[tuple[float, Any]] = deque()  # (last_used, browser)
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._pw: Optional[Any] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._started = False

    async def start(self) -> None:
        """Launch the playwright driver and warm min_idle browsers."""
        if self.max_instances < 1:
            logger.warning("Browser disabled (max_instances=0)")
            return
        if self.engine == "patchright":
            from patchright.async_api import async_playwright
        else:
            from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._semaphore = asyncio.Semaphore(self.max_instances)
        # Warm the pool to min_idle (release after launch so they sit idle).
        for _ in range(self.min_idle):
            browser = await self._launch_new()
            if browser is not None:
                self._idle.append((time.monotonic(), browser))
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._started = True
        logger.info("Browser pool ready: %d idle, max %d", len(self._idle), self.max_instances)

    async def stop(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        while self._idle:
            _, browser = self._idle.popleft()
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
        if self._pw is not None:
            await self._pw.stop()
        self._started = False

    async def _launch_new(self) -> Optional[Any]:
        try:
            launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
            if self.stealth:
                launch_args.append("--disable-blink-features=AutomationControlled")
            browser = await self._pw.chromium.launch(
                headless=self.headless,
                timeout=self.launch_timeout * 1000,
                args=launch_args,
            )
            return browser
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to launch Chromium: %s", exc)
            return None

    async def acquire(self) -> Any:
        """Get a browser instance (idle or new), respecting max_instances."""
        if not self._started or self._semaphore is None:
            raise RuntimeError("Browser pool not started or disabled")
        await self._semaphore.acquire()
        try:
            while self._idle:
                ts, browser = self._idle.popleft()
                if browser.is_connected():
                    return browser
            browser = await self._launch_new()
            if browser is None:
                self._semaphore.release()
                raise RuntimeError("Could not launch Chromium")
            return browser
        except Exception:
            self._semaphore.release()
            raise

    def release(self, browser: Any) -> None:
        """Return a browser to the idle pool."""
        try:
            if browser.is_connected():
                self._idle.append((time.monotonic(), browser))
        finally:
            self._semaphore.release()

    async def render(
        self,
        url: str,
        wait_for: Optional[str] = None,
        timeout: int = 30,
    ) -> str:
        """Render a URL with Chromium and return the final DOM HTML."""
        browser = await self.acquire()
        page = None
        try:
            context = await browser.new_context(user_agent=self.user_agent)
            page = await context.new_page()
            if self.stealth:
                await page.add_init_script(STEALTH_INIT_SCRIPT)
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout * 1000,
            )
            if wait_for:
                await page.wait_for_selector(wait_for, timeout=timeout * 1000)
            else:
                try:
                    await page.wait_for_load_state(
                        "networkidle",
                        timeout=self.network_idle_timeout * 1000,
                    )
                except Exception:  # noqa: BLE001 (networkidle is best-effort)
                    pass
            if self.scroll_steps > 0:
                await self._scroll_to_bottom(page)
            html = await page.content()
            return html
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
            self.release(browser)

    async def _scroll_to_bottom(self, page: Any) -> None:
        """Scroll the page to the bottom to trigger lazy-loaded content.

        Comment-heavy sites (YouTube, Reddit) mount comments only when they
        scroll into view (IntersectionObserver). A single jump to the bottom
        skips those observers, and even viewport-by-viewport jumps can miss
        them. YouTube in particular only mounts comments during a *smooth*
        (animated) scroll. So we animate to the bottom, let the animation and
        lazy requests settle, and repeat up to ``scroll_steps`` rounds (pages
        that grow while scrolling), stopping early when the height stops
        growing. A short networkidle wait lets the requests finish.
        """
        last_height = -1
        for _ in range(self.scroll_steps):
            await page.evaluate(
                "window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'})"
            )
            await page.wait_for_timeout(1500)
            height = await page.evaluate("document.body.scrollHeight")
            if height <= last_height:
                break
            last_height = height
        try:
            await page.wait_for_load_state("networkidle", timeout=2000)
        except Exception:  # noqa: BLE001 (best-effort)
            pass

    async def _cleanup_loop(self) -> None:
        """Reap idle browsers past idle_timeout (keeping min_idle warm)."""
        while True:
            await asyncio.sleep(15)
            if not self._started:
                return
            now = time.monotonic()
            to_close = []
            keep = self.min_idle
            while self._idle:
                ts, browser = self._idle[0]
                if len(self._idle) > keep and now - ts > self.idle_timeout:
                    self._idle.popleft()
                    to_close.append(browser)
                else:
                    break
            for browser in to_close:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
            if to_close:
                logger.info("Browser pool: closed %d idle instances", len(to_close))
