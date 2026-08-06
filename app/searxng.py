"""SearXNG client for Forage search.

Calls the configured SearXNG instance (/search?format=json) and normalizes
the results into the Hermes web-search envelope:

    {"success": true, "data": {"web": [{title, url, description, position}]}}
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from .config import ForageConfig

logger = logging.getLogger(__name__)


def search_searxng(
    config: ForageConfig,
    query: str,
    limit: int,
    language: Optional[str] = None,
    engines: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Execute a search against SearXNG and return the Hermes envelope."""
    base = config.search.searxng_url.rstrip("/")
    params: Dict[str, Any] = {
        "q": query,
        "format": "json",
        "pageno": 1,
    }
    if language:
        params["language"] = language
    if engines:
        params["engines"] = ",".join(engines)

    try:
        resp = httpx.get(
            f"{base}/search",
            params=params,
            timeout=config.search.timeout,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning("SearXNG HTTP error: %s", exc)
        return {
            "success": False,
            "error": f"SearXNG returned HTTP {exc.response.status_code}",
        }
    except httpx.RequestError as exc:
        logger.warning("SearXNG request error: %s", exc)
        return {
            "success": False,
            "error": f"Could not reach SearXNG at {base}: {exc}",
        }

    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("SearXNG response parse error: %s", exc)
        return {"success": False, "error": "Could not parse SearXNG response as JSON"}

    raw_results = data.get("results", [])
    # Rank by SearXNG score, then cap to requested limit.
    sorted_results = sorted(
        raw_results,
        key=lambda r: float(r.get("score", 0) or 0),
        reverse=True,
    )[:limit]

    web = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "description": r.get("content", ""),
            "position": idx + 1,
        }
        for idx, r in enumerate(sorted_results)
    ]
    return {"success": True, "data": {"web": web}}
