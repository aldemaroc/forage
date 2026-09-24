"""Forage: self-hosted web search & extract service for Hermes.

Phase 3: /search (SearXNG) + /extract (hybrid static -> browser).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import __version__
from .auth import extract_bearer, key_is_valid, load_api_keys
from .browser import BrowserPool, SearchBrowser
from .cache import TTLCache
from .config import load_config
from .display import ensure_display, stop_display
from .extract import extract_url
from .firecrawl import build_firecrawl_router
from .searxng import search_searxng
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
    needs_display = (not config.browser.headless) or search_local_headful
    display = ensure_display() if needs_display else None
    for pool in browser_pools.values():
        await pool.start()
    yield
    for pool in browser_pools.values():
        await pool.stop()
    stop_display(display)


app = FastAPI(
    title="Forage",
    version=__version__,
    description=(
        "Self-hosted web search & extract service for Hermes, with a "
        "Firecrawl-compatible API (POST /v1/scrape, POST /v1/search)."
    ),
    lifespan=lifespan,
)


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
    Firecrawl-compatible ``POST /v1/search``.
    """
    cache_enabled = config.cache.enabled and config.cache.search.enabled and not bypass_cache
    key = _search_cache_key(query, limit, language, engines)
    if cache_enabled:
        cached = search_cache.get(key)
        if cached is not None:
            return cached, "hit"

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

    header = "miss" if cache_enabled else ("bypass" if bypass_cache else "disabled")
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
    ``POST /v1/scrape``.
    """
    cache_enabled = config.cache.enabled and config.cache.extract.enabled and not bypass_cache
    key = _extract_cache_key(urls, force_render, wait_for, fmt, engine)
    if cache_enabled:
        cached = extract_cache.get(key)
        if cached is not None:
            return cached, "hit"

    async def _extract_one(url: str) -> Dict[str, Any]:
        try:
            return await extract_url(
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
            return {"url": url, "error": str(exc)}

    # Parallel extraction: static fetches run concurrently; browser renders are
    # bounded by the pool semaphore (browser.max_instances). gather preserves
    # the input URL order.
    results = await asyncio.gather(*(_extract_one(u) for u in urls))

    if cache_enabled and all("error" not in r for r in results):
        extract_cache.set(key, results, ttl=config.cache.extract.ttl)

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


@app.post("/admin/cache/purge")
async def purge_cache(_auth: None = Depends(require_auth)) -> dict:
    """Clear the in-memory caches (search + extract)."""
    cleared = search_cache.clear() + extract_cache.clear()
    return {"cleared": cleared}


app.include_router(
    build_firecrawl_router(
        run_extract=run_extract,
        run_search=run_search,
        require_auth=require_auth,
    )
)


if __name__ == "__main__":
    import uvicorn

    logger.info(
        "Starting Forage %s on %s:%s (config: %s)",
        __version__,
        config.server.host,
        config.server.port,
        config.source_path,
    )
    uvicorn.run(
        "app.main:app",
        host=config.server.host,
        port=config.server.port,
        workers=config.server.workers,
        log_level=config.server.log_level,
    )
