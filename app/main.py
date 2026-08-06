"""Forage: self-hosted web search & extract service for Hermes.

Phase 3: /search (SearXNG) + /extract (hybrid static -> browser).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

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

config = load_config()

logging.basicConfig(
    level=getattr(logging, config.server.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("forage")

search_cache = TTLCache(max_entries=config.cache.max_entries)
extract_cache = TTLCache(max_entries=config.cache.max_entries)
browser_pool = BrowserPool(config.browser, user_agent=config.extract.browser_user_agent)

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
    await browser_pool.start()
    yield
    await browser_pool.stop()


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


def _search_cache_key(req: SearchRequest) -> str:
    engines = ",".join(sorted(req.engines)) if req.engines else ""
    return f"search:{req.query}|{req.limit}|{req.language or ''}|{engines}"


def _extract_cache_key(urls: List[str], force_render: bool, wait_for: Optional[str], fmt: str) -> str:
    return f"extract:{','.join(urls)}|{force_render}|{wait_for or ''}|{fmt}"


@app.get("/health")
async def health() -> dict:
    """Liveness probe: cheap, no I/O."""
    return {
        "status": "ok",
        "service": "forage",
        "version": __version__,
        "config_source": config.source_path,
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
    """Search via SearXNG, normalized to the Hermes web-search envelope."""
    bypass = bool(cache_control and "no-cache" in cache_control.lower())
    cache_enabled = config.cache.enabled and config.cache.search.enabled and not bypass

    key = _search_cache_key(req)
    if cache_enabled:
        cached = search_cache.get(key)
        if cached is not None:
            return JSONResponse(content=cached, headers={"X-Forage-Cache": "hit"})

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

    key = _extract_cache_key(req.urls, req.force_render, req.wait_for, fmt)
    if cache_enabled:
        cached = extract_cache.get(key)
        if cached is not None:
            return JSONResponse(content=cached, headers={"X-Forage-Cache": "hit"})

    async def _extract_one(url: str) -> Dict[str, Any]:
        try:
            return await extract_url(
                config,
                browser_pool,
                url,
                force_render=req.force_render,
                wait_for=req.wait_for,
                output_format=fmt,
                only_main_content=req.only_main_content,
                timeout=req.timeout,
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
