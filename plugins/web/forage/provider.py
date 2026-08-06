"""Forage web search + extract (Hermes plugin form).

Subclasses :class:`agent.web_search_provider.WebSearchProvider` and talks to
the self-hosted Forage service over local HTTP. Supports both search and
extract (the hybrid static/browser strategy lives server-side).

Config keys this provider responds to::

    web:
      search_backend: "forage"
      extract_backend: "forage"

Env vars::

    FORAGE_URL=http://localhost:3672       # base URL of the Forage service
    FORAGE_API_KEY=...                     # optional Bearer key (auth.enabled)
    FORAGE_BYPASS_CACHE=true|false         # optional: always bypass cache
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)


def _env(key: str, default: str = "") -> str:
    try:
        from hermes_cli.config import get_env_value

        val = get_env_value(key)
    except Exception:  # noqa: BLE001
        val = None
    if val is None:
        val = os.getenv(key, "")
    return (val or default).strip()


def _forage_url() -> str:
    return _env("FORAGE_URL", "http://localhost:3672")


def _forage_api_key() -> Optional[str]:
    return _env("FORAGE_API_KEY") or None


def _bypass_cache() -> bool:
    return _env("FORAGE_BYPASS_CACHE", "").lower() in ("1", "true", "yes")


class ForageWebSearchProvider(WebSearchProvider):
    """Search + extract via the self-hosted Forage service."""

    @property
    def name(self) -> str:
        return "forage"

    @property
    def display_name(self) -> str:
        return "Forage"

    def is_available(self) -> bool:
        return bool(_forage_url())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = _forage_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if _bypass_cache():
            headers["Cache-Control"] = "no-cache"
        return headers

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Search via Forage; returns the Hermes web-search envelope."""
        base = _forage_url().rstrip("/")
        try:
            resp = httpx.post(
                f"{base}/search",
                json={"query": query, "limit": max(1, min(int(limit), 50))},
                headers=self._headers(),
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            return {"success": False, "error": f"Forage returned HTTP {exc.response.status_code}"}
        except httpx.RequestError as exc:
            return {"success": False, "error": f"Could not reach Forage at {base}: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}
        if not data.get("success"):
            return {"success": False, "error": data.get("error", "Forage search failed")}
        return data

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract URLs via Forage; returns a per-URL result list.

        The web_extract_tool expects the raw list (``{"results": [...]}``
        wrapper is applied by the tool itself), so we return ``data``
        directly, NOT the service envelope.
        """
        base = _forage_url().rstrip("/")
        fmt = kwargs.get("format") or "markdown"
        formats = ["html"] if fmt == "html" else ["markdown"]
        url_list = list(urls)
        error_items = lambda msg: [  # noqa: E731
            {"url": u, "title": "", "content": "", "raw_content": "", "error": msg}
            for u in url_list
        ]
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{base}/extract",
                    json={"urls": url_list, "formats": formats},
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            return error_items(f"Forage returned HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return error_items(f"Could not reach Forage at {base}: {exc}")
        except Exception as exc:  # noqa: BLE001
            return error_items(str(exc))
        if not data.get("success"):
            return error_items(data.get("error", "Forage extract failed"))
        # The service itself honors extract.raw_content_markdown (default
        # true), so raw_content already mirrors the clean markdown; no
        # realignment needed here.
        return data.get("data", [])

    async def full_extract(
        self,
        urls: List[str],
        format: str = "markdown",
        force_render: bool = False,
        wait_for: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Extract FULL page text (only_main_content=false) for one or more URLs.

        Backs the ``forage_full_extract`` agent tool: live per-call control to
        keep comments / forum content that trafilatura would drop as non-main.
        Same Hermes per-URL result list contract as :meth:`extract`.
        """
        base = _forage_url().rstrip("/")
        fmt = format or "markdown"
        formats = ["html"] if fmt == "html" else ["markdown"]
        payload: Dict[str, Any] = {
            "urls": list(urls),
            "formats": formats,
            "only_main_content": False,
        }
        if force_render:
            payload["force_render"] = True
        if wait_for:
            payload["wait_for"] = wait_for
        if timeout:
            payload["timeout"] = int(timeout)

        error_items = lambda msg: [  # noqa: E731
            {"url": u, "title": "", "content": "", "raw_content": "", "error": msg}
            for u in urls
        ]
        try:
            async with httpx.AsyncClient(timeout=timeout or 60) as client:
                resp = await client.post(
                    f"{base}/extract",
                    json=payload,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            return error_items(f"Forage returned HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return error_items(f"Could not reach Forage at {base}: {exc}")
        except Exception as exc:  # noqa: BLE001
            return error_items(str(exc))
        if not data.get("success"):
            return error_items(data.get("error", "Forage extract failed"))
        return data.get("data", [])
