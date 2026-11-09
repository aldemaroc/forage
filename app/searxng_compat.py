"""SearXNG-compatible read API: ``GET /search`` and ``GET /``.

Forage's native API is Hermes-shaped: ``POST /search`` with a JSON body. Most
self-hosted chat UIs, however, only implement SearXNG as a search provider and
discover a backend as ``<base>/search?q=...&format=json`` (LibreChat is the
client that motivated this). This module answers exactly that call with
SearXNG's JSON envelope, on top of the same ``run_search`` the native endpoint
uses: same engines, same browser, same pacer, same cache entries. It
reimplements nothing and never touches extraction.

What is emulated is the READ path only:

- ``GET /search`` and ``GET /`` (SearXNG's two documented endpoints).
- ``format=json`` is the only accepted format. SearXNG answers 403 for a
  format its settings do not enable, and so does this: no HTML, no CSV, no RSS.
- ``POST /search`` keeps Forage's native JSON contract untouched. SearXNG's
  form-encoded POST is deliberately not implemented, because the route already
  has a different meaning here.

Deliberate gaps, all documented in ``docs/SEARXNG-API.md``:

- ``categories``: ``general`` only. Forage has no image/video/news/map engine,
  so a request for another category gets an empty result set instead of
  unrelated web results.
- ``pageno``: page 1 only. A higher page gets an empty ``results`` array, which
  reads as "no more pages".
- ``time_range`` and ``safesearch`` are accepted and ignored: the SERP engines
  Forage drives expose no such filter.
- ``answers``, ``infoboxes``, ``corrections`` and ``suggestions`` are always
  empty because Forage does not produce them.
- ``engine`` / ``engines`` reflect the engines that answered this search, and
  ``score`` is derived from the result rank: Forage does not compute SearXNG's
  relevance score, and pretending otherwise would be a lie in the payload.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .auth import key_is_valid, load_api_keys
from .serp import SERP_ALIASES, SERP_ENGINES

logger = logging.getLogger(__name__)

# SearXNG clients do not send a limit: they expect one page of results, which
# is 10-20 on a real instance. 10 matches Forage's own PAGE_SIZE.
DEFAULT_LIMIT = 10
MAX_LIMIT = 50

# SearXNG's error taxonomy -> the reason string its ``unresponsive_engines``
# carries. Clients display this pair; they never see Forage's internals.
_ERROR_REASONS = {
    "challenge": "CAPTCHA",
    "timeout": "timeout",
    "http": "HTTP error",
    "network": "network error",
    "parse": "parse error",
}

_bearer = HTTPBearer(auto_error=False)


def _make_auth(config: Any) -> Callable[..., None]:
    """The same gate as the native API, plus ``?api_key=`` for SearXNG clients.

    SearXNG itself has no API key, so clients that support one (LibreChat's
    ``searxngApiKey``) carry it as a query parameter. With ``auth.enabled:
    false`` (the default) nothing is checked, exactly like everywhere else.
    """

    def _auth(
        request: Request,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    ) -> None:
        if not config.auth.enabled:
            return
        token = (
            credentials.credentials
            if credentials
            else request.query_params.get("api_key")
        )
        if not key_is_valid(token, load_api_keys()):
            raise HTTPException(status_code=401, detail="Unauthorized")

    return _auth


def _result_doc(
    item: Dict[str, Any],
    position: int,
    engines: Sequence[str],
) -> Dict[str, Any]:
    """One entry of SearXNG's ``results`` array."""
    url = item.get("url") or ""
    parts = urlparse(url)
    return {
        "url": url,
        "title": item.get("title") or "",
        "content": item.get("description") or "",
        "engine": engines[0] if engines else "",
        "template": "default.html",
        "parsed_url": [
            parts.scheme,
            parts.netloc,
            parts.path,
            parts.params,
            parts.query,
            parts.fragment,
        ],
        "img_src": "",
        "thumbnail": "",
        "priority": "",
        "engines": list(engines),
        "positions": [position],
        # Rank-derived, not a relevance score. Documented as such.
        "score": max(0.1, round(1.0 - 0.05 * (position - 1), 4)),
        "category": "general",
        "publishedDate": None,
    }


def _envelope(
    query: str,
    results: List[Dict[str, Any]],
    engine_status: Dict[str, Dict[str, Any]],
    extra_unresponsive: Sequence[Sequence[str]] = (),
) -> Dict[str, Any]:
    """SearXNG's top-level JSON envelope, with no field invented beyond it."""
    ok_engines = [
        name for name, status in engine_status.items() if status.get("status") == "ok"
    ]
    unresponsive: List[List[str]] = []
    for name, status in engine_status.items():
        state = status.get("status")
        if state in ("ok", "no_results"):
            continue
        reason = (
            status.get("error")
            or _ERROR_REASONS.get(status.get("error_type") or state or "")
            or state
            or "error"
        )
        unresponsive.append([name, reason])
    unresponsive.extend([list(entry) for entry in extra_unresponsive])

    return {
        "query": query,
        "number_of_results": len(results),
        "results": results,
        "answers": [],
        "corrections": [],
        "infoboxes": [],
        "suggestions": [],
        "unresponsive_engines": unresponsive,
    }


def _normalize_engines(raw: Optional[str]) -> Tuple[List[str], List[str]]:
    """Split SearXNG's ``engines`` parameter into (usable, unknown) names."""
    wanted: List[str] = []
    unknown: List[str] = []
    for chunk in (raw or "").split(","):
        name = chunk.strip().lower()
        if not name:
            continue
        canonical = SERP_ALIASES.get(name, name)
        if canonical in SERP_ENGINES:
            if canonical not in wanted:
                wanted.append(canonical)
        else:
            unknown.append(name)
    return wanted, unknown


def build_searxng_router(
    run_search: Callable[..., Awaitable[Tuple[Dict[str, Any], str]]],
    config: Any,
) -> APIRouter:
    """Mount the SearXNG-compatible surface on top of ``run_search``.

    ``run_search(query, limit, language, engines, bypass_cache)`` returns
    ``(payload, cache_state)``, the same callable the native ``POST /search``
    and the Firecrawl shims use, so a query answered here lands in the same
    cache entries.
    """
    router = APIRouter(tags=["searxng-compat"])
    auth_dep = _make_auth(config)

    async def _handle(
        q: str,
        fmt: str,
        language: Optional[str],
        engines: Optional[str],
        categories: Optional[str],
        pageno: int,
        limit: Optional[int],
    ) -> JSONResponse:
        query = (q or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="Missing query parameter q")

        # SearXNG serves only the formats enabled in its settings file; Forage
        # serves JSON. Answering 403 is the closest match to that behaviour.
        if (fmt or "json").strip().lower() != "json":
            raise HTTPException(
                status_code=403,
                detail="Format not enabled: this instance serves format=json only",
            )

        # Only the "general" category exists here. Asking for another one must
        # not silently degrade into unrelated web results.
        asked_categories = [
            c.strip().lower() for c in (categories or "").split(",") if c.strip()
        ]
        unsupported_categories = [c for c in asked_categories if c != "general"]

        wanted, unknown = _normalize_engines(engines)
        extra_unresponsive: List[List[str]] = [
            [name, "engine not available on this instance"] for name in unknown
        ]
        extra_unresponsive += [
            [name, "category not supported on this instance"]
            for name in unsupported_categories
        ]

        # Nothing this instance can answer: empty result set, with the reason
        # in ``unresponsive_engines`` (never a fake success with web results).
        if (asked_categories and "general" not in asked_categories) or (
            engines and not wanted
        ):
            return JSONResponse(
                _envelope(query, [], {}, extra_unresponsive),
                headers={"X-Forage-Cache": "miss"},
            )

        # Forage holds one SERP page per query; there is no page 2.
        if pageno > 1:
            return JSONResponse(
                _envelope(query, [], {}, extra_unresponsive),
                headers={"X-Forage-Cache": "miss"},
            )

        page_limit = DEFAULT_LIMIT if limit is None else max(1, min(limit, MAX_LIMIT))
        lang = None if (language or "").strip().lower() in ("", "all") else language

        payload, cache_state = await run_search(
            query,
            page_limit,
            lang,
            wanted or None,
        )

        engine_status = payload.get("data", {}).get("engines") or payload.get("engines")
        if not payload.get("success"):
            # SearXNG answers 200 with ``results: []`` when every engine fails,
            # and reports the failure per engine. Do the same: an empty page is
            # what clients already know how to display.
            logger.warning(
                "SearXNG-compat search failed for %r: %s",
                query,
                payload.get("error", "unknown error"),
            )
            status = engine_status or {}
            if not status and payload.get("error"):
                extra_unresponsive.append(["forage", str(payload["error"])])
            return JSONResponse(
                _envelope(query, [], status, extra_unresponsive),
                headers={"X-Forage-Cache": cache_state},
            )

        web = payload.get("data", {}).get("web") or []
        ok_engines = [
            name
            for name, status in (engine_status or {}).items()
            if status.get("status") == "ok"
        ]
        results = [
            _result_doc(item, index + 1, ok_engines) for index, item in enumerate(web)
        ]
        return JSONResponse(
            _envelope(query, results, engine_status or {}, extra_unresponsive),
            headers={"X-Forage-Cache": cache_state},
        )

    @router.get(
        "/search",
        summary="SearXNG-compatible search (format=json)",
        description=(
            "Search with SearXNG's request parameters and JSON envelope, so a "
            "client written against SearXNG can point its instance URL at "
            "Forage. Native ``POST /search`` is untouched."
        ),
    )
    async def searxng_search(
        q: str = Query(default="", description="Search query"),
        fmt: str = Query(default="json", alias="format"),
        language: Optional[str] = Query(default=None),
        engines: Optional[str] = Query(default=None),
        categories: Optional[str] = Query(default=None),
        pageno: int = Query(default=1, ge=1),
        time_range: Optional[str] = Query(default=None),
        safesearch: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None, ge=1, le=MAX_LIMIT),
        _auth_dep: None = Depends(auth_dep),
    ) -> JSONResponse:
        # ``time_range`` and ``safesearch`` are accepted for compatibility and
        # ignored: the engines behind the SERP rendering expose no such filter.
        del time_range, safesearch
        return await _handle(q, fmt, language, engines, categories, pageno, limit)

    @router.get("/", include_in_schema=False)
    async def searxng_root(
        q: str = Query(default="", description="Search query"),
        fmt: str = Query(default="json", alias="format"),
        language: Optional[str] = Query(default=None),
        engines: Optional[str] = Query(default=None),
        categories: Optional[str] = Query(default=None),
        pageno: int = Query(default=1, ge=1),
        time_range: Optional[str] = Query(default=None),
        safesearch: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None, ge=1, le=MAX_LIMIT),
        _auth_dep: None = Depends(auth_dep),
    ) -> JSONResponse:
        # SearXNG exposes the same search on "/" and "/search".
        del time_range, safesearch
        return await _handle(q, fmt, language, engines, categories, pageno, limit)

    return router
