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
* Whenever more than one engine is used for the same query, they run in
  parallel (``asyncio.gather``); the browser pool semaphore bounds the
  concurrency to ``browser.max_instances``.
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
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from bs4 import BeautifulSoup

from .config import ForageConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine registry
# ---------------------------------------------------------------------------

SERP_ENGINES = ("google", "bing", "yahoo", "duckduckgo")
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


def _visible_url(cite_text: str) -> str:
    """Take a search-engine visible URL (may contain breadcrumb ' › ')."""
    for sep in (" › ", " > ", " ›", "\u203a"):
        if sep in cite_text:
            cite_text = cite_text.split(sep)[0]
    return cite_text.strip()


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


def _parse_google(html: str, _language: str) -> List[Dict[str, str]]:
    """Google organic results.

    Google obfuscates the <a href> (``/goto?url=``), but the visible URL in
    ``<cite>`` is the real destination and parses cleanly.
    """
    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict[str, str]] = []
    for div in soup.select("div.MjjYud"):
        h3 = div.find("h3")
        if not h3:
            continue
        title = h3.get_text(" ", strip=True)
        cite = _visible_url(_clean_text(div.select_one("cite")))
        snippet = _clean_text(
            div.select_one("div.VwiC3b") or div.select_one("div.IsZvec")
        )
        if title:
            results.append(
                {"title": title, "url": cite or "", "description": snippet}
            )
    if results:
        return results
    # Fallback: older/newer layouts - h3 with the next <cite> sibling.
    for h3 in soup.find_all("h3"):
        title = h3.get_text(" ", strip=True)
        cite = _visible_url(_clean_text(h3.find_next("cite")))
        if title:
            results.append(
                {"title": title, "url": cite or "", "description": ""}
            )
    return results


def _parse_bing(html: str, _language: str) -> List[Dict[str, str]]:
    """Bing organic results (li.b_algo). href is a /ck/a redirect wrapper,
    so the visible URL comes from <cite> (clean when no breadcrumb)."""
    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict[str, str]] = []
    for li in soup.select("li.b_algo"):
        h2a = li.select_one("h2 a")
        if not h2a:
            continue
        title = h2a.get_text(" ", strip=True)
        cite = _visible_url(_clean_text(li.select_one("cite")))
        snippet = _clean_text(
            li.select_one(".b_caption p")
            or li.select_one(".b_lineclamp4")
            or li.select_one(".b_lineclamp2")
        )
        if title:
            results.append(
                {"title": title, "url": cite or "", "description": snippet}
            )
    return results


def _parse_yahoo(html: str, _language: str) -> List[Dict[str, str]]:
    """Yahoo organic results (div.compTitle, direct <a href> with real URL)."""
    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict[str, str]] = []
    for block in soup.select("div.compTitle"):
        a = block.find("a", href=True)
        h3 = block.find("h3")
        if not a or not h3:
            continue  # "Searches related to" / "See results about" boxes have no h3
        href = a["href"]
        if not href.startswith("http"):
            continue
        title = h3.get_text(" ", strip=True)
        snippet = _clean_text(block.find_next("div", class_="compText"))
        results.append(
            {"title": title, "url": href, "description": snippet}
        )
    return results


def _parse_ddg(html: str, _language: str) -> List[Dict[str, str]]:
    """DuckDuckGo html endpoint results (.result__a, direct href)."""
    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict[str, str]] = []
    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        if href.startswith("//"):
            href = "https:" + href
        if not href.startswith("http"):
            continue
        title = a.get_text(" ", strip=True)
        snippet = _clean_text(a.find_next("a", class_="result__snippet"))
        if title:
            results.append(
                {"title": title, "url": href, "description": snippet}
            )
    return results


# Engine id -> (url builder, parser, engine-specific markers)
# Extra markers are ORed with the generic challenge list.
SERP_ENGINE_SPECS = {
    "google": {
        "build": _build_google_url,
        "parse": _parse_google,
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
        "challenge": (
            "select all squares",
            "complete the following challenge",
            "anomaly challenge",
            "puzzle",
        ),
        "no_results": ("no more results", "no results", "no se encontraron"),
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
    no_results_hits = [m for m in spec["no_results"] if m in low]
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
        url = "https:" + url
    if url.startswith("http"):
        return url.split("#")[0].rstrip("/")
    return ""


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _fetch_engine(
    config: ForageConfig,
    pool_for: Callable[[str, str], Any],
    browser_chain: List[str],
    engine: str,
    query: str,
    limit: int,
    language: str,
) -> Dict[str, Any]:
    """Render one SERP and parse it. Returns {status, results, error_type, error}.

    ``browser_chain`` is the ordered list of browser engines to try for this
    search engine: if the first browser hits an anti-bot challenge (or a
    network/parse error), the next browser in the chain is tried, until one
    yields a usable answer (ok / no_results) or the chain is exhausted.
    """
    spec = SERP_ENGINE_SPECS[engine]
    url = spec["build"](query, language, limit)
    failures: List[str] = []

    for browser in browser_chain:
        try:
            html = await pool_for(engine, browser).render(
                url,
                timeout=config.search.serp_timeout,
                network_idle_timeout=config.browser.network_idle_timeout,
            )
        except asyncio.TimeoutError:
            failures.append(f"{browser}: timeout")
            continue
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{browser}: {exc}")
            continue

        if isinstance(html, dict):  # readability render returned an article dict - not expected here
            html = html.get("content", "")

        status, error_type, detail = _classify(engine, str(html))
        if status == "error":
            # Challenge / http / parse failure on this browser: try the next one.
            failures.append(f"{browser}: {error_type} ({detail})")
            logger.warning("SERP %s classified %s on browser %s: %s", engine, error_type, browser, detail)
            continue

        results: List[Dict[str, str]] = []
        try:
            results = spec["parse"](str(html), language)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{browser}: parse raised {exc}")
            logger.warning("SERP %s parse raised on browser %s: %s", engine, browser, exc)
            continue
        return {"status": status, "error_type": error_type, "error": detail, "results": results}

    # All browsers in the chain failed.
    err = "; ".join(failures) if failures else "no browsers in chain"
    return {"status": "error", "error_type": "challenge", "error": err, "results": []}


async def search_forage(
    config: ForageConfig,
    pool_for: Callable[[str, str], Any],
    chain_for: Callable[[str], List[str]],
    query: str,
    limit: int,
    language: Optional[str] = None,
    engines: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Search using Forage's own SERP engines (browser render + DOM parse).

    ``pool_for(search_engine, browser)`` returns the browser pool that renders a
    given search engine SERP with a specific browser engine.
    ``chain_for(search_engine)`` returns the ordered fallback chain of browser
    engines for a search engine (per-engine overrides in
    ``browser.search_engine_overrides``).
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
    seen_urls = set()
    statuses: Dict[str, Dict[str, Any]] = {}

    def _ingest(batch: List[Dict[str, Any]]) -> None:
        for r in batch:
            u = _normalize_url(r.get("url", ""))
            if not u or u in seen_urls:
                continue
            seen_urls.add(u)
            collected.append(r)

    # 1. First engine alone (the configured order matters).
    first = names[0]
    st = await _fetch_engine(
        config, pool_for, chain_for(first), first, query, max(PAGE_SIZE, limit), lang
    )
    statuses[first] = st
    _ingest(st.get("results", []))

    # 2. Extra engines only when needed (failed or limit not reached), in parallel.
    if len(collected) < limit and len(names) > 1:
        need = limit - len(collected)
        rest = await asyncio.gather(
            *(
                _fetch_engine(
                    config, pool_for, chain_for(name), name, query, max(PAGE_SIZE, need), lang
                )
                for name in names[1:]
            )
        )
        for name, st_rest in zip(names[1:], rest):
            statuses[name] = st_rest
            _ingest(st_rest.get("results", []))
            if len(collected) >= limit:
                break  # gather already launched all; just stop ingesting

    collected = collected[:limit]
    for idx, r in enumerate(collected):
        r["position"] = idx + 1

    # Per-engine status summary (public, diagnostic).
    engines_public = {
        name: {
            "status": s.get("status", "error"),
            **({"error_type": s["error_type"]} if s.get("error_type") else {}),
            **({"error": s.get("error")} if s.get("error") else {}),
        }
        for name, s in statuses.items()
    }

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
