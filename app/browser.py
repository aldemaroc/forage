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
import os
import pathlib
import random as _random
import time

from . import metrics
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


def _is_dead_browser(exc: BaseException) -> bool:
    """True when the scrapling browser died (session must be recreated)."""
    msg = str(exc).lower()
    return (
        "has been closed" in msg
        or "target page" in msg
        or "browser has been closed" in msg
        or "is closed" in msg
    )


class RenderTimeout(RuntimeError):
    """A browser render exceeded its hard wall-clock cap.

    Raised instead of waiting forever: the caller turns it into a fast error
    item, so the HTTP client gets an answer (and the semaphore is released)
    even when the underlying browser stopped responding.
    """


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
        self.cdp_url = getattr(browser_config, "cdp_url", "")  # engine=obscura / chrome-local
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
        # A long-lived scrapling session degrades silently (see
        # _recycle_scrapling_session), so it is recycled on a budget and every
        # fetch runs under a hard wall-clock cap.
        self.render_timeout_grace = getattr(browser_config, "render_timeout_grace", 20)
        self.session_max_renders = getattr(browser_config, "session_max_renders", 200)
        self.session_max_age = getattr(browser_config, "session_max_age", 21600)
        self.close_pages_when_idle = getattr(browser_config, "close_pages_when_idle", True)
        self.session_idle_timeout = getattr(browser_config, "session_idle_timeout", 120)
        self._idle_close_at = 0.0
        self._idle_watch_task: Optional[asyncio.Task] = None
        self._scrapling_renders = 0
        self._scrapling_started = time.monotonic()
        self._inflight = 0  # renders currently using the shared session
        self._recycle_pending = False  # recycle requested while busy
        self._idle: Deque[tuple[float, Any]] = deque()  # (last_used, browser)
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._pw: Optional[Any] = None
        self._cdp_browser: Optional[Any] = None  # engine=obscura: connected CDP browser
        self._scrapling_session: Optional[Any] = None
        self._solver_session: Optional[Any] = None  # lazy; scrapling session with solve_cloudflare=True (last-resort retry)
        self._solver_lock = asyncio.Lock()  # guards lazy solver session creation (any engine)
        self._session_lock = asyncio.Lock()  # guards scrapling session recreation when it dies
        self._cleanup_task: Optional[asyncio.Task] = None
        self._started = False

    async def start(self) -> None:
        """Launch the engine and warm the pool."""
        if self.max_instances < 1:
            logger.warning("Browser disabled (max_instances=0)")
            return
        if self.engine == "scrapling":
            # Lazy on purpose: a browser that is up at boot costs ~450 MB of
            # Chromium + driver with nothing to render, and every uvicorn worker
            # pays it. The session is created on the first render and dropped
            # again by the idle watchdog.
            self._scrapling_session = None
            self._scrapling_started = time.monotonic()
            self._scrapling_renders = 0
            self._semaphore = asyncio.Semaphore(self.max_instances)
            self._started = True
            logger.info("Scrapling session ready (max_pages=%d)", self.max_instances)
            return
        if self.engine == "obscura" or self.engine == "chrome-local":
            # External CDP server (Obscura or the local desktop Chrome for
            # Hermes on 9222). Connect once; the server owns the browser
            # processes. Concurrency is capped client-side.
            from playwright.async_api import async_playwright

            if not self.cdp_url:
                raise RuntimeError("browser.cdp_url is required for this engine")
            self._pw = await async_playwright().start()
            self._cdp_browser = await self._pw.chromium.connect_over_cdp(
                endpoint_url=self.cdp_url,
                timeout=self.launch_timeout * 1000,
            )
            self._semaphore = asyncio.Semaphore(self.max_instances)
            self._started = True
            logger.info("CDP connected: %s (engine=%s)", self.cdp_url, self.engine)
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
        metrics.set_session_open("scrapling", False)
        metrics.observe_session_close("scrapling", "shutdown")

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
        """Render a URL, recording the outcome for ``/metrics``."""
        started = time.monotonic()
        try:
            html = await self._render(
                url,
                wait_for,
                timeout,
                scroll_steps,
                network_idle_timeout,
                challenge_timeout,
                readability,
            )
        except BaseException:
            metrics.observe_render(self.engine, "error", time.monotonic() - started)
            raise
        metrics.observe_render(self.engine, "ok", time.monotonic() - started)
        return html

    async def _render(
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
        if self.engine in ("obscura", "chrome-local"):
            return await self._cdp_render(url, wait_for, timeout, steps, idle_cap, readability)
        browser = await self.acquire()
        page = None
        context = None
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
            # Close the page AND the context. Closing only the page leaves the
            # context (its storage partition and network context) behind, one
            # per render, for as long as the pool browser lives.
            if page is not None:
                await _quiet_close(page.close())
            if context is not None:
                await _quiet_close(context.close())
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
            if self.engine == "chrome-local":
                # The local desktop Chrome is already running with its real
                # profile (cookies, trust, no incognito). Playwright's
                # new_context() would create an INCORGNITO context that Google
                # treats as automation. Use the browser's existing default
                # context (the real Chrome profile) instead.
                if not self._cdp_browser.contexts:
                    raise RuntimeError("chrome-local: no existing browser context found")
                context = self._cdp_browser.contexts[0]
            else:
                context = await self._cdp_browser.new_context(
                    user_agent=self.user_agent,
                    viewport={"width": 1280, "height": 800},
                )
            page = await context.new_page()
            # engine=chrome-local: the desktop Chrome is already a real
            # browser; injecting the stealth init script (navigator.webdriver
            # override etc.) is exactly the automation fingerprint Google
            # detects. Only apply stealth to engines that are actually headless.
            if self.stealth and self.engine != "chrome-local":
                await page.add_init_script(STEALTH_INIT_SCRIPT)
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout * 1000,
            )
            if self.engine == "chrome-local":
                # The Google SERP sometimes resolves its challenge a moment
                # after domcontentloaded. A fixed 3s settle wait beats
                # networkidle, which on a challenge page returns instantly
                # (challenge pages fire no background requests). Validated by
                # the isolated playwright test: h3=10 with a 3s wait.
                await page.wait_for_timeout(3000)
            elif wait_for:
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
            # engine=chrome-local: never close the shared real-profile context
            # (it belongs to the desktop Chrome; only the page is ours).
            if context is not None and self.engine != "chrome-local":
                try:
                    await context.close()
                except Exception:  # noqa: BLE001
                    pass
            self._semaphore.release()

    async def _restart_scrapling_session(self, reason: str = "browser death") -> None:
        """Recreate the scrapling session after its browser died.

        Under heavy load the StealthyFetcher's internal browser can be closed
        (crash / idle / resource pressure); every subsequent fetch then fails
        with "Target page, context or browser has been closed". Recreate the
        session once (serialized) so the pool heals itself.
        """
        async with self._session_lock:
            if self._scrapling_session is not None:
                try:
                    await self._scrapling_session.close()
                except Exception:  # noqa: BLE001
                    pass
                self._scrapling_session = None
            from scrapling.fetchers import AsyncStealthySession

            launch_started = time.monotonic()
            self._scrapling_session = AsyncStealthySession(
                headless=self.headless,
                network_idle=False,
                timeout=self.launch_timeout * 1000,
                max_pages=max(1, self.max_instances),
                solve_cloudflare=self.solve_cloudflare,
                useragent=self.user_agent,
            )
            try:
                await self._scrapling_session.start()
            except BaseException:
                metrics.observe_launch("scrapling", "error", time.monotonic() - launch_started)
                raise
            metrics.observe_launch("scrapling", "ok", time.monotonic() - launch_started)
            metrics.set_session_open("scrapling", True)
            self._scrapling_started = time.monotonic()
            self._scrapling_renders = 0
            logger.warning("Scrapling session recreated (%s)", reason)

    async def _recycle_scrapling_session(self, reason: str) -> None:
        """Recycle the shared scrapling session without breaking in-flight work.

        A session that has served a lot of pages stops answering: fetch() never
        returns, which hangs the request behind it and keeps its semaphore slot
        forever. There is no pool-level health signal for that, so the session
        is recycled on a render/time budget and whenever a render blows the
        hard cap. Closing it while other renders are using it would fail those
        requests, so the recycle is deferred to the next idle render.
        """
        if self._inflight > 1:
            self._recycle_pending = True
            logger.info(
                "Scrapling session recycle deferred (%d renders in flight): %s",
                self._inflight,
                reason,
            )
            return
        await self._restart_scrapling_session(reason)
        metrics.observe_session_close("scrapling", "budget")

    async def _reset_solver_session(self, reason: str) -> None:
        """Drop the lazy solver session so the next retry builds a fresh one."""
        async with self._solver_lock:
            if self._solver_session is not None:
                try:
                    await self._solver_session.close()
                except Exception:  # noqa: BLE001
                    pass
                self._solver_session = None
            logger.warning("Scrapling solver session reset (%s)", reason)

    async def _scrapling_call(
        self,
        session: Any,
        url: str,
        *,
        timeout: int,
        wait_selector: Optional[str],
        page_action: Any,
        label: str,
        solver: bool = False,
    ) -> Any:
        """Run one scrapling fetch under a hard wall-clock cap.

        scrapling's own ``timeout`` only bounds page navigation; when the
        underlying browser wedges the call never comes back, so the cap is
        enforced here and a wedged session is replaced.
        """
        cap = max(1, timeout) + max(0, self.render_timeout_grace)

        async def _fetch() -> Any:
            return await asyncio.wait_for(
                session.fetch(
                    url,
                    timeout=timeout * 1000,
                    wait_selector=wait_selector,
                    page_action=page_action,
                ),
                timeout=cap,
            )

        try:
            resp = await _fetch()
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "%s render of %s hit the hard cap (%ss); dropping the session",
                label,
                url,
                cap,
            )
            if solver:
                await self._reset_solver_session(f"hard timeout after {cap}s")
            else:
                await self._recycle_scrapling_session(f"hard timeout after {cap}s")
            raise RenderTimeout(f"render timed out after {cap}s")
        except Exception as exc:  # noqa: BLE001
            if not _is_dead_browser(exc):
                raise
            # The session's browser died (heavy load). Recreate it and retry
            # once instead of failing the request.
            if solver:
                await self._reset_solver_session("browser died")
                session = await self._get_solver_session()
            else:
                await self._restart_scrapling_session("browser died")
                session = self._scrapling_session
            if session is None:
                raise
            resp = await _fetch()
        if not solver:
            self._scrapling_renders += 1
        return resp

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
        if not self._started or self._semaphore is None:
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
        self._inflight += 1
        try:
            if self._recycle_pending:
                self._recycle_pending = False
                await self._recycle_scrapling_session("deferred recycle")
            elif (
                self.session_max_renders > 0
                and self._scrapling_renders >= self.session_max_renders
            ) or (
                self.session_max_age > 0
                and time.monotonic() - self._scrapling_started >= self.session_max_age
            ):
                await self._recycle_scrapling_session(
                    f"budget reached ({self._scrapling_renders} renders, "
                    f"{int(time.monotonic() - self._scrapling_started)}s)"
                )
            if self._scrapling_session is None:
                # Only reachable when the idle watchdog closed the session, so
                # rebuild it instead of failing the first request after a nap.
                await self._restart_scrapling_session("reopen after idle")
            resp = await self._scrapling_call(
                self._scrapling_session,
                url,
                timeout=timeout,
                wait_selector=wait_for or None,
                page_action=_page_action,
                label="scrapling",
            )
            if readability and result.get("article"):
                return result["article"]
            if resp is None or not resp.body:
                raise RuntimeError(f"Empty response for {url}")
            return resp.body.decode("utf-8", errors="ignore")
        finally:
            self._inflight -= 1
            # Still holding the semaphore: no other render can start, so closing
            # the warm tabs here cannot pull a page out from under anyone.
            if self._inflight == 0 and self.close_pages_when_idle:
                await self._close_idle_pages()
                self._schedule_idle_close()
            self._semaphore.release()

    def _schedule_idle_close(self) -> None:
        """Arm the idle timer that closes the session when nothing else runs."""
        if self.session_idle_timeout <= 0:
            return
        self._idle_close_at = time.monotonic() + self.session_idle_timeout
        if self._idle_watch_task is None or self._idle_watch_task.done():
            self._idle_watch_task = asyncio.create_task(self._idle_watch())

    async def _idle_watch(self) -> None:
        """Close the scrapling session once it has been idle long enough.

        A live session means a Chromium tree plus a Playwright driver holding
        hundreds of megabytes with nothing to do. Closing it costs one browser
        launch (~1-2s) on the next render, which is cheap next to rendering the
        page itself, and the response cache absorbs most repeats anyway.
        """
        while True:
            delay = self._idle_close_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay + 0.05)
                continue
            if self._semaphore is None:
                return
            try:
                # Holding the semaphore guarantees no render starts under us.
                await asyncio.wait_for(self._semaphore.acquire(), 1)
            except (asyncio.TimeoutError, TimeoutError):
                return  # renders are queued; their idle tail reschedules us
            try:
                if self._inflight or self._idle_close_at > time.monotonic():
                    continue
                await self._close_idle_session()
                return
            finally:
                self._semaphore.release()

    async def _close_idle_session(self) -> None:
        """Drop the shared session (and its browser tree) while idle."""
        session, self._scrapling_session = self._scrapling_session, None
        self._recycle_pending = False
        self._scrapling_renders = 0
        if session is not None:
            try:
                await asyncio.wait_for(session.close(), 20)
            except BaseException:  # noqa: BLE001 (cleanup never raises)
                logger.exception("Closing the idle scrapling session failed")
            logger.info(
                "Scrapling session closed (idle for %ss)", self.session_idle_timeout
            )
        metrics.set_session_open("scrapling", False)
        metrics.observe_session_close("scrapling", "idle")
        await self._reset_solver_session("idle")

    async def _close_idle_pages(self) -> None:
        """Close the tabs the scrapling page pool keeps warm between renders.

        The pool holds up to ``max_pages`` tabs per session so the next render
        can reuse one, and every warm tab keeps a Chromium renderer plus the
        decoded DOM alive: ~70 MB per heavy page, times one session per worker.
        Our response cache already prevents the re-render, so keeping the DOM
        warm buys a fraction of a second and costs hundreds of megabytes.
        """
        for session in (self._scrapling_session, self._solver_session):
            if session is None:
                continue
            try:
                await asyncio.wait_for(session.close_pages(), 10)
            except BaseException:  # noqa: BLE001 (cleanup never raises)
                pass

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
            resp = await self._scrapling_call(
                session,
                url,
                timeout=timeout,
                wait_selector=wait_for or None,
                page_action=_page_action,
                label="scrapling solver",
                solver=True,
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


# ---------------------------------------------------------------------------
# SERP browser (search.provider=forage)
# ---------------------------------------------------------------------------


class SerpPacer:
    """Process-wide minimum interval between SERP renders.

    Search engines answer a burst of queries with an anti-bot page even when
    the browser itself looks fine (measured against Google: 10 renders back to
    back, 0 usable pages; the same renders spaced out, 10 of 10). The pacer
    serializes renders and enforces ``browser.search.min_interval``, with a
    small random jitter so the cadence is not a fixed metronome.

    Shared by every SERP browser instance of the process, so concurrent
    searches cannot burst either.
    """

    def __init__(self) -> None:
        self._lock: Optional[asyncio.Lock] = None
        self._next_at = 0.0

    async def wait(self, min_interval: float) -> None:
        if min_interval <= 0:
            return
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            now = time.monotonic()
            delay = self._next_at - now
            if delay > 0:
                await asyncio.sleep(delay)
            jitter = (min_interval * 0.25) * _random.random()
            self._next_at = time.monotonic() + min_interval + jitter


SERP_PACER = SerpPacer()


# How long a single close step (page, context, browser, driver) may take.
CLOSE_STEP_TIMEOUT = 15.0


async def _quiet_close(awaitable: Any) -> None:
    """Await one close step. Never raises, never hangs, never skips on cancel.

    ``except Exception`` is not enough here: ``CancelledError`` is a
    ``BaseException``, so a cancelled request would blow straight out of the
    cleanup and leave the browser running.
    """
    try:
        await asyncio.wait_for(awaitable, CLOSE_STEP_TIMEOUT)
    except BaseException:  # noqa: BLE001
        pass


async def _finish_in_background(task: "asyncio.Future[None]") -> None:
    """Wait for a cleanup task, letting it run on if the caller is cancelled."""
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        raise


class SearchBrowser:
    """One-shot browser for SERP rendering.

    Opened when a search starts and closed when it ends: no idle browser sits
    in memory between searches, and the whole search (all engines, all
    pagination) runs inside one browser session, so cookies and history
    accumulate the way they do for a human.

    Two modes, resolved from the config:

    - **cdp**: connect to ``browser.search.cdp_url`` (falling back to
      ``browser.cdp_url``) and render on the real Chrome already running
      there. Pages opened by us are closed at the end; the remote browser is
      never closed. When both CDP and local are configured, CDP wins.
    - **local**: launch the browser shipped in the container (playwright or
      patchright), headful by default (an Xvfb display is started by
      ``app.display``), and close it at the end.

    Usage (one instance per search request)::

        async with SearchBrowser(config.browser) as browser:
            html = await browser.render(url, timeout=20)
    """

    def __init__(self, browser_config: Any) -> None:
        self.settings = browser_config.search
        self.cdp_url = (self.settings.cdp_url or browser_config.cdp_url or "").strip()
        self.mode = "cdp" if self.cdp_url else ("local" if self.settings.local else "")
        self.headless = self.settings.headless
        self.stealth = browser_config.stealth
        self.launch_timeout = browser_config.launch_timeout
        self.user_agent = self.settings.user_agent or browser_config.search.user_agent or DEFAULT_BROWSER_UA
        self.engine = self.settings.engine
        self._pw: Optional[Any] = None
        self._browser: Optional[Any] = None
        self._context: Optional[Any] = None
        self._page: Optional[Any] = None
        self._owns_context = False
        self._render_lock: Optional[asyncio.Lock] = None
        self.started = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if not self.mode:
            raise RuntimeError(
                "search browser disabled: set browser.search.local=true or "
                "browser.search.cdp_url"
            )
        if self.headless is False and not os.environ.get("DISPLAY") and self.mode == "local":
            # No display available (headful requested but Xvfb could not
            # start): degrade to headless instead of failing every search.
            logger.error(
                "Headful search requested but no DISPLAY is available: "
                "falling back to headless (expect anti-bot pages)"
            )
            self.headless = True
        launch_started = time.monotonic()
        try:
            await self._launch()
        except BaseException:
            metrics.observe_launch("search", "error", time.monotonic() - launch_started)
            raise
        metrics.observe_launch("search", "ok", time.monotonic() - launch_started)
        metrics.set_session_open("search", True)
        self.started = True
        logger.info(
            "Search browser ready (mode=%s engine=%s headless=%s)",
            self.mode, self.engine if self.mode == "local" else "-", self.headless,
        )

    async def _launch(self) -> None:
        if self.engine == "patchright":
            from patchright.async_api import async_playwright
        else:
            from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        if self.mode == "cdp":
            self._browser = await self._pw.chromium.connect_over_cdp(
                endpoint_url=self.cdp_url,
                timeout=self.launch_timeout * 1000,
            )
            # Use the browser's real profile (cookies, trust, no incognito).
            # A fresh incognito context is exactly what anti-bot systems flag.
            if self._browser.contexts:
                self._context = self._browser.contexts[0]
                self._owns_context = False
            else:
                self._context = await self._browser.new_context(user_agent=self.user_agent)
                self._owns_context = True
        else:
            args = ["--no-sandbox", "--disable-dev-shm-usage"]
            if self.stealth:
                args.append("--disable-blink-features=AutomationControlled")
            self._browser = await self._pw.chromium.launch(
                headless=self.headless,
                timeout=self.launch_timeout * 1000,
                args=args,
            )
            self._context = await self._browser.new_context(
                user_agent=self.user_agent,
                locale="pt-BR",
                viewport={"width": 1366, "height": 768},
            )
            self._owns_context = True
        self._page = await self._context.new_page()
        # The stealth init script is only for the browsers we launch: on a
        # real Chrome profile it would ADD an automation fingerprint.
        if self.stealth and self.mode == "local":
            await self._page.add_init_script(STEALTH_INIT_SCRIPT)

    async def stop(self) -> None:
        """Close the browser, surviving cancellation.

        Cleanup runs in its own shielded task so a request that gets cancelled
        (client gone, upstream timeout) can die while the browser still goes
        away. The browser itself is closed explicitly in local mode: relying on
        ``playwright.stop()`` alone leaves the node driver plus chromium alive
        whenever that call is skipped or fails, and every leaked tree costs
        ~90 container tasks until the container cannot fork anymore.
        """
        page, context, browser, pw = self._page, self._context, self._browser, self._pw
        owns_context, mode = self._owns_context, self.mode
        self._page = None
        self._context = None
        self._browser = None
        self._pw = None
        self._owns_context = False
        self.started = False
        metrics.set_session_open("search", False)

        async def _drain() -> None:
            if page is not None:
                await _quiet_close(page.close())
            if owns_context and context is not None:
                await _quiet_close(context.close())
            # A CDP browser belongs to whoever is running it: disconnect only.
            if mode == "local" and browser is not None:
                await _quiet_close(browser.close())
            if pw is not None:
                await _quiet_close(pw.stop())

        await _finish_in_background(asyncio.ensure_future(_drain()))

    async def __aenter__(self) -> "SearchBrowser":
        try:
            await self.start()
        except BaseException:
            # _launch() starts the driver before the browser is usable, and
            # ``async with`` never calls __aexit__ when __aenter__ raises: a
            # launch that dies at new_page() (chromium crash, dead CDP
            # endpoint) would otherwise leak the driver and the crashed
            # chromium for the lifetime of the container.
            cleanup = asyncio.ensure_future(self.stop())
            try:
                await asyncio.shield(cleanup)
            except BaseException:  # noqa: BLE001
                cleanup.add_done_callback(lambda t: t.cancelled() or t.exception())
            raise
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.stop()


    # -- rendering ---------------------------------------------------------

    async def use_engine(self, engine: str) -> None:
        """Switch the local browser engine, relaunching if needed.

        No-op in CDP mode (the engine is whatever is behind the CDP endpoint)
        and when the engine is already active.
        """
        if self.mode != "local" or engine == self.engine:
            return
        logger.info("Search browser: switching engine %s -> %s", self.engine, engine)
        await self.stop()
        self.engine = engine
        await self._launch()
        self.started = True

    async def render(
        self,
        url: str,
        timeout: int = 30,
        engine: Optional[str] = None,
        wait_for: Optional[str] = None,
    ) -> str:
        """Render one SERP and return its HTML.

        Applies the process-wide pacer, a human-ish pre-navigation gesture and
        the results settle wait. The page is reused across renders of the same
        search.
        """
        if not self.started or self._page is None:
            raise RuntimeError("Search browser not started")
        if self._render_lock is None:
            self._render_lock = asyncio.Lock()
        # One page per search: renders are serialized here even when the
        # caller fires engines in parallel, because they share that page.
        async with self._render_lock:
            if engine:
                await self.use_engine(engine)
            await SERP_PACER.wait(self.settings.min_interval)
            if self.settings.humanize:
                await self._humanize()
            await self._page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            # Wait for the element that only shows up once the results are
            # rendered (per engine; "h3" is the generic fallback), then let the
            # remaining results settle. No results at all is a valid answer,
            # so the wait is best-effort. The cap is short on purpose: a SERP
            # that never renders the selector is a challenge or an empty page,
            # and waiting the full request timeout on it only delays the next
            # engine. Server-rendered engines (google/bing/yahoo/ddg) have the
            # selector in the initial DOM; only SPAs need longer, and those are
            # not in the default chain.
            try:
                await self._page.wait_for_selector(
                    wait_for or "h3", timeout=min(timeout, 4) * 1000
                )
            except Exception:  # noqa: BLE001 (best-effort: no results is valid)
                pass
            if self.settings.settle_ms:
                await self._page.wait_for_timeout(self.settings.settle_ms)
            return await self._page.content()

    async def _humanize(self) -> None:
        """Mouse movement, a small scroll and a short dwell before navigating.

        Renders that jump straight to the next URL, with no pointer movement
        and no dwell time, are the pattern search engines flag: measured
        against Google, the same renders with a gesture hold up.
        """
        try:
            await self._page.mouse.move(300, 200, steps=8)
            await self._page.mouse.move(900, 480, steps=12)
            await self._page.mouse.wheel(0, 200)
            await self._page.wait_for_timeout(400)
        except Exception:  # noqa: BLE001
            pass
