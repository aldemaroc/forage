"""Forage: self-hosted web search & extract service for Hermes.

Phase 3: /search (SearXNG) + /extract (hybrid static -> browser).
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, Dict, List, Optional

# prometheus_client decides between single-process and multiprocess mode when it
# is imported, so the directory has to be decided before `.metrics` is imported.
# uvicorn workers inherit this variable, and each one writes its own file there:
# a scrape then reports the whole container instead of the worker that answered.
os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", "/tmp/forage-prometheus")

if __name__ == "__main__":
    # Only the supervisor gets here (`python -m app.main`); uvicorn's workers
    # import this module as `app.main`, so they skip the wipe and keep each
    # other's files. The wipe has to happen *before* the metrics module is
    # imported: a metric's file is created when the metric object is built, so
    # deleting the directory afterwards would leave the supervisor writing to an
    # unlinked inode and its counters would never be scraped. Files from a
    # previous run would otherwise keep reporting processes that are gone.
    import pathlib as _pathlib
    import shutil as _shutil

    _mp_dir = os.environ["PROMETHEUS_MULTIPROC_DIR"]
    _shutil.rmtree(_mp_dir, ignore_errors=True)
    _pathlib.Path(_mp_dir).mkdir(parents=True, exist_ok=True)

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import __version__
from . import metrics, proctree
from .auth import extract_bearer, key_is_valid, load_api_keys
from .browser import BrowserPool, SearchBrowser
from .cache import TTLCache
from .config import load_config
from .display import ensure_display, stop_display
from .extract import extract_url
from .firecrawl import API_PREFIXES, build_firecrawl_routers
from .searxng import search_searxng
from .searxng_compat import build_searxng_router
from .serp import search_forage

config = load_config()

logging.basicConfig(
    level=getattr(logging, config.server.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("forage")

search_cache = TTLCache(max_entries=config.cache.max_entries)
extract_cache = TTLCache(max_entries=config.cache.max_entries)

# The extract browser keeps a warm pool of instances (static first, browser as
# fallback, so instances live between requests). Search does NOT use this pool:
# it opens its own one-shot SearchBrowser per request and closes it at the end.
def _make_pools() -> Dict[str, BrowserPool]:
    eng = config.browser.engine
    bc = config.browser
    if eng == "chrome-local" and not bc.cdp_url:
        bc = replace(bc, cdp_url="http://172.20.0.1:9222")
    pool = BrowserPool(bc, user_agent=config.extract.browser_user_agent)
    logger.info("Extract browser pool for engine=%s", eng)
    return {eng: pool}


browser_pools: Dict[str, BrowserPool] = _make_pools()


def browser_chain_for(search_engine: str) -> List[str]:
    """Ordered list of local browser engines to try for a search engine.

    ``browser.search_engine_overrides`` may map a search engine to a single
    engine id or a fallback chain (list); unlisted search engines use
    ``browser.search.engine``. Only engines that can be launched per search
    (playwright, patchright) are honored: scrapling keeps a session of its own,
    and obscura/chrome-local are CDP endpoints, now configured for search as a
    whole through ``browser.search.cdp_url``.
    """
    override = config.browser.search_engine_overrides.get(search_engine)
    chain = list(override) if isinstance(override, (list, tuple)) else ([override] if override else [])
    usable = [eng for eng in chain if eng in ("playwright", "patchright")]
    dropped = [eng for eng in chain if eng not in usable]
    if dropped:
        logger.warning(
            "browser.search_engine_overrides[%s]: ignoring %s for search "
            "(only playwright/patchright launch per search; use "
            "browser.search.cdp_url for a CDP browser)",
            search_engine, ", ".join(dropped),
        )
    return usable or [config.browser.search.engine]

api_keys = load_api_keys()
bearer_scheme = HTTPBearer(auto_error=False)


def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> None:
    """Reject unauthenticated requests when auth.enabled is true."""
    if not config.auth.enabled:
        return
    # HTTPBearer already strips the "Bearer " scheme; credentials.credentials
    # is the raw token. Do NOT run extract_bearer again here.
    token = credentials.credentials if credentials else None
    if not key_is_valid(token, api_keys):
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _idle_maintenance(interval: float) -> None:
    """Drop expired cache entries and hand the freed memory back to the OS.

    Two things keep Forage fat while idle: expired cache entries, which stay in
    the dict until someone asks for the key, and glibc, which keeps freed
    blocks in its arenas so RSS never comes down even after the data is gone.
    A sweep plus ``malloc_trim`` returns the process to its working set.
    """
    while True:
        try:
            dropped = search_cache.purge_expired() + extract_cache.purge_expired()
            metrics.set_cache_entries("search", len(search_cache))
            metrics.set_cache_entries("extract", len(extract_cache))
            if dropped:
                logger.debug("Cache sweep dropped %d expired entrie(s)", dropped)
            _release_free_memory()
        except Exception:  # noqa: BLE001 (maintenance must never kill the app)
            logger.exception("Idle maintenance failed")
        await asyncio.sleep(max(interval, 5.0))


def _release_free_memory() -> None:
    """Return glibc's free arenas to the kernel (Linux only, best effort)."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # noqa: BLE001 (musl, macOS, Windows: nothing to do)
        pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # A headful browser needs an X display. Start one (Xvfb) before the pools,
    # and only when the configuration actually asks for a headful local browser
    # (a search browser rendering through CDP needs no display of ours).
    search_local_headful = (
        config.search.provider == "forage"
        and config.browser.search.local
        and not (config.browser.search.cdp_url or config.browser.cdp_url)
        and not config.browser.search.headless
    )
    # First thing in the worker: pick this instance's session registry and patch
    # the asyncio spawn so every browser driver becomes a session leader. The
    # pools below warm browsers at start, so this cannot move any lower. The
    # supervisor's janitor (started in main()) cleans up after a worker that dies
    # without closing its browsers.
    proctree.configure(config.server.port)
    proctree.install()
    needs_display = (not config.browser.headless) or search_local_headful
    display = ensure_display() if needs_display else None
    for pool in browser_pools.values():
        await pool.start()
    metrics.set_info(__version__, config.browser.engine, config.search.provider)
    metrics.set_cache_entries("search", len(search_cache))
    metrics.set_cache_entries("extract", len(extract_cache))
    maintenance_task = None
    if config.cache.sweep_interval > 0:
        maintenance_task = asyncio.create_task(
            _idle_maintenance(float(config.cache.sweep_interval))
        )
    yield
    if maintenance_task is not None:
        maintenance_task.cancel()
        try:
            await maintenance_task
        except BaseException:  # noqa: BLE001 (shutdown must not raise)
            pass
    for pool in browser_pools.values():
        await pool.stop()
    stop_display(display)


app = FastAPI(
    title="Forage",
    version=__version__,
    description=(
        "Self-hosted web search & extract service for Hermes, with a "
        "Firecrawl-compatible API served under every prefix in "
        "app.firecrawl.API_PREFIXES (POST /v1/scrape, POST /v1/search, and the "
        "same routes under /v2, where /v2/search uses Firecrawl v2's grouped "
        "{'web': [...]} data envelope)."
    ),
    lifespan=lifespan,
)


@app.middleware("http")
async def _traffic_metrics(request: Request, call_next):
    """Count every request against its route template, never its raw path.

    Using the template (``/v1/scrape``) instead of the URL keeps the ``route``
    label bounded: a public service would otherwise mint a series per URL.
    """
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        metrics.observe_http(request.method, route, 500, time.perf_counter() - started)
        raise
    route = getattr(request.scope.get("route"), "path", "unmatched")
    metrics.observe_http(request.method, route, response.status_code, time.perf_counter() - started)
    return response


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=50)
    language: Optional[str] = Field(default=None, max_length=20)
    engines: Optional[List[str]] = None


class ExtractRequest(BaseModel):
    urls: List[str] = Field(min_length=1, max_length=20)
    formats: Optional[List[str]] = Field(default=None, max_length=5)
    only_main_content: bool = True
    force_render: bool = False
    wait_for: Optional[str] = Field(default=None, max_length=200)
    timeout: Optional[int] = Field(default=None, ge=1, le=120)
    engine: Optional[str] = Field(default=None, pattern="^(trafilatura|readability)$")


#: Every ``method`` value ``extract.py``/``documents.py`` can report, plus
#: ``other`` for anything unforeseen. The label is whitelisted because a label
#: value invented at runtime would add a series nobody asked for.
_EXTRACT_METHODS = frozenset(
    {"static", "browser", "browser+solver", "browser+readability", "pdf", "docx", "xlsx", "pptx", "rtf"}
)


def _extract_method_label(method: Optional[str]) -> str:
    return method if method in _EXTRACT_METHODS else "other"


def _search_cache_key(
    query: str, limit: int, language: Optional[str], engines: Optional[List[str]]
) -> str:
    joined = ",".join(sorted(engines)) if engines else ""
    return f"search:{query}|{limit}|{language or ''}|{joined}"


def _extract_cache_key(urls: List[str], force_render: bool, wait_for: Optional[str], fmt: str, engine: Optional[str]) -> str:
    return f"extract:{','.join(urls)}|{force_render}|{wait_for or ''}|{fmt}|{engine or ''}"


async def run_search(
    query: str,
    limit: int,
    language: Optional[str] = None,
    engines: Optional[List[str]] = None,
    bypass_cache: bool = False,
) -> tuple[Dict[str, Any], str]:
    """Search through the configured provider, with cache.

    Returns ``(payload, cache_state)``. Used by ``POST /search`` and by the
    Firecrawl-compatible ``POST /v1/search`` and ``POST /v2/search``: both
    prefixes share this one implementation and the same cache entries, and only
    the Firecrawl envelope wrapped around the result differs by version.
    """
    cache_enabled = config.cache.enabled and config.cache.search.enabled and not bypass_cache
    key = _search_cache_key(query, limit, language, engines)
    started = time.perf_counter()
    provider = config.search.provider
    if cache_enabled:
        cached = search_cache.get(key)
        if cached is not None:
            metrics.observe_cache("search", "hit")
            metrics.observe_search(
                provider,
                "hit",
                "ok" if cached.get("success") else "error",
                time.perf_counter() - started,
                len(cached.get("data") or []),
            )
            return cached, "hit"
        metrics.observe_cache("search", "miss")

    if config.search.provider == "forage":
        # One browser per search, opened here and closed at the end of the
        # request: nothing stays warm between searches.
        async with SearchBrowser(config.browser) as browser:
            result = await search_forage(
                config,
                browser,
                browser_chain_for,
                query=query,
                limit=limit,
                language=language,
                engines=engines,
            )
    else:
        result = search_searxng(
            config,
            query=query,
            limit=limit,
            language=language,
            engines=engines,
        )

    if cache_enabled and result.get("success"):
        search_cache.set(key, result, ttl=config.cache.search.ttl)
        metrics.set_cache_entries(
            "search", len(search_cache)
        )

    header = "miss" if cache_enabled else ("bypass" if bypass_cache else "disabled")
    metrics.observe_search(
        provider,
        header,
        "ok" if result.get("success") else "error",
        time.perf_counter() - started,
        len(result.get("data") or []),
    )
    return result, header


async def run_extract(
    urls: List[str],
    *,
    fmt: str = "markdown",
    only_main_content: bool = True,
    force_render: bool = False,
    wait_for: Optional[str] = None,
    timeout: Optional[int] = None,
    engine: Optional[str] = None,
    bypass_cache: bool = False,
) -> tuple[List[Dict[str, Any]], str]:
    """Extract URLs with the hybrid strategy (static -> browser fallback).

    Returns ``(results, cache_state)``, one result dict per URL and in the
    input order. Used by ``POST /extract`` and by the Firecrawl-compatible
    ``POST /v1/scrape`` and ``POST /v2/scrape``: both prefixes hit the same
    extraction path and the same cache.
    """
    cache_enabled = config.cache.enabled and config.cache.extract.enabled and not bypass_cache
    key = _extract_cache_key(urls, force_render, wait_for, fmt, engine)
    started = time.perf_counter()
    if cache_enabled:
        cached = extract_cache.get(key)
        if cached is not None:
            metrics.observe_cache("extract", "hit")
            for item in cached:
                metrics.observe_extract(
                    _extract_method_label(item.get("method")),
                    "cached",
                    0.0,
                    len(item.get("markdown") or item.get("content") or ""),
                )
            return cached, "hit"
        metrics.observe_cache("extract", "miss")

    async def _extract_one(url: str) -> Dict[str, Any]:
        url_started = time.perf_counter()
        try:
            result = await extract_url(
                config,
                browser_pools[config.browser.engine],
                url,
                force_render=force_render,
                wait_for=wait_for,
                output_format=fmt,
                only_main_content=only_main_content,
                timeout=timeout,
                engine=engine,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Extract failed for %s", url)
            metrics.observe_extract("other", "error", time.perf_counter() - url_started, 0)
            return {"url": url, "error": str(exc)}
        payload = result.get("markdown") or result.get("content") or ""
        metrics.observe_extract(
            _extract_method_label(result.get("method")),
            "error" if "error" in result else "ok",
            time.perf_counter() - url_started,
            len(payload),
        )
        return result

    # Parallel extraction: static fetches run concurrently; browser renders are
    # bounded by the pool semaphore (browser.max_instances). gather preserves
    # the input URL order.
    results = await asyncio.gather(*(_extract_one(u) for u in urls))

    if cache_enabled and all("error" not in r for r in results):
        extract_cache.set(key, results, ttl=config.cache.extract.ttl)
        metrics.set_cache_entries("extract", len(extract_cache))

    header = "miss" if cache_enabled else ("bypass" if bypass_cache else "disabled")
    return list(results), header


@app.get("/health")
async def health() -> dict:
    """Liveness probe: cheap, no I/O."""
    return {
        "status": "ok",
        "service": "forage",
        "version": __version__,
        "config_source": config.source_path,
        "browser_engine": config.browser.engine,
        "search_provider": config.search.provider,
        "firecrawl_prefixes": list(API_PREFIXES),
        "search_browser": {
            "mode": "cdp" if (config.browser.search.cdp_url or config.browser.cdp_url) else
                    ("local" if config.browser.search.local else "disabled"),
            "engine": config.browser.search.engine,
            "headless": config.browser.search.headless,
        },
        "cache": {
            "enabled": config.cache.enabled,
            "max_entries": config.cache.max_entries,
            "search": {
                "enabled": config.cache.search.enabled,
                "ttl": config.cache.search.ttl,
            },
            "extract": {
                "enabled": config.cache.extract.enabled,
                "ttl": config.cache.extract.ttl,
            },
        },
    }


@app.post("/search")
async def search(
    req: SearchRequest,
    request: Request,
    cache_control: Optional[str] = Header(default=None),
    _auth: None = Depends(require_auth),
) -> JSONResponse:
    """Search via the configured provider (SearXNG or Forage's own SERP
    engines), normalized to the Hermes web-search envelope."""
    bypass = bool(cache_control and "no-cache" in cache_control.lower())
    result, header = await run_search(
        req.query, req.limit, req.language, req.engines, bypass_cache=bypass
    )
    return JSONResponse(content=result, headers={"X-Forage-Cache": header})


@app.post("/extract")
async def extract(
    req: ExtractRequest,
    request: Request,
    cache_control: Optional[str] = Header(default=None),
    _auth: None = Depends(require_auth),
) -> JSONResponse:
    """Extract URLs using the hybrid strategy (static -> browser fallback)."""
    bypass = bool(cache_control and "no-cache" in cache_control.lower())

    fmt = "markdown"
    if req.formats:
        if "html" in req.formats:
            fmt = "html"
        elif "raw_html" in req.formats:
            fmt = "html"

    results, header = await run_extract(
        req.urls,
        fmt=fmt,
        only_main_content=req.only_main_content,
        force_render=req.force_render,
        wait_for=req.wait_for,
        timeout=req.timeout,
        engine=req.engine,
        bypass_cache=bypass,
    )
    return JSONResponse(
        content={"success": True, "data": results}, headers={"X-Forage-Cache": header}
    )


def _metrics_denied(request: Request):
    """SearXNG's ``general.open_metrics`` check, for compatibility.

    When a password is configured the scrape must send HTTP Basic auth whose
    password matches it; the username is ignored, exactly like SearXNG. Empty
    password means the endpoint is open.
    """
    password = config.metrics.open_metrics
    if not password:
        return None
    header = request.headers.get("authorization", "")
    given = ""
    if header[:6].lower() == "basic ":
        try:
            decoded = base64.b64decode(header[6:].strip()).decode("utf-8", "replace")
            given = decoded.partition(":")[2]
        except Exception:  # noqa: BLE001 (malformed header is just a denial)
            given = ""
    if given and hmac.compare_digest(given, password):
        return None
    return PlainTextResponse(
        "access forbidden\n",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="metrics"'},
    )


def _metrics_response(request: Request):
    if not config.metrics.enabled:
        # 404 (not 403) mirrors SearXNG: a scrape that gets this means the
        # setting never took effect, not that the credential is wrong.
        raise HTTPException(status_code=404, detail="open metrics is disabled")
    denied = _metrics_denied(request)
    if denied is not None:
        return denied
    return PlainTextResponse(metrics.render().decode("utf-8"), media_type="text/plain; version=0.0.4")


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request):
    """Prometheus/OpenMetrics exposition (SearXNG-compatible series included)."""
    return _metrics_response(request)


@app.get("/admin/metrics", include_in_schema=False)
async def prometheus_metrics_admin(
    request: Request, _auth: None = Depends(require_auth)
):
    """Same exposition under Firecrawl's admin path, so a scrape config written
    for Firecrawl (`GET /admin/metrics`, text/plain) keeps working."""
    return _metrics_response(request)


@app.post("/admin/cache/purge")
async def purge_cache(_auth: None = Depends(require_auth)) -> dict:
    """Clear the in-memory caches (search + extract)."""
    cleared = search_cache.clear() + extract_cache.clear()
    return {"cleared": cleared}


# Firecrawl-compatible surface, mounted once per version prefix from one shared
# set of route implementations: /v2/scrape is identical to /v1/scrape, and
# /v2/search is /v1/search with Firecrawl's v2 grouped data envelope
# ({"web": [...]}). See app.firecrawl.API_PREFIXES.
for firecrawl_router in build_firecrawl_routers(
    run_extract=run_extract,
    run_search=run_search,
    require_auth=require_auth,
    prefixes=API_PREFIXES,
):
    logger.info("Mounting Firecrawl-compatible API at %s", firecrawl_router.prefix)
    app.include_router(firecrawl_router)

# SearXNG-compatible read surface: GET /search and GET / answer with SearXNG's
# query parameters and JSON envelope, over the same run_search (same engines,
# same browser, same caches). Clients that only speak SearXNG as a search
# provider can point their instance URL at Forage. The native POST /search
# keeps its Hermes-shaped contract. See docs/SEARXNG-API.md.
app.include_router(build_searxng_router(run_search=run_search, config=config))
logger.info("Mounting SearXNG-compatible API at GET /search and GET /")


if __name__ == "__main__":
    import uvicorn

    logger.info(
        "Starting Forage %s on %s:%s (config: %s)",
        __version__,
        config.server.host,
        config.server.port,
        config.source_path,
    )
    # The X display is started by this process, not by a worker: an Xvfb owned
    # by a worker becomes an orphan the moment that worker dies (OOM kill,
    # crash, restart) and the surviving worker still needs it. Owning it here
    # also means it goes away with the container.
    wants_headful = (
        not config.browser.headless
        or (
            config.search.provider == "forage"
            and config.browser.search.local
            and not (config.browser.search.cdp_url or config.browser.cdp_url)
            and not config.browser.search.headless
        )
    )
    display = ensure_display() if wants_headful else None
    # Sessions left behind by a worker that died without closing them belong to
    # nobody once that worker is gone, so this process (the only one that
    # outlives every worker) sweeps them.
    proctree.configure(config.server.port)
    proctree.reset_registry()
    proctree.install()
    proctree.start_janitor_thread(float(config.server.janitor_interval))
    try:
        uvicorn.run(
            "app.main:app",
            host=config.server.host,
            port=config.server.port,
            workers=config.server.workers,
            log_level=config.server.log_level,
        )
    finally:
        stop_display(display)
