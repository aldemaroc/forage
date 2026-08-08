"""In-process Playwright browser pool for Forage.

Config-driven: browser.min_idle, browser.max_instances, browser.idle_timeout.
Browsers are Chromium instances launched by Playwright inside the container
(no external CDP). The pool keeps idle instances warm and reaps them after
idle_timeout, down to min_idle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Union

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

# Titles that indicate an anti-bot challenge page (Cloudflare Turnstile etc.).
# Used to wait for non-interactive challenges to auto-resolve after load.
CHALLENGE_TITLES = (
    "just a moment",
    "attention required",
    "checking your browser",
    "verifying you are human",
)

# Mozilla Readability.js (https://github.com/mozilla/readability), vendored at
# build time. Runs inside the page (page.evaluate) to extract the article DOM
# from the rendered page, mimicking what Jina Reader does for product pages
# where trafilatura's main-content heuristic drops the buybox (Amazon).
_READABILITY_JS = (pathlib.Path(__file__).parent / "readability.js").read_text(
    encoding="utf-8"
)


def _readability_eval() -> str:
    """Return a JS IIFE that parses the current document and returns a JSON
    string with the article title and HTML (or null when Readability finds no
    article)."""
    return (
        "(function() {\n"
        + _READABILITY_JS
        + "\nvar _r = new Readability(document).parse();"
        "\nreturn _r ? JSON.stringify({title: _r.title || '', content: _r.content}) : null;\n})()"
    )


def _parse_readability(raw: Optional[str]) -> Optional[dict]:
    """Parse the JSON returned by the Readability IIFE into {title, content}."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict) or not parsed.get("content"):
        return None
    return {"title": parsed.get("title") or "", "content": parsed["content"]}


class BrowserPool:
    """In-process browser pool for Forage.

    Supports four engines:
      - playwright (default): vanilla Playwright Chromium with light stealth
      - patchright: undetected Playwright fork (same API)
      - scrapling: StealthyFetcher from the Scrapling framework, which
        impersonates real browser fingerprints and can solve Cloudflare
        Turnstile/Interstitial out of the box. A single AsyncStealthySession
        is kept alive and its internal tab pool (max_pages) handles
        concurrency; networkidle and scrolling are replicated inside a
        page_action so behaviour matches the Playwright path.
      - obscura: external CDP server (the Obscura Rust/V8 browser). The
        pool connects via Playwright connect_over_cdp to browser.cdp_url
        instead of launching Chromium locally. Concurrency is capped by
        max_instances (the CDP server owns the actual browser processes).
    """

    def __init__(self, browser_config: Any, user_agent: Optional[str] = None) -> None:
        self.engine = browser_config.engine
        self.cdp_url = getattr(browser_config, "cdp_url", "")  # engine=obscura
        self.min_idle = browser_config.min_idle
        self.max_instances = browser_config.max_instances
        self.idle_timeout = browser_config.idle_timeout
        self.launch_timeout = browser_config.launch_timeout
        self.network_idle_timeout = browser_config.network_idle_timeout
        self.scroll_steps = browser_config.scroll_steps
        self.challenge_timeout = browser_config.challenge_timeout
        self.solve_cloudflare = browser_config.solve_cloudflare
        self.fallback_solver = browser_config.fallback_solver
        self.headless = browser_config.headless
        self.stealth = browser_config.stealth
        # Explicit browser UA wins; otherwise fall back to a real Chrome UA
        # (a bot UA would be a giveaway against anti-bot systems).
        self.user_agent = user_agent or DEFAULT_BROWSER_UA
        self._idle: Deque[tuple[float, Any]] = deque()  # (last_used, browser)
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._pw: Optional[Any] = None
        self._cdp_browser: Optional[Any] = None  # engine=obscura: connected CDP browser
        self._scrapling_session: Optional[Any] = None
        self._solver_session: Optional[Any] = None  # lazy; scrapling session with solve_cloudflare=True (last-resort retry)
        self._solver_lock = asyncio.Lock()  # guards lazy solver session creation (any engine)
        self._cleanup_task: Optional[asyncio.Task] = None
        self._started = False

    async def start(self) -> None:
        """Launch the engine and warm the pool."""
        if self.max_instances < 1:
            logger.warning("Browser disabled (max_instances=0)")
            return
        if self.engine == "scrapling":
            from scrapling.fetchers import AsyncStealthySession

            # network_idle is handled inside page_action (capped), so the
            # session itself does not wait for it (streaming pages would
            # otherwise hit the full fetch timeout). solve_cloudflare is
            # configurable: the built-in solver costs ~5s per page (it waits
            # for networkidle before detecting), while our page_action polls
            # the title and resolves non-interactive challenges for free.
            self._scrapling_session = AsyncStealthySession(
                headless=self.headless,
                network_idle=False,
                timeout=self.launch_timeout * 1000,
                max_pages=max(1, self.max_instances),
                solve_cloudflare=self.solve_cloudflare,
                useragent=self.user_agent,
            )
            await self._scrapling_session.start()
            self._semaphore = asyncio.Semaphore(self.max_instances)
            self._started = True
            logger.info("Scrapling session ready (max_pages=%d)", self.max_instances)
            return
        if self.engine == "obscura":
            # External CDP server (Obscura). Connect once; the server owns
            # the browser processes. Concurrency is capped client-side.
            from playwright.async_api import async_playwright

            if not self.cdp_url:
                raise RuntimeError("browser.cdp_url is required for engine=obscura")
            self._pw = await async_playwright().start()
            self._cdp_browser = await self._pw.chromium.connect_over_cdp(
                endpoint_url=self.cdp_url,
                timeout=self.launch_timeout * 1000,
            )
            self._semaphore = asyncio.Semaphore(self.max_instances)
            self._started = True
            logger.info("Obscura CDP connected: %s", self.cdp_url)
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
        if self._cdp_browser is not None:
            try:
                await self._cdp_browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._cdp_browser = None
        if self._scrapling_session is not None:
            try:
                await self._scrapling_session.close()
            except Exception:  # noqa: BLE001
                pass
            self._scrapling_session = None
        if self._solver_session is not None:
            try:
                await self._solver_session.close()
            except Exception:  # noqa: BLE001
                pass
            self._solver_session = None
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
        scroll_steps: Optional[int] = None,
        network_idle_timeout: Optional[int] = None,
        challenge_timeout: Optional[int] = None,
        readability: bool = False,
    ) -> Union[str, Dict[str, str]]:
        """Render a URL with the configured engine and return the final DOM HTML.

        Per-call overrides (used by domain_overrides): ``scroll_steps``,
        ``network_idle_timeout`` and ``challenge_timeout``; None falls back
        to the global browser config.

        When ``readability`` is True, the page is parsed in-browser with
        Mozilla Readability.js and ``{"title", "content"}`` (article HTML) is
        returned instead of the full DOM (falls back to the full DOM string
        when no article is found).
        """
        steps = self.scroll_steps if scroll_steps is None else scroll_steps
        idle_cap = self.network_idle_timeout if network_idle_timeout is None else network_idle_timeout
        chal_cap = self.challenge_timeout if challenge_timeout is None else challenge_timeout
        if self.engine == "scrapling":
            return await self._scrapling_render(url, wait_for, timeout, steps, idle_cap, chal_cap, readability)
        if self.engine == "obscura":
            return await self._cdp_render(url, wait_for, timeout, steps, idle_cap, readability)
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
                        timeout=idle_cap * 1000,
                    )
                except Exception:  # noqa: BLE001 (networkidle is best-effort)
                    pass
            if steps > 0:
                await self._scroll_to_bottom(page, steps)
            if readability:
                article = _parse_readability(await page.evaluate(_readability_eval()))
                if article:
                    return article
            html = await page.content()
            return html
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
            self.release(browser)

    async def _cdp_render(
        self,
        url: str,
        wait_for: Optional[str] = None,
        timeout: int = 30,
        scroll_steps: int = 0,
        network_idle_timeout: Optional[int] = None,
        readability: bool = False,
    ) -> Union[str, Dict[str, str]]:
        """Render via an external Obscura CDP server (engine=obscura).

        The CDP server owns the browser processes; each request opens a fresh
        page on the shared connected browser. Same load-wait ladder as the
        Playwright path: domcontentloaded -> optional selector -> capped
        networkidle (best-effort) -> optional scroll -> page content.
        """
        if not self._started or self._cdp_browser is None or self._semaphore is None:
            raise RuntimeError("Obscura CDP not connected or disabled")
        idle_cap = self.network_idle_timeout if network_idle_timeout is None else network_idle_timeout

        await self._semaphore.acquire()
        page = None
        context = None
        try:
            context = await self._cdp_browser.new_context(
                user_agent=self.user_agent,
                viewport={"width": 1280, "height": 800},
            )
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
                        timeout=idle_cap * 1000,
                    )
                except Exception:  # noqa: BLE001 (networkidle is best-effort)
                    pass
            if scroll_steps > 0:
                await self._scroll_to_bottom(page, scroll_steps)
            if readability:
                article = _parse_readability(await page.evaluate(_readability_eval()))
                if article:
                    return article
            html = await page.content()
            return html
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
            if context is not None:
                try:
                    await context.close()
                except Exception:  # noqa: BLE001
                    pass
            self._semaphore.release()

    async def _scrapling_render(
        self,
        url: str,
        wait_for: Optional[str] = None,
        timeout: int = 30,
        scroll_steps: int = 0,
        network_idle_timeout: Optional[int] = None,
        challenge_timeout: Optional[int] = None,
        readability: bool = False,
    ) -> Union[str, Dict[str, str]]:
        """Render via Scrapling StealthyFetcher (single shared session).

        networkidle and scrolling are replicated inside a page_action so the
        behaviour matches the Playwright path: domcontentloaded -> capped
        networkidle wait (best-effort) -> optional scroll -> page content.
        """
        if not self._started or self._scrapling_session is None or self._semaphore is None:
            raise RuntimeError("Scrapling session not started or disabled")
        idle_cap = self.network_idle_timeout if network_idle_timeout is None else network_idle_timeout
        chal_cap = self.challenge_timeout if challenge_timeout is None else challenge_timeout

        result: dict = {}

        async def _page_action(page: Any) -> None:
            # Cloudflare Turnstile non-interactive challenges auto-validate a
            # few seconds after load. The StealthyFetcher's solver runs before
            # page_action and can miss a challenge that is still booting, so
            # wait here until the challenge title disappears. Polling the title
            # is cheap and never hangs streaming pages (they have no challenge).
            for _ in range(max(1, chal_cap)):
                title = (await page.title()).lower()
                if not any(c in title for c in CHALLENGE_TITLES):
                    break
                await page.wait_for_timeout(1000)
            if idle_cap > 0:
                try:
                    await page.wait_for_load_state(
                        "networkidle",
                        timeout=idle_cap * 1000,
                    )
                except Exception:  # noqa: BLE001 (networkidle is best-effort)
                    pass
            if scroll_steps > 0:
                await self._scroll_to_bottom(page, scroll_steps)
            if readability:
                try:
                    result["article"] = _parse_readability(
                        await page.evaluate(_readability_eval())
                    )
                except Exception:  # noqa: BLE001
                    result["article"] = None

        await self._semaphore.acquire()
        try:
            resp = await self._scrapling_session.fetch(
                url,
                timeout=timeout * 1000,
                wait_selector=wait_for or None,
                page_action=_page_action,
            )
            if readability and result.get("article"):
                return result["article"]
            if resp is None or not resp.body:
                raise RuntimeError(f"Empty response for {url}")
            return resp.body.decode("utf-8", errors="ignore")
        finally:
            self._semaphore.release()

    async def _get_solver_session(self) -> Any:
        """Lazily create the scrapling session with the built-in Cloudflare
        solver enabled (last-resort retry for anti-bot failures)."""
        # Check INSIDE the lock: a concurrent retry may have assigned the
        # session but not finished start() yet; returning it early would call
        # fetch() on a session that is not alive ("Context manager has been
        # closed").
        async with self._solver_lock:
            if self._solver_session is None:
                from scrapling.fetchers import AsyncStealthySession

                self._solver_session = AsyncStealthySession(
                    headless=self.headless,
                    network_idle=False,
                    timeout=self.launch_timeout * 1000,
                    max_pages=max(1, self.max_instances),
                    solve_cloudflare=True,
                    useragent=self.user_agent,
                )
                await self._solver_session.start()
                logger.info("Scrapling solver session ready (last-resort anti-bot retry)")
        return self._solver_session

    async def render_with_solver(
        self,
        url: str,
        wait_for: Optional[str] = None,
        timeout: int = 30,
        scroll_steps: Optional[int] = None,
        network_idle_timeout: Optional[int] = None,
    ) -> str:
        """Render with the scrapling session that has solve_cloudflare=True.

        Used as a last-resort retry when the configured engine hits an
        anti-bot challenge. The built-in solver can handle interactive
        challenges that the page_action poll cannot.
        """
        if not self._started or self._semaphore is None:
            raise RuntimeError("Browser pool not started or disabled")
        session = await self._get_solver_session()
        steps = self.scroll_steps if scroll_steps is None else scroll_steps
        idle_cap = self.network_idle_timeout if network_idle_timeout is None else network_idle_timeout

        async def _page_action(page: Any) -> None:
            if idle_cap > 0:
                try:
                    await page.wait_for_load_state(
                        "networkidle",
                        timeout=idle_cap * 1000,
                    )
                except Exception:  # noqa: BLE001 (networkidle is best-effort)
                    pass
            if steps > 0:
                await self._scroll_to_bottom(page, steps)

        await self._semaphore.acquire()
        try:
            resp = await session.fetch(
                url,
                timeout=timeout * 1000,
                wait_selector=wait_for or None,
                page_action=_page_action,
            )
            if resp is None or not resp.body:
                raise RuntimeError(f"Empty response for {url}")
            return resp.body.decode("utf-8", errors="ignore")
        finally:
            self._semaphore.release()

    async def _scroll_to_bottom(self, page: Any, steps: int = 0) -> None:
        """Scroll the page to the bottom to trigger lazy-loaded content.

        Comment-heavy sites (YouTube, Reddit) mount comments only when they
        scroll into view (IntersectionObserver). A single jump to the bottom
        skips those observers, and even viewport-by-viewport jumps can miss
        them. YouTube in particular only mounts comments during a *smooth*
        (animated) scroll. So we animate to the bottom, let the animation and
        lazy requests settle, and repeat up to ``steps`` rounds (pages that
        grow while scrolling), stopping early when the height stops growing.
        A short networkidle wait lets the requests finish.
        """
        last_height = -1
        for _ in range(steps):
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
