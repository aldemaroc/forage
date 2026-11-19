"""Own SERP search engine for Forage (alternative to the SearXNG provider).

Renders the search engine result pages (Google, Bing, Yahoo, DuckDuckGo)
through the existing browser pool (Scrapling / Playwright / Obscura) and
parses each engine's DOM with an engine-specific parser (BeautifulSoup),
so we control extraction end to end. No trafilatura/readability pipeline is
involved: this module never runs the extract content conversion.

Architecture
------------
The orchestration lives in ``search_forage()`` and follows these rules:

* Engines are tried in the configured order (``search.engines`` in the
  config, or the per-request ``engines`` list). The first engine runs alone;
  the remaining engines are only consulted when needed.
* When an engine fails (challenge, timeout, network error), we move on to
  the next engine in the list.
* When the requested limit exceeds what one engine returns (e.g. Google
  caps the first SERP at 10), the next engines are queried automatically.
* Whenever more than one engine is used for the same query, they are queried
  one at a time, in the configured order, and the search stops as soon as the
  requested limit is met. SERP renders are serialized anyway (one page, one
  pacer), so querying every engine up front only wastes renders. Only the
  primary engine retries a challenged render; a fallback that fails hands the
  search over to the next engine.
* Every engine result is classified with certainty: ``ok`` (results were
  found and parsed), ``no_results`` (the engine answered and genuinely had
  nothing), or ``error`` (challenge / timeout / http / network / parse).
  This distinction is the contract: an empty answer is not a failure, and a
  CAPTCHA is not an empty answer.

The JSON envelope keeps the Hermes web-search contract:
    {"success": true, "data": {"web": [...]}} with
    ``data.engines`` carrying per-engine status for diagnostics.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from . import metrics
from .config import ForageConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine registry
# ---------------------------------------------------------------------------

SERP_ENGINES = ("google", "bing", "yahoo", "duckduckgo", "brave")
SERP_ALIASES = {"ddg": "duckduckgo"}

# How many organic results the first SERP page is asked for (browsers are
# expensive; engines cap the first page anyway).
PAGE_SIZE = 10
# Minimum HTML size before we even try to classify. Below this the render
# almost certainly returned an error/blank page.
MIN_HTML_BYTES = 2000

# Generic markers that mean "this is an anti-bot page", independent of engine.
# These are matched against the VISIBLE text (title + body text), never against
# the raw HTML: scripts and assets routinely contain "captcha"/"challenge"
# (e.g. Cloudflare's challenge-platform) on perfectly good pages.
_GENERIC_CHALLENGE = (
    "captcha",
    "verify you are human",
    "are you a robot",
    "unusual traffic",
    "unusual activity",
    "your computer network",
    "tráfego incomum",
    "detectamos tráfego",
    "verify you're not a robot",
    "please complete the security check",
    "security check",
)

# ---------------------------------------------------------------------------
# Per-engine builders / parsers / markers
# ---------------------------------------------------------------------------


def _clean_text(node) -> str:
    if node is None:
        return ""
    return node.get_text(" ", strip=True)


# ---------------------------------------------------------------------------
# Result URL recovery
# ---------------------------------------------------------------------------
#
# Search engines do not hand out the destination URL in the DOM. Each one hides
# it a different way, and the parsers below undo it, in this order of cost:
#
# * inline unwrap (no request): the anchor carries the URL inside a redirect
#   wrapper (Google ``/url?q=``, Bing ``/ck/a?u=``, DuckDuckGo ``/l/?uddg=``,
#   Yahoo ``/RU=``);
# * redirect resolve (one HTTP request per result): the anchor carries an opaque
#   token that only the engine can decode (Google ``/goto?url=``). Handled by
#   ``SearchBrowser.resolve_redirects``;
# * breadcrumb rebuild (always available, lossy): the visible ``<cite>`` prints
#   the URL segmented by ``›``, and long paths arrive truncated with ``…``.
#
# ``url_source`` on each result says which of the three produced the URL.

# Separators a SERP puts where the URL had a slash (›, », >).
_BREADCRUMB_SPLIT = re.compile(r"\s*[\u203a\u00bb>]\s*")
_TRUNCATION_MARKS = ("\u2026", "...")  # …, ...
_DOMAIN_START = re.compile(
    r"^(?:https?://)?[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:[:/]|$)", re.IGNORECASE
)


def _cite_url(cite_text: str) -> str:
    """Rebuild a URL from the breadcrumb a SERP prints under the title.

    ``https://example.com › blog › post`` becomes
    ``https://example.com/blog/post``: joining the segments is the only way the
    path survives, since cutting at the first separator (the old behaviour) left
    nothing but the host. A trailing segment cut with ``…`` is dropped, because
    half a slug is not a URL.

    Returns "" when the text is not a URL at all: Google prints the date there
    for some cards (``há 8 meses``), and Brave prints the age for forum threads.
    """
    text = (cite_text or "").strip()
    if not text:
        return ""
    parts = [part.strip() for part in _BREADCRUMB_SPLIT.split(text) if part.strip()]
    if not parts or not _DOMAIN_START.match(parts[0]):
        return ""
    head = parts[0]
    if not head.lower().startswith(("http://", "https://")):
        head = "https://" + head
    tail = parts[1:]
    while tail and tail[-1].endswith(_TRUNCATION_MARKS):
        tail.pop()
    return head.rstrip("/") + ("/" + "/".join(tail) if tail else "")


def _absolutize(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("//"):
        return "https:" + href
    return href


def _google_wrapped_target(href: str) -> str:
    """Google's plain wrapper: ``/url?q=<target>&sa=U``."""
    if "/url?q=" not in href:
        return ""
    match = re.search(r"[?&]q=([^&]+)", href)
    return unquote(match.group(1)) if match else ""


def _bing_wrapped_target(href: str) -> str:
    """Bing wraps every organic link: ``/ck/a?...&u=a1<base64url>``.

    The token is the target URL with an ``a1`` version prefix, base64url
    encoded. Decoding it is lossless, query string included.
    """
    parsed = urlsplit(href)
    if not parsed.netloc.lower().endswith("bing.com") or "/ck/a" not in parsed.path:
        return ""
    token = (parse_qs(parsed.query).get("u") or [""])[0]
    if not token.startswith("a1"):
        return ""
    payload = token[2:]
    payload += "=" * (-len(payload) % 4)
    try:
        return base64.urlsafe_b64decode(payload).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


def _ddg_wrapped_target(href: str) -> str:
    """DuckDuckGo's html endpoint: ``/l/?uddg=<urlencoded target>``."""
    parsed = urlsplit(href)
    if not parsed.netloc.lower().endswith("duckduckgo.com") or not parsed.path.startswith("/l/"):
        return ""
    match = re.search(r"[?&]uddg=([^&]+)", href)
    return unquote(match.group(1)) if match else ""


def _yahoo_wrapped_target(href: str) -> str:
    """Yahoo's ad/redirect wrapper: ``.../RU=<urlencoded target>/RK=...``."""
    if "r.search.yahoo.com" not in href:
        return ""
    match = re.search(r"/RU=([^/]+)/", href)
    return unquote(match.group(1)) if match else ""


_WRAPPED_TARGETS = (
    _google_wrapped_target,
    _bing_wrapped_target,
    _ddg_wrapped_target,
    _yahoo_wrapped_target,
)


def _unwrap_href(href: str) -> str:
    """Destination behind an engine redirect wrapper, or "" when there is none.

    A plain ``http`` href is returned as is: not every engine wraps its links.
    """
    href = _absolutize(href)
    if not href:
        return ""
    for unwrap in _WRAPPED_TARGETS:
        target = _absolutize(unwrap(href))
        if target.startswith("http"):
            return target
    return href if href.startswith("http") else ""


def _redirect_token(href: str, base: str = "https://www.google.com") -> str:
    """Opaque redirect token that has to be resolved over HTTP (Google ``/goto``).

    The token is encrypted rather than encoded: decoding it locally does not
    work, only following it does.
    """
    href = (href or "").strip()
    if "/goto?url=" not in href:
        return ""
    return href if href.startswith("http") else urljoin(base, href)


def _result(title: str, url: str, description: str, source: str, token: str = "") -> Dict[str, str]:
    """One parsed result. ``token`` is dropped by the resolver once spent."""
    entry = {"title": title, "url": url, "description": description, "url_source": source}
    if token:
        entry["resolve"] = token
    return entry


def _build_google_url(query: str, language: str, limit: int) -> str:
    return (
        "https://www.google.com/search"
        f"?q={quote(query)}&hl={language}&num={min(limit, PAGE_SIZE)}"
    )


def _build_bing_url(query: str, language: str, limit: int) -> str:
    return (
        "https://www.bing.com/search"
        f"?q={quote(query)}&setlang={language}&count={min(limit, PAGE_SIZE)}"
    )


def _build_yahoo_url(query: str, language: str, limit: int) -> str:
    return (
        "https://search.yahoo.com/search"
        f"?p={quote(query)}&n={min(limit, PAGE_SIZE)}"
    )


def _build_ddg_url(query: str, language: str, limit: int) -> str:
    return f"https://html.duckduckgo.com/html/?q={quote(query)}"


def _build_brave_url(query: str, language: str, limit: int) -> str:
    # `source=web` skips Brave's "AI answers" tab; spellcheck off keeps the
    # query verbatim (a corrected query would change the results silently).
    return (
        "https://search.brave.com/search"
        f"?q={quote(query)}&source=web&spellcheck=0&lang={language}"
    )


def _parse_google(html: str, _language: str) -> List[Dict[str, str]]:
    """Google organic results.

    Google obfuscates the ``<a href>``: ``/goto?url=<encrypted token>`` cannot be
    decoded locally, only followed, so the parser keeps the token and the
    resolver spends it later (``_apply_redirect_resolution``). The ``<cite>``
    breadcrumb is the fallback, and the only source for cards Google renders
    without a usable link (top stories, video blocks).
    """
    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict[str, str]] = []

    def add(h3, cite_text: str, snippet: str) -> None:
        title = h3.get_text(" ", strip=True)
        if not title:
            return
        anchor = h3.find_parent("a")
        href = str(anchor.get("href", "")) if anchor else ""
        url = _unwrap_href(href)
        cite = _cite_url(cite_text)
        token = "" if url else _redirect_token(href)
        results.append(
            _result(title, url or cite, snippet, "href" if url else "cite", token)
        )

    for div in soup.select("div.MjjYud"):
        h3 = div.find("h3")
        if not h3:
            continue
        snippet = _clean_text(
            div.select_one("div.VwiC3b") or div.select_one("div.IsZvec")
        )
        add(h3, _clean_text(div.select_one("cite")), snippet)
    if results:
        return results
    # Fallback: older/newer layouts - h3 with the next <cite> sibling.
    for h3 in soup.find_all("h3"):
        add(h3, _clean_text(h3.find_next("cite")), "")
    return results


def _parse_bing(html: str, _language: str) -> List[Dict[str, str]]:
    """Bing organic results (li.b_algo). The href is a ``/ck/a`` wrapper whose
    ``u=a1<base64url>`` parameter is the exact destination: decoded inline, no
    request. The ``<cite>`` (breadcrumb, often truncated) is the fallback."""
    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict[str, str]] = []
    for li in soup.select("li.b_algo"):
        h2a = li.select_one("h2 a")
        if not h2a:
            continue
        title = h2a.get_text(" ", strip=True)
        url = _unwrap_href(str(h2a.get("href", "")))
        cite = _cite_url(_clean_text(li.select_one("cite")))
        snippet = _clean_text(
            li.select_one(".b_caption p")
            or li.select_one(".b_lineclamp4")
            or li.select_one(".b_lineclamp2")
        )
        if title:
            results.append(
                _result(title, url or cite, snippet, "href" if url else "cite")
            )
    return results


def _parse_yahoo(html: str, _language: str) -> List[Dict[str, str]]:
    """Yahoo organic results (div.compTitle, direct <a href> with the real URL).

    Yahoo only wraps ads and some redirects (``r.search.yahoo.com``...``RU=``),
    which ``_unwrap_href`` also decodes.
    """
    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict[str, str]] = []
    for block in soup.select("div.compTitle"):
        a = block.find("a", href=True)
        h3 = block.find("h3")
        if not a or not h3:
            continue  # "Searches related to" / "See results about" boxes have no h3
        title = h3.get_text(" ", strip=True)
        url = _unwrap_href(str(a["href"]))
        if not url:
            continue
        snippet = _clean_text(block.find_next("div", class_="compText"))
        results.append(_result(title, url, snippet, "href"))
    return results


def _parse_ddg(html: str, _language: str) -> List[Dict[str, str]]:
    """DuckDuckGo html endpoint results (.result__a).

    Every link is ``//duckduckgo.com/l/?uddg=<urlencoded target>``: the target is
    decoded inline, no request.
    """
    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict[str, str]] = []
    for a in soup.select("a.result__a"):
        url = _unwrap_href(str(a.get("href", "")))
        if not url:
            continue
        title = a.get_text(" ", strip=True)
        snippet = _clean_text(a.find_next("a", class_="result__snippet"))
        if title:
            results.append(_result(title, url, snippet, "href"))
    return results


def _parse_brave(html: str, _language: str) -> List[Dict[str, str]]:
    """Brave Search organic results.

    Brave renders its own markup (Svelte components, no ``<h3>``): each result
    is a ``div.snippet[data-type=web]`` holding a ``.title`` and a
    ``.generic-snippet .content`` description. The anchor href is
    ``/go/v1#<token>``: the destination lives in the fragment, which the page
    resolves in the browser and never appears in the DOM. Navigating one page per
    result to recover it costs more than the search itself, so the URL comes from
    the ``<cite>`` breadcrumb (``url_source: cite``) and may lose a truncated
    tail.
    """
    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict[str, str]] = []
    blocks = soup.select('div.snippet[data-type="web"]') or soup.select("div.snippet")
    for block in blocks:
        anchor = block.select_one("a.l1") or block.find("a", href=True)
        if anchor is None:
            continue
        title = _clean_text(block.select_one(".title")) or _clean_text(anchor)
        url = _unwrap_href(str(anchor.get("href", "")))
        cite = _cite_url(_clean_text(block.select_one("cite")))
        if not url and not cite:
            continue
        snippet = _clean_text(
            block.select_one(".generic-snippet .content")
            or block.select_one(".content")
            or block.select_one(".snippet-description")
        )
        if title:
            results.append(
                _result(title, url or cite, snippet, "href" if url else "cite")
            )
    return results


# Engine id -> (url builder, parser, engine-specific markers, wait selector).
# The wait selector is the element that only appears once the results are
# rendered: waiting for it beats a fixed sleep and returns as soon as the SERP
# is ready. Brave renders no <h3>, hence the per-engine selector.
SERP_ENGINE_SPECS = {
    "google": {
        "build": _build_google_url,
        "parse": _parse_google,
        "wait_for": "div.MjjYud, #search h3",
        "challenge": (
            "our systems have detected",
            "detectaram tráfego",
            "about this page",
            "sua rede de computadores",
        ),
        "no_results": (
            "did not match any documents",
            "não correspondeu a nenhum documento",
            "no results were found",
        ),
    },
    "bing": {
        "build": _build_bing_url,
        "parse": _parse_bing,
        "wait_for": "li.b_algo",
        "challenge": (
            "there was a problem with your request",
            "please complete the security check",
            "your request couldn't be processed",
        ),
        "no_results": ("there are no results for", "no results found"),
    },
    "yahoo": {
        "build": _build_yahoo_url,
        "parse": _parse_yahoo,
        "wait_for": "div.compTitle",
        "challenge": (
            "consent.yahoo.com",
            "unexpected activity",
            "actividad inusual",
            "comportamiento anormal",
        ),
        "no_results": (
            "we couldn't find any results",
            "não encontramos resultados",
            "we didn't find",
        ),
    },
    "duckduckgo": {
        "build": _build_ddg_url,
        "parse": _parse_ddg,
        "wait_for": "a.result__a",
        "challenge": (
            "select all squares",
            "complete the following challenge",
            "anomaly challenge",
            "puzzle",
        ),
        "no_results": ("no more results", "no results", "no se encontraron"),
    },
    "brave": {
        "build": _build_brave_url,
        "parse": _parse_brave,
        "wait_for": "div.snippet",
        "challenge": (
            "unusual activity",
            "atividade incomum",
            "please solve the challenge",
            "verify you are human",
            "this page is not available",
        ),
        "no_results": (
            "no results found",
            "didn't find any results",
            "não encontramos resultados",
            "no se encontraron resultados",
        ),
    },
}


def _classify(engine: str, html: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Return (status, error_type, detail) for a rendered SERP.

    status is one of: ``ok``, ``no_results``, ``error``.
    error_type (when status=error): ``challenge`` | ``timeout`` | ``http`` |
    ``network`` | ``parse``. detail is a human readable string.

    This is the "did the search really happen?" contract: a page that loaded
    with no organic results and no challenge markers means the engine
    answered and simply had nothing; a challenge page is an error, not an
    empty result set.

    Challenge detection uses the VISIBLE text (title + body) for generic
    markers, because bot/security words appear in scripts and asset URLs on
    perfectly good pages. Engine-specific markers are additionally checked
    against the raw HTML (they are specific enough to be reliable).
    """
    spec = SERP_ENGINE_SPECS[engine]
    if not html or not html.strip():
        return "error", "http", "Empty response from engine"

    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "").strip()
    title_low = title.lower()
    body_text = soup.get_text(" ", strip=True).lower()
    low = html.lower()

    # Challenge: generic markers in visible text; engine-specific markers in
    # visible text OR raw HTML (they are precise enough to be safe).
    challenge_hits = []
    for marker in _GENERIC_CHALLENGE:
        if marker in body_text or marker in title_low:
            challenge_hits.append(marker)
    for marker in spec["challenge"]:
        if marker in body_text or marker in title_low or marker in low:
            challenge_hits.append(marker)
    if challenge_hits:
        return "error", "challenge", "Anti-bot page: %s" % ", ".join(challenge_hits[:3])

    # No-results markers BEFORE the size floor: a genuine "no results" page is
    # small but carries an explicit marker, so it must not look like a network
    # error. Only pages WITH no markers and tiny size are treated as http errors.
    #
    # Matched against the VISIBLE text, never the raw HTML: a client-rendered
    # engine ships its whole UI as a JS bundle (Brave is a Svelte app), so
    # strings like "no results found" live in the HTML of a page full of
    # results. This is the same reasoning as the challenge markers.
    no_results_hits = [m for m in spec["no_results"] if m in body_text or m in title_low]

    if no_results_hits:
        return "no_results", None, "Engine found no results: %s" % no_results_hits[0]

    if len(html.strip()) < MIN_HTML_BYTES:
        return "error", "http", "Page too small to be a SERP (%d bytes)" % len(html)

    # No explicit markers: the parser decides. A page with zero parsed results
    # that is otherwise a real loaded page is a genuine no-results answer.
    parsed = spec["parse"](html, "")
    if parsed:
        return "ok", None, None
    if title:
        return "no_results", None, "SERP loaded (%s) but no organic results" % title[:60]
    return "error", "parse", "SERP loaded but title and results are missing"


def _normalize_url(url: str) -> str:
    url = url.strip()
    if url.startswith("//"):
        url = "https://" + url
    if url.startswith("http"):
        return url.split("#")[0].rstrip("/")
    return ""


def _dedupe_key(result: Dict[str, Any]) -> Tuple[str, str]:
    """Identity of a result for de-duplication.

    A URL taken from the anchor is exact, so the URL alone identifies the result.
    A URL rebuilt from the breadcrumb may be truncated, so the title is part of
    the key: keying on the approximate URL alone collapsed every result of the
    same host into the first one, which is how a 10-result Google SERP turned into
    a handful of bare domains.
    """
    url = _normalize_url(str(result.get("url", "")))
    if not url:
        return ("", "")
    if result.get("url_source") in ("href", "resolved"):
        return (url, "")
    return (url, str(result.get("title", "")))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _apply_redirect_resolution(
    config: Any,
    browser: Any,
    results: List[Dict[str, Any]],
    referer: str,
    engine: str = "",
) -> int:
    """Spend the redirect tokens the parsers collected (Google ``/goto``).

    Runs once the result list is final, so only URLs that are actually returned
    cost a request. Each token is one small HTTP request through the browser's
    own request context (same cookies as the SERP), never a page render, and the
    ones that miss fall back to a committed navigation. A token that cannot be
    resolved keeps the breadcrumb URL: the result still works, and ``url_source``
    says the URL is approximate.

    Returns the number of results left on the breadcrumb fallback.
    """
    pending = [r for r in results if r.get("resolve")]
    if not pending:
        return 0
    if not getattr(config.search, "resolve_urls", True):
        for result in pending:
            result.pop("resolve", None)
            result.pop("resolve_referer", None)
        return 0
    targets = [str(result["resolve"]) for result in pending]
    timeout = getattr(config.search, "resolve_timeout", 8)
    budget = getattr(config.search, "resolve_budget", 15)
    deadline = time.monotonic() + max(1, budget)
    try:
        resolved = list(
            await browser.resolve_redirects(
                targets, referer=referer, timeout=timeout, budget=budget
            )
        )
    except Exception as exc:  # noqa: BLE001 (a resolver bug must not kill the search)
        logger.warning("URL resolution failed, keeping the breadcrumb fallback: %s", exc)
        resolved = [None] * len(targets)
    # Second stage: the engine throttles the redirect endpoint after a handful of
    # hops, but a real navigation still lands. It costs a committed navigation
    # (never a full load), so it only runs for what the first stage missed and
    # only while the budget lasts.
    for index, final in enumerate(resolved):
        if final is not None or time.monotonic() >= deadline:
            continue
        resolved[index] = await browser.resolve_by_navigation(targets[index], timeout=timeout)
    for result, final in zip(pending, resolved or []):
        result.pop("resolve", None)
        result.pop("resolve_referer", None)
        if final:
            result["url"] = final
            result["url_source"] = "resolved"
    unresolved = [r for r in pending if r.get("url_source") != "resolved"]
    if unresolved:
        logger.warning(
            "%d of %d %s results kept the <cite> breadcrumb URL: the redirect token did not resolve",
            len(unresolved),
            len(pending),
            engine or "this engine",
        )
    return len(unresolved)


async def _fetch_engine(
    config: ForageConfig,
    browser: Any,
    browser_chain: List[str],
    engine: str,
    query: str,
    limit: int,
    language: str,
    retries: Optional[int] = None,
) -> Dict[str, Any]:
    """Render one SERP and parse it. Returns {status, results, error_type, error}.

    ``browser`` is the one-shot ``SearchBrowser`` of this search request.
    ``browser_chain`` is the ordered list of local browser engines to try for
    this search engine (from ``browser.search_engine_overrides``); unused in
    CDP mode, where there is only one browser behind the endpoint.

    A render that comes back as an anti-bot page is retried in place
    (``browser.search.retries`` with ``retry_backoff`` seconds between tries)
    before the next browser engine is spent: measured against Google, a
    challenged render usually succeeds on the next attempt, while switching
    browser engines costs a full launch. ``retries`` overrides the configured
    count: fallback engines are called with 0, because a fallback that fails
    must hand over to the next engine instead of spending the search budget on
    its own retries.
    """
    spec = SERP_ENGINE_SPECS[engine]
    url = spec["build"](query, language, limit)
    engine_started = time.monotonic()
    http_seconds = 0.0
    failures: List[str] = []
    chain: List[Optional[str]] = [None] if getattr(browser, "mode", "local") == "cdp" else list(browser_chain)
    retries = max(0, int(config.browser.search.retries if retries is None else retries))
    backoff = max(0.0, float(config.browser.search.retry_backoff))

    for browser_engine in chain:
        for attempt in range(retries + 1):
            if attempt:
                await asyncio.sleep(backoff)
            try:
                render_started = time.monotonic()
                html = await browser.render(
                    url,
                    timeout=config.search.serp_timeout,
                    engine=browser_engine,
                    wait_for=spec.get("wait_for"),
                )
                http_seconds += time.monotonic() - render_started
            except asyncio.TimeoutError:
                failures.append(f"{browser_engine or browser.engine}: timeout")
                continue
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{browser_engine or browser.engine}: {exc}")
                continue

            if isinstance(html, dict):  # readability render returned an article dict - not expected here
                html = html.get("content", "")

            status, error_type, detail = _classify(engine, str(html))
            if status == "error":
                # Challenge / http / parse failure: retry this browser, then
                # move to the next one in the chain.
                failures.append(f"{browser_engine or browser.engine}: {error_type} ({detail})")
                logger.warning(
                    "SERP %s classified %s on browser %s (attempt %d/%d): %s",
                    engine, error_type, browser_engine or browser.engine,
                    attempt + 1, retries + 1, detail,
                )
                continue

            results: List[Dict[str, str]] = []
            try:
                results = spec["parse"](str(html), language)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{browser_engine or browser.engine}: parse raised {exc}")
                logger.warning("SERP %s parse raised on browser %s: %s", engine, browser_engine, exc)
                continue
            for result in results:
                if result.get("resolve"):
                    result["resolve_referer"] = url
            metrics.observe_engine(
                engine,
                results=len(results),
                total=time.monotonic() - engine_started,
                http=http_seconds,
                ok=True,
            )
            return {
                "status": status,
                "error_type": error_type,
                "error": detail,
                "results": results,
            }

    # Every attempt failed.
    err = "; ".join(failures) if failures else "no browsers in chain"
    metrics.observe_engine(
        engine,
        results=0,
        total=time.monotonic() - engine_started,
        http=http_seconds,
        ok=False,
    )
    return {"status": "error", "error_type": "challenge", "error": err, "results": []}


async def search_forage(
    config: ForageConfig,
    browser: Any,
    chain_for: Callable[[str], List[str]],
    query: str,
    limit: int,
    language: Optional[str] = None,
    engines: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Search using Forage's own SERP engines (browser render + DOM parse).

    ``browser`` is the one-shot ``SearchBrowser`` opened for this request; it
    renders every engine of the search and is closed by the caller.
    ``chain_for(search_engine)`` returns the ordered fallback chain of local
    browser engines for a search engine (``browser.search_engine_overrides``);
    it is ignored in CDP mode, where a single browser sits behind the endpoint.
    """
    lang = language or config.search.default_lang

    names: List[str] = []
    for raw in (engines or list(config.search.engines)):
        name = SERP_ALIASES.get(raw, raw)
        if name in SERP_ENGINE_SPECS:
            if name not in names:
                names.append(name)
    if not names:
        return {
            "success": False,
            "error": (
                "No valid search engines configured for provider=forage "
                f"(got {list(engines or config.search.engines)}; "
                f"valid: {', '.join(SERP_ENGINES)})"
            ),
        }

    collected: List[Dict[str, Any]] = []
    seen_keys: set = set()
    statuses: Dict[str, Dict[str, Any]] = {}
    unresolved: Dict[str, int] = {}

    def _ingest(batch: List[Dict[str, Any]], engine_name: str) -> None:
        for r in batch:
            key = _dedupe_key(r)
            if not key[0] or key in seen_keys:
                continue
            seen_keys.add(key)
            r["_engine"] = engine_name
            collected.append(r)

    # 1. First engine alone (the configured order matters).
    first = names[0]
    st = await _fetch_engine(
        config,
        browser,
        chain_for(first),
        first,
        query,
        max(PAGE_SIZE, limit),
        lang,
    )
    statuses[first] = st
    _ingest(st.get("results", []), first)

    # 2. Extra engines only when needed (the first one failed or did not reach
    # the limit), one at a time and stopping as soon as the limit is met.
    # Renders are serialized anyway (one page, one pacer), so firing every
    # engine up front only adds renders whose results get thrown away.
    for name in names[1:]:
        if len(collected) >= limit:
            break
        need = limit - len(collected)
        st_rest = await _fetch_engine(
            config,
            browser,
            chain_for(name),
            name,
            query,
            max(PAGE_SIZE, need),
            lang,
            retries=0,
        )
        statuses[name] = st_rest
        _ingest(st_rest.get("results", []), name)

    collected = collected[:limit]
    for idx, r in enumerate(collected):
        r["position"] = idx + 1

    # Exact URLs last, and only for the results that are actually returned: a
    # redirect token costs a request, so spending it on a result that the limit
    # or the de-duplication would drop is waste.
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for result in collected:
        referer = result.get("resolve_referer")
        if result.get("resolve") and referer:
            groups.setdefault(str(referer), []).append(result)
    for referer, items in groups.items():
        engine_name = str(items[0].get("_engine") or "")
        left = await _apply_redirect_resolution(config, browser, items, referer, engine_name)
        if left:
            unresolved[engine_name] = unresolved.get(engine_name, 0) + left

    # Per-engine status summary (public, diagnostic).
    engines_public = {
        name: {
            "status": s.get("status", "error"),
            **({"error_type": s["error_type"]} if s.get("error_type") else {}),
            **({"error": s["error"]} if s.get("error") else {}),
            **({"url_fallback": unresolved[name]} if unresolved.get(name) else {}),
        }
        for name, s in statuses.items()
    }
    for result in collected:
        result.pop("_engine", None)
        result.pop("resolve", None)
        result.pop("resolve_referer", None)

    if not collected:
        # Distinguish "nothing found" from "everything failed".
        ok_found = any(s.get("status") == "no_results" for s in statuses.values())
        any_ok = any(s.get("status") == "ok" for s in statuses.values())
        if ok_found or any_ok:
            return {
                "success": True,
                "data": {"web": [], "engines": engines_public},
            }
        errors = ", ".join(
            f"{n}: {s.get('error_type') or s.get('status')} ({s.get('error', '')})"
            for n, s in statuses.items()
        )
        return {
            "success": False,
            "error": f"All search engines failed: {errors}",
            "engines": engines_public,
        }

    return {
        "success": True,
        "data": {"web": collected, "engines": engines_public},
    }
