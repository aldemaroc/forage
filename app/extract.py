"""Hybrid extraction: static HTTP first, Playwright browser fallback.

Decision flow (config-driven):
  1. domain in force_render_domains | force_render | wait_for  -> browser
  2. static fetch (httpx)
  3. HTTP 403/429                                          -> browser
  4. looks_like_spa(html) or trafilatura text < min_content_chars -> browser
  5. otherwise deliver the static result
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
import trafilatura

from .browser import BrowserPool
from .config import ForageConfig
from .documents import extract_document_bytes, looks_like_document

logger = logging.getLogger(__name__)

# One short retry for transient server-side errors (rate limit / 5xx blips).
# Static-only: the hybrid flow already falls back to the browser on 403/429,
# so retrying here targets brief 429/5xx spikes, not persistent blocks.
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_DELAY = 0.5
RETRY_ATTEMPTS = 2  # total attempts: 1 initial + 1 retry

SPA_MARKERS = [
    'id="root"',
    'id="app"',
    'id="__next"',
    'id="app-root"',
    'id="nuxt"',
    'id="svelte"',
    "data-reactroot",
    "ng-app=",          # attribute form only; bare "ng-app" matches "shopping-app" in prose
    "__NEXT_DATA__",
    "__NUXT__",
    "ytInitialData",  # YouTube (present in the static HTML shell)
    "ytcfg",          # YouTube config blob
]


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _domain_matches(domain: str, pattern: str) -> bool:
    pattern = pattern.lower().strip()
    if not pattern:
        return False
    return domain == pattern or domain.endswith("." + pattern)


def _in_domain_list(url: str, domains: Tuple[str, ...]) -> bool:
    domain = _domain(url)
    return any(_domain_matches(domain, d) for d in domains)


def rewrite_url(url: str, rules: Tuple[Any, ...]) -> str:
    """Apply the first matching prefix rewrite rule to a URL.

    Rule format: ``match`` is "host/path-prefix" (www-insensitive host),
    ``replace`` is the new "host[/path-prefix]". Scheme (http/https), the
    remaining path, query and fragment are preserved.

    Example: match="reddit.com/r/", replace="old.reddit.com/r/" turns
    https://www.reddit.com/r/selfhosted/comments/xyz into
    https://old.reddit.com/r/selfhosted/comments/xyz.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path or "/"
    for rule in rules:
        match = (rule.match or "").lower()
        m_host, _, m_path = match.partition("/")
        m_path = "/" + m_path if m_path else ""
        if host != m_host or not path.startswith(m_path):
            continue
        replace = (rule.replace or "").lower()
        r_host, _, r_path = replace.partition("/")
        r_path = "/" + r_path if r_path else ""
        rest = path[len(m_path):]
        if r_path:
            new_path = r_path.rstrip("/") + "/" + rest.lstrip("/") if rest else r_path.rstrip("/") + "/"
        else:
            new_path = "/" + rest.lstrip("/") if rest else "/"
        new_url = f"{parsed.scheme}://{r_host}{new_path}"
        if parsed.query:
            new_url += "?" + parsed.query
        if parsed.fragment:
            new_url += "#" + parsed.fragment
        return new_url
    return url


def looks_like_spa(html: str) -> bool:
    low = html.lower()
    return any(marker in low for marker in SPA_MARKERS)


CHALLENGE_TITLES = [
    "attention required",
    "just a moment",
    "checking your browser",
    "access denied",
    "ddos-guard",
    "sucuri",
    "website is using a security service",
]
# NOTE: "challenge-platform" is deliberately NOT a marker. Cloudflare injects
# /cdn-cgi/challenge-platform/scripts/jsd/main.js into EVERY page it serves
# (JS detections), even with no active challenge - the substring would false
# positive on any Cloudflare-backed site.
CHALLENGE_MARKERS = [
    "cf-challenge",
    "cf-browser-verification",
    "cf-error-details",
]


def looks_like_challenge(html: str, title: str) -> bool:
    """Detect anti-bot challenge pages (Cloudflare, DDoS-Guard, etc.).

    Title match is the primary signal; marker match is secondary. The
    generic word "captcha" is deliberately NOT a marker; MediaWiki and
    other sites embed it in edit/config scripts (false positive).
    """
    low_title = title.lower()
    if any(marker in low_title for marker in CHALLENGE_TITLES):
        return True
    low_html = html.lower()
    return any(marker in low_html for marker in CHALLENGE_MARKERS)


def _extract_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.S | re.I)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    return ""


async def _check_robots(client: httpx.AsyncClient, config: ForageConfig, url: str) -> Optional[str]:
    """Return an error string when robots.txt disallows the URL, else None."""
    if not config.extract.respect_robots:
        return None
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        resp = await client.get(robots_url, timeout=5)
        if resp.status_code != 200:
            return None
        path = parsed.path or "/"
        disallowed = False
        user_agent = "*"
        for raw in resp.text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                user_agent = value
            elif key == "disallow" and user_agent == "*":
                if value == "":
                    disallowed = False
                elif value == "/" or path.startswith(value):
                    disallowed = True
        if disallowed:
            return f"Blocked by robots.txt ({robots_url})"
    except httpx.RequestError:
        pass
    return None


async def _extract_document(
    config: ForageConfig,
    url: str,
    timeout: int,
) -> Optional[Dict[str, Any]]:
    """Download and extract a document (pdf/docx/xlsx/pptx/rtf).

    Returns the Hermes envelope entry when the URL yields a parseable
    document; None when it is not a document or parsing fails, so the
    caller falls through to the normal hybrid flow.
    """
    headers = {
        "User-Agent": config.extract.user_agent,
        "Accept": "*/*",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
    except httpx.RequestError as exc:
        logger.warning("Document download failed for %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        logger.info("%s -> HTTP %d, falling back to hybrid", url, resp.status_code)
        return None
    content_type = resp.headers.get("content-type", "")
    if not looks_like_document(url, content_type):
        return None
    try:
        text, title, method_label = extract_document_bytes(
            resp.content,
            url,
            content_type=content_type,
            max_chars=config.extract.max_content_chars,
        )
    except ValueError as exc:
        logger.info("%s -> document parse failed (%s), falling back to hybrid", url, exc)
        return None
    if config.extract.raw_content_markdown:
        raw_content = text
    else:
        raw_content = ""
    return {
        "url": url,
        "title": title,
        "content": text,
        "raw_content": raw_content,
        "method": method_label,
    }


async def fetch_static(
    config: ForageConfig,
    url: str,
) -> Tuple[Optional[str], int, str]:
    """Fetch URL with plain HTTP. Returns (html, status, final_url)."""
    headers = {
        "User-Agent": config.extract.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=config.extract.timeout) as client:
        robots_error = await _check_robots(client, config, url)
        if robots_error:
            return None, 0, url  # caller treats 0 as blocked-by-robots
        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code in RETRY_STATUS and attempt < RETRY_ATTEMPTS - 1:
                    logger.info(
                        "%s -> HTTP %d (transient), retrying in %.1fs",
                        url, resp.status_code, RETRY_DELAY,
                    )
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                return resp.text, resp.status_code, str(resp.url)
            except httpx.RequestError as exc:
                if attempt < RETRY_ATTEMPTS - 1:
                    logger.info("%s -> request error, retrying in %.1fs: %s", url, RETRY_DELAY, exc)
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                logger.warning("Static fetch failed for %s: %s", url, exc)
                return None, 0, url
        return None, 0, url


def _plain_text(html: str) -> str:
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def _extract_text(html: str, only_main_content: bool, max_chars: int) -> str:
    if only_main_content:
        text = trafilatura.extract(
            html,
            output_format="markdown",  # structured markdown (headings, bold, lists)
            include_comments=False,
            include_tables=True,
            favor_precision=False,
        )
        text = text or ""
    else:
        text = _plain_text(html)
    return text[:max_chars]


async def extract_url(
    config: ForageConfig,
    pool: BrowserPool,
    url: str,
    *,
    force_render: bool = False,
    wait_for: Optional[str] = None,
    output_format: str = "markdown",
    only_main_content: bool = True,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """Extract a single URL using the hybrid strategy. Hermes-envelope entry."""
    effective_timeout = timeout or config.extract.timeout
    method = "static"

    original_url = url
    rewritten = rewrite_url(url, config.extract.url_rewrites)
    if rewritten != url:
        logger.info("%s -> URL rewritten to %s", url, rewritten)
        url = rewritten

    # Forums and comment-heavy sites: trafilatura treats comments as non-main
    # content and drops them. full_text_domains forces the whole-page path.
    effective_main = only_main_content and not _in_domain_list(
        url, config.extract.full_text_domains
    )

    # Documents (pdf/docx/xlsx/pptx/rtf) are extracted from raw bytes -
    # never through the browser (Chromium renders PDFs poorly). Falls back
    # to the hybrid flow when the URL is not actually a document.
    if not force_render:
        doc_result = await _extract_document(config, url, effective_timeout)
        if doc_result is not None:
            doc_result["url"] = original_url
            return doc_result

    want_browser = (
        force_render
        or bool(wait_for)
        or _in_domain_list(url, config.extract.force_render_domains)
    )

    html: Optional[str] = None
    status = 0

    if not want_browser:
        html, status, _ = await fetch_static(config, url)
        if status == 0:
            # network error or robots-blocked; browser rarely helps, fail clean
            return {"url": original_url, "error": "Failed to fetch URL (network error or robots.txt)"}
        if status in (401, 403, 429):
            logger.info("%s -> HTTP %d, falling back to browser", url, status)
            want_browser = True
        elif html is not None and looks_like_challenge(html, _extract_title(html)):
            # Some anti-bot setups (e.g. Cloudflare managed challenge) answer
            # 200 with a challenge page. Give the browser a shot before failing.
            logger.info("%s -> static anti-bot challenge page, falling back to browser", url)
            want_browser = True

    if want_browser:
        try:
            html = await pool.render(url, wait_for=wait_for, timeout=effective_timeout)
            method = "browser"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Browser render failed for %s: %s", url, exc)
            if html is None:
                return {"url": original_url, "error": f"Browser render failed: {exc}"}
            # static html (if any) is still better than nothing

    if html is None:
        return {"url": original_url, "error": "No content extracted"}

    # Hybrid analysis on the static HTML (only relevant when not forced to browser)
    if not want_browser:
        text = _extract_text(html, effective_main, config.extract.max_content_chars)
        if looks_like_spa(html) or len(text) < config.extract.min_content_chars:
            logger.info("%s -> low content (%d chars), falling back to browser", url, len(text))
            try:
                html = await pool.render(url, wait_for=wait_for, timeout=effective_timeout)
                method = "browser"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Browser render failed for %s: %s", url, exc)

    if output_format == "html":
        content = html
        raw_content = html[: config.extract.max_content_chars]
    else:
        content = _extract_text(html, effective_main, config.extract.max_content_chars)
        # By default (raw_content_markdown), raw_content mirrors the clean
        # markdown; matches Firecrawl's contract, which Hermes' web_extract
        # tool relies on (it reads raw_content first). Disable to keep the
        # raw HTML in raw_content instead.
        if config.extract.raw_content_markdown:
            raw_content = content
        else:
            raw_content = html[: config.extract.max_content_chars]

    if not content:
        return {"url": original_url, "error": "No content extracted"}

    title = _extract_title(html)
    if looks_like_challenge(html, title):
        if config.browser.fallback_solver:
            # Last-resort retry: the scrapling built-in solver handles
            # challenges (including interactive ones) that the page_action
            # poll cannot. Only pays the ~5s/page solver cost on failure.
            logger.info("%s -> anti-bot challenge, retrying with scrapling solver", url)
            try:
                solver_html = await pool.render_with_solver(url, wait_for=wait_for, timeout=effective_timeout)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Solver retry failed for %s: %s", url, exc)
                solver_html = None
            if solver_html:
                html = solver_html
                method = "browser+solver"
                title = _extract_title(html)
                if output_format == "html":
                    content = html
                    raw_content = html[: config.extract.max_content_chars]
                else:
                    content = _extract_text(html, effective_main, config.extract.max_content_chars)
                    raw_content = content if config.extract.raw_content_markdown else html[: config.extract.max_content_chars]
                if not content:
                    return {"url": original_url, "error": "No content extracted"}
        if looks_like_challenge(html, title):
            logger.warning("%s -> anti-bot challenge page detected%s", url, " (after solver retry)" if method == "browser+solver" else "")
            return {
                "url": original_url,
                "title": title,
                "method": method,
                "error": "Blocked by anti-bot challenge (Cloudflare or similar)",
            }

    result: Dict[str, Any] = {
        "url": original_url,
        "title": title,
        "content": content,
        "raw_content": raw_content,
        "method": method,
    }
    if url != original_url:
        result["rewritten_url"] = url
    return result
