"""Forage web search + extract plugin (user plugin).

Talks to the self-hosted Forage service (FORAGE_URL) for both web_search
and web_extract, and exposes the ``forage_full_extract`` agent tool for
live full-page extraction (only_main_content=false) on demand.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from .provider import ForageWebSearchProvider

_FULL_EXTRACT_SCHEMA: Dict[str, Any] = {
    "name": "forage_full_extract",
    "description": (
        "Extract FULL page text (only_main_content=false) from one or more URLs "
        "via the self-hosted Forage service. Use when the default web_extract "
        "misses content (forum comments, thread replies, review sections) or "
        "whenever the full page text is wanted instead of the main article. "
        "Returns per-URL {url, title, content, raw_content, method} entries."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "URLs to extract (full text).",
            },
            "format": {
                "type": "string",
                "enum": ["markdown", "html"],
                "default": "markdown",
                "description": "Output format.",
            },
            "force_render": {
                "type": "boolean",
                "default": False,
                "description": "Force browser (JS) rendering instead of static fetch.",
            },
            "wait_for": {
                "type": "string",
                "description": "Optional CSS selector to wait for before extracting (browser mode).",
            },
            "timeout": {
                "type": "integer",
                "description": "Override the per-URL timeout in seconds.",
            },
        },
        "required": ["urls"],
    },
}


async def _full_extract_handler(args: Dict[str, Any], **kwargs: Any) -> str:
    """Registry handler: run full-page extraction and return JSON string."""
    urls = args.get("urls") or []
    if not urls:
        return json.dumps({"success": False, "error": "urls is required"})
    provider = ForageWebSearchProvider()
    try:
        results = await provider.full_extract(
            urls=urls,
            format=args.get("format", "markdown"),
            force_render=bool(args.get("force_render", False)),
            wait_for=args.get("wait_for"),
            timeout=args.get("timeout"),
        )
        return json.dumps({"success": True, "data": results}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"success": False, "error": str(exc)})


def register(ctx) -> None:
    """Register the Forage provider and the forage_full_extract agent tool."""
    ctx.register_web_search_provider(ForageWebSearchProvider())
    ctx.register_tool(
        name="forage_full_extract",
        toolset="forage",
        schema=_FULL_EXTRACT_SCHEMA,
        handler=_full_extract_handler,
        is_async=True,
        description="Full-page text extraction via Forage (only_main_content=false).",
        emoji="🐔",
    )
