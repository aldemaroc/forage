"""Firecrawl-compatible API: POST /v1/scrape and POST /v1/search.

Forage's native API is Hermes-shaped (``POST /search``, ``POST /extract``).
This module is a thin adapter that puts Firecrawl's request and response
contract on top of the same extraction, so a client written against Firecrawl
(the pi-web-agent extension that motivated it, the Firecrawl SDKs, anything
that speaks ``POST /v1/scrape``) works unchanged.

It reimplements nothing: the hybrid static -> browser decision, the browser
pool, the document handling and both caches all live in ``app.main`` and are
injected here as the two service callables. Switching
``Firecrawl client -> Forage`` is a base URL change, not a code change.

Contract summary (full matrix in ``docs/FIRECRAWL.md``):

- Success: ``{"success": true, "data": {..., "metadata": {...}}}``.
- Failure: ``{"success": false, "code": <Firecrawl error code>, "error": msg}``.
- A page no engine could extract is HTTP 500 + ``SCRAPE_ALL_ENGINES_FAILED``,
  which is exactly what Firecrawl reports and what clients key on to fall back
  to their own renderer.
- Supported formats: ``markdown`` (default) and ``html`` / ``rawHtml``.
  Everything else Firecrawl can ask for (``json``, ``extract``, ``summary``,
  ``links``, ``screenshot``, ``actions``, ``changeTracking``, ...) is not
  silently faked and does not fail the request either: the page is still
  returned, with the ignored formats/options listed in ``data.warning``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# Firecrawl's own casing for the formats Forage can serve.
FORMAT_CANONICAL = {"markdown": "markdown", "html": "html", "rawhtml": "rawHtml"}

# Forage clamps an extract timeout to 1-120s (config and request level).
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 120

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


class ScrapeRequest(BaseModel):
    """Firecrawl ``POST /scrape`` body.

    ``extra="allow"`` on purpose: Firecrawl has a long list of optional fields
    (``actions``, ``headers``, ``proxy``, ``waitFor``, ``maxAge``, ...) and a
    compatibility layer must not answer 422 because a client sent one of them.
    They are collected in ``model_extra`` and reported in ``data.warning``.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    url: Optional[str] = None
    formats: Optional[Any] = None
    only_main_content: Optional[bool] = Field(default=None, alias="onlyMainContent")
    timeout: Optional[Any] = None


class SearchRequest(BaseModel):
    """Firecrawl ``POST /search`` body (v1). Unknown fields are ignored."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    query: Optional[str] = None
    limit: Optional[Any] = None
    lang: Optional[str] = None
    language: Optional[str] = None
    scrapeOptions: Optional[Any] = None  # noqa: N815 (Firecrawl's field name)


def _error(message: str, code: str, status: int) -> JSONResponse:
    """Firecrawl's error envelope: {"success": false, "code", "error"}."""
    return JSONResponse(
        status_code=status,
        content={"success": False, "code": code, "error": message},
    )


def _normalize_formats(raw: Any) -> Tuple[List[str], List[str]]:
    """Split the requested formats into (supported, ignored).

    Accepts Firecrawl's two shapes: a list of strings (``["markdown"]``) or a
    list of objects (``[{"type": "markdown"}]``), plus a bare string. Returns
    canonical names (``markdown``/``html``/``rawHtml``); anything Forage cannot
    produce lands in the second list with the name the client used.
    """
    items: List[str] = []
    if raw is None:
        items = ["markdown"]
    elif isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        for entry in raw:
            if isinstance(entry, str):
                items.append(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("type"), str):
                items.append(entry["type"])

    supported: List[str] = []
    ignored: List[str] = []
    for name in items:
        canonical = FORMAT_CANONICAL.get(str(name).strip().lower())
        if canonical is None:
            if name not in ignored:
                ignored.append(name)
        elif canonical not in supported:
            supported.append(canonical)
    if not supported:
        # A client asking only for something we cannot produce still gets the
        # page as markdown; the ignored formats are listed in data.warning.
        supported = ["markdown"]
    return supported, ignored


def _timeout_seconds(raw: Any) -> Optional[int]:
    """Firecrawl sends milliseconds; Forage takes seconds (1-120)."""
    if raw is None:
        return None
    try:
        ms = float(raw)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, int(round(ms / 1000.0))))


def _is_timeout(message: str) -> bool:
    return "timeout" in (message or "").lower()


async def _extract_once(
    run_extract: Callable[..., Awaitable[Tuple[List[Dict[str, Any]], str]]],
    url: str,
    fmt: str,
    only_main_content: bool,
    timeout: Optional[int],
    bypass_cache: bool,
) -> Tuple[Dict[str, Any], str]:
    """Run one extraction and return (result, cache state)."""
    results, cache_state = await run_extract(
        [url],
        fmt=fmt,
        only_main_content=only_main_content,
        timeout=timeout,
        bypass_cache=bypass_cache,
    )
    result = results[0] if results else {"url": url, "error": "No result returned"}
    return result, cache_state


def build_firecrawl_router(
    *,
    run_extract: Callable[..., Awaitable[Tuple[List[Dict[str, Any]], str]]],
    run_search: Callable[..., Awaitable[Tuple[Dict[str, Any], str]]],
    require_auth: Callable[..., None],
) -> APIRouter:
    """Build the /v1 router on top of the native Forage services."""
    router = APIRouter(prefix="/v1", tags=["firecrawl-compat"])

    @router.post("/scrape")
    async def scrape(
        req: ScrapeRequest,
        cache_control: Optional[str] = Header(default=None),
        _auth: None = Depends(require_auth),
    ) -> JSONResponse:
        """Scrape one URL and answer in Firecrawl's envelope."""
        url = (req.url or "").strip()
        if not url:
            return _error("Missing required field: url", "BAD_REQUEST", 400)
        if not _URL_RE.match(url):
            return _error(f"Invalid url (http/https required): {url}", "BAD_REQUEST", 400)

        supported, ignored = _normalize_formats(req.formats)
        ignored_options = sorted((req.model_extra or {}).keys())
        timeout = _timeout_seconds(req.timeout)
        only_main_content = True if req.only_main_content is None else req.only_main_content
        bypass_cache = bool(cache_control and "no-cache" in cache_control.lower())

        needs_html = "html" in supported or "rawHtml" in supported
        formats_to_extract = (["markdown"] if "markdown" in supported else []) + (
            ["html"] if needs_html else []
        )

        outcomes = await asyncio.gather(
            *(
                _extract_once(run_extract, url, fmt, only_main_content, timeout, bypass_cache)
                for fmt in formats_to_extract
            )
        )

        data: Dict[str, Any] = {}
        cache_state = "disabled"
        document: Dict[str, Any] = {}
        failure: Optional[str] = None
        for fmt, (result, state) in zip(formats_to_extract, outcomes):
            cache_state = state
            if "error" in result:
                failure = failure or str(result["error"])
                logger.warning("Firecrawl /v1/scrape %s (%s): %s", url, fmt, result["error"])
                continue
            content = result.get("content", "")
            if fmt == "markdown":
                data["markdown"] = content
            else:
                if "html" in supported:
                    data["html"] = content
                if "rawHtml" in supported:
                    data["rawHtml"] = content
            if not document:
                document = result

        if not data:
            failure = failure or "No content extracted"
            if _is_timeout(failure):
                return _error(failure, "SCRAPE_TIMEOUT", 408)
            return _error(failure, "SCRAPE_ALL_ENGINES_FAILED", 500)

        markdown = data.get("markdown")
        if markdown is not None and not markdown.strip() and not needs_html:
            # Firecrawl answers an empty page with a failed scrape, and clients
            # use that to fall back to their own renderer.
            return _error("No content extracted", "SCRAPE_ALL_ENGINES_FAILED", 500)

        source = document.get("rewritten_url") or document.get("url") or url
        payload: Dict[str, Any] = {}
        if "markdown" in data:
            payload["markdown"] = data["markdown"]
        for key in ("html", "rawHtml"):
            if key in data:
                payload[key] = data[key]
        payload["metadata"] = {
            "title": document.get("title") or "",
            "sourceURL": source,
            "url": source,
            "statusCode": 200,
            "cacheState": cache_state,
        }

        warnings: List[str] = []
        if ignored:
            warnings.append(f"formats not supported by Forage were ignored: {', '.join(ignored)}")
        if ignored_options:
            warnings.append(
                "options not supported by Forage were ignored: " + ", ".join(ignored_options)
            )
        if warnings:
            payload["warning"] = "; ".join(warnings)
            logger.info("Firecrawl /v1/scrape %s: %s", url, payload["warning"])

        return JSONResponse(
            content={"success": True, "data": payload},
            headers={"X-Forage-Cache": cache_state},
        )

    @router.post("/search")
    async def search(
        req: SearchRequest,
        cache_control: Optional[str] = Header(default=None),
        _auth: None = Depends(require_auth),
    ) -> JSONResponse:
        """Search and answer in Firecrawl's v1 search envelope.

        Note: Firecrawl v1 returns ``data`` as an array of documents, while v2
        returns an object (``{"web": [...]}``). This speaks v1.
        """
        query = (req.query or "").strip()
        if not query:
            return _error("Missing required field: query", "BAD_REQUEST", 400)

        limit = req.limit if isinstance(req.limit, int) and not isinstance(req.limit, bool) else 5
        limit = max(1, min(100, limit))
        language = req.lang or req.language
        bypass_cache = bool(cache_control and "no-cache" in cache_control.lower())

        payload, cache_state = await run_search(
            query, limit, language=language, bypass_cache=bypass_cache
        )
        if not payload.get("success"):
            return _error(
                str(payload.get("error") or "Search failed"), "UNKNOWN_ERROR", 500
            )

        web = ((payload.get("data") or {}).get("web")) or []
        documents: List[Dict[str, Any]] = []
        for item in web:
            doc = {
                "url": item.get("url"),
                "title": item.get("title"),
                "description": item.get("description"),
                "position": item.get("position"),
            }
            documents.append({k: v for k, v in doc.items() if v is not None})

        body: Dict[str, Any] = {
            "success": True,
            "data": documents,
            "id": str(uuid.uuid4()),
        }

        ignored = sorted((req.model_extra or {}).keys())
        warnings: List[str] = []
        if req.scrapeOptions:
            formats = req.scrapeOptions.get("formats") if isinstance(req.scrapeOptions, dict) else None
            if formats:
                warnings.append(
                    "scrapeOptions are not supported by Forage: results carry no page content"
                )
            ignored = [name for name in ignored if name != "scrapeOptions"]
        if ignored:
            warnings.append("options not supported by Forage were ignored: " + ", ".join(ignored))
        if warnings:
            body["warning"] = "; ".join(warnings)
            logger.info("Firecrawl /v1/search %r: %s", query, body["warning"])

        return JSONResponse(content=body, headers={"X-Forage-Cache": cache_state})

    return router
