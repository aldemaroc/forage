"""Forage: self-hosted web search & extract service for Hermes.

Phase 3: /search (SearXNG) + /extract (hybrid static -> browser).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import __version__
from .auth import extract_bearer, key_is_valid, load_api_keys
from .browser import BrowserPool
from .cache import TTLCache
from .config import load_config
from .extract import extract_url
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

# One BrowserPool per distinct browser engine that will be used. The default
# pool matches browser.engine; extra pools are created for the engines named
# in browser.search_engine_overrides (provider=forage). Each pool keeps its
# own Playwright launch / Scrapling session, so switching engines per search
# engine is just a dict lookup.
def _make_pools() -> Dict[str, BrowserPool]:
    engines_needed = {config.browser.engine}
    for chain in config.browser.search_engine_overrides.values():
        engines_needed.update(chain if isinstance(chain, (list, tuple)) else [chain])
    pools: Dict[str, BrowserPool] = {}
    for eng in sorted(engines_needed):
        bc = replace(config.browser, engine=eng)
        if eng == "chrome-local" and not bc.cdp_url:
            bc = replace(bc, cdp_url=config.browser.cdp_url or "http://172.20.0.1:9222")
        pools[eng] = BrowserPool(bc, user_agent=config.extract.browser_user_agent)
        logger.info("Browser pool for engine=%s", eng)
    return pools


browser_pools: Dict[str, BrowserPool] = _make_pools()


def get_pool_for_search_engine(search_engine: str, browser: Optional[str] = None) -> BrowserPool:
    """Resolve the browser pool used to render a given search engine SERP
    (provider=forage). Falls back to the configured default browser engine.

    ``browser`` overrides the chain (used by the fallback logic in serp.py).
    """
    wanted = browser or _first_browser_for(search_engine)
    return browser_pools[wanted]


def browser_chain_for(search_engine: str) -> List[str]:
    """Ordered list of browser engines to try for a search engine (provider=forage).

    ``browser.search_engine_overrides`` may map a search engine to a single
    engine id or a fallback chain (list). Unlisted search engines use the
    configured default browser engine.
    """
    override = config.browser.search_engine_overrides.get(search_engine)
    if override:
        return list(override) if isinstance(override, (list, tuple)) else [override]
    return [config.browser.engine]


def _first_browser_for(search_engine: str) -> str:
    return browser_chain_for(search_engine)[0]

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
    for pool in browser_pools.values():
        await pool.start()
    yield
    for pool in browser_pools.values():
        await pool.stop()


app = FastAPI(
    title="Forage",
    version=__version__,
    description="Self-hosted web search & extract service for Hermes.",
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


def _search_cache_key(req: SearchRequest) -> str:
    engines = ",".join(sorted(req.engines)) if req.engines else ""
    return f"search:{req.query}|{req.limit}|{req.language or ''}|{engines}"


def _extract_cache_key(urls: List[str], force_render: bool, wait_for: Optional[str], fmt: str, engine: Optional[str]) -> str:
    return f"extract:{','.join(urls)}|{force_render}|{wait_for or ''}|{fmt}|{engine or ''}"


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
    cache_enabled = config.cache.enabled and config.cache.search.enabled and not bypass

    key = _search_cache_key(req)
    if cache_enabled:
        cached = search_cache.get(key)
        if cached is not None:
            return JSONResponse(content=cached, headers={"X-Forage-Cache": "hit"})

    if config.search.provider == "forage":
        result = await search_forage(
            config,
            get_pool_for_search_engine,
            browser_chain_for,
            query=req.query,
            limit=req.limit,
            language=req.language,
            engines=req.engines,
        )
    else:
        result = search_searxng(
            config,
            query=req.query,
            limit=req.limit,
            language=req.language,
            engines=req.engines,
        )

    if cache_enabled and result.get("success"):
        search_cache.set(key, result, ttl=config.cache.search.ttl)

    header = "miss" if cache_enabled else ("bypass" if bypass else "disabled")
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
    cache_enabled = config.cache.enabled and config.cache.extract.enabled and not bypass

    fmt = "markdown"
    if req.formats:
        if "html" in req.formats:
            fmt = "html"
        elif "raw_html" in req.formats:
            fmt = "html"

    key = _extract_cache_key(req.urls, req.force_render, req.wait_for, fmt, req.engine)
    if cache_enabled:
        cached = extract_cache.get(key)
        if cached is not None:
            return JSONResponse(content=cached, headers={"X-Forage-Cache": "hit"})

    async def _extract_one(url: str) -> Dict[str, Any]:
        try:
            return await extract_url(
                config,
                browser_pools[config.browser.engine],
                url,
                force_render=req.force_render,
                wait_for=req.wait_for,
                output_format=fmt,
                only_main_content=req.only_main_content,
                timeout=req.timeout,
                engine=req.engine,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Extract failed for %s", url)
            return {"url": url, "error": str(exc)}

    # Parallel extraction: static fetches run concurrently; browser renders are
    # bounded by the pool semaphore (browser.max_instances). gather preserves
    # the input URL order in the envelope.
    results = await asyncio.gather(*(_extract_one(u) for u in req.urls))
    payload = {"success": True, "data": results}

    if cache_enabled:
        all_ok = all("error" not in r for r in results)
        if all_ok:
            extract_cache.set(key, payload, ttl=config.cache.extract.ttl)

    header = "miss" if cache_enabled else ("bypass" if bypass else "disabled")
    return JSONResponse(content=payload, headers={"X-Forage-Cache": header})


@app.post("/admin/cache/purge")
async def purge_cache(_auth: None = Depends(require_auth)) -> dict:
    """Clear the in-memory caches (search + extract)."""
    cleared = search_cache.clear() + extract_cache.clear()
    return {"cleared": cleared}


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
