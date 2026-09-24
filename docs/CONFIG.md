# Forage Configuration Reference

All behavior is configured in `config.yaml`, bind-mounted read-only into the container at `/etc/forage/config.yaml`. Secrets **never** go in the YAML: use environment variables (see the table at the end).

**Reload**: after changing `config.yaml`, run `docker compose restart`. There is no hot reload by design: the config decides which processes (e.g. the browser pool) start, so a restart is the safe way to apply changes. Downtime is 1-2 seconds.

Resolution order: `built-in defaults → config.yaml → environment variables`.

---

## `server`

| Key | Default | Description |
|---|---|---|
| `host` | `0.0.0.0` | Bind address inside the container. Bridge networking requires `0.0.0.0` (docker-proxy routes to it); the real exposure is controlled by the compose `ports:` mapping. |
| `port` | `3672` | HTTP port (T9 of "FORA"). The app reads the config before starting uvicorn, so this is honored without rebuilding. |
| `workers` | `2` | uvicorn worker processes. |
| `log_level` | `info` | `debug` \| `info` \| `warning` \| `error` |

## `cache`

In-memory LRU only (lost on restart, by design). The master switch `enabled` turns everything off; per-section toggles can only disable further.

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Master switch. `false` disables all caching regardless of section toggles. |
| `max_entries` | `500` | Global LRU cap across both caches. |
| `search.enabled` | `true` | Cache search results. |
| `search.ttl` | `300` | Search TTL in seconds (5 min). Keeps repeated queries from hammering SearXNG's engines (anti-bot protection). |
| `extract.enabled` | `false` | Extract caching off by default: extraction is always fresh (Firecrawl behavior). A short TTL (e.g. `120`) gives fast repeat extraction with bounded staleness. |
| `extract.ttl` | `3600` | Extract TTL in seconds (1 h). Pre-defined so enabling the toggle is enough. |

Bypass per request with the `Cache-Control: no-cache` header; the response header `X-Forage-Cache` reports `hit|miss|bypass|disabled`.

## `search`

| Key | Default | Description |
|---|---|---|
| `searxng_url` | `http://searxng:8080` | Base URL of the SearXNG instance. On Docker, use the service name on the shared network (see docs/SEARXNG.md). Only used when `provider: searxng`. |
| `default_lang` | `pt-BR` | Language passed to SearXNG / used as `hl`/`setlang` on the SERP URLs (provider=forage). |
| `engines` | `[google, bing, duckduckgo, brave]` | Engine list. With `provider: forage` it is the ordered list of Forage's own SERP engines (valid: `google`, `bing`, `yahoo`, `duckduckgo` / alias `ddg`, `brave`). Order matters: the first engine runs alone; the rest are queried only when it fails or the limit is not met, and then in parallel. With `provider: searxng` it is the SearXNG engine filter. |
| `timeout` | `15` | Timeout in seconds per search request (provider=searxng). |
| `provider` | `forage` | Search backend. `forage` (default) uses Forage's own SERP engines (browser render + DOM parse per engine, no SearXNG needed); `searxng` aggregates through the SearXNG instance. Each engine's result is classified `ok` / `no_results` / `error` (challenge, timeout, http, network or parse), so a CAPTCHA page is never mistaken for an empty answer and vice versa. |
| `serp_timeout` | `20` | Seconds per SERP render (provider=forage). |

## `extract`

| Key | Default | Description |
|---|---|---|
| `timeout` | `30` | Total seconds budget per URL (applies to static fetch and browser render). |
| `max_content_chars` | `100000` | Cap on extracted content size. |
| `only_main_content` | `true` | Strip navigation/ads/footer (trafilatura main-content extraction). |
| `engine` | `readability` | Extract engine: `readability` (default; Readability.js + markdownify) or `trafilatura`. `readability` runs Mozilla Readability.js inside the browser page (`page.evaluate`, no Node runtime) and converts the article with markdownify, keeping buyboxes/comments that trafilatura drops as non-main. `trafilatura` is the lighter plain-HTTP main-content extractor. Either way the engine only applies to pages that already need a browser render: it never forces one. Can also be set per request or per domain override. |
| `user_agent` | `ForageBot/0.1 (+https://github.com/aldemaroc/forage)` | User-Agent for the **static** fetch (httpx). |
| `browser_user_agent` | `null` (commented) | User-Agent for the **browser** (Playwright). When unset, a real Chrome desktop UA is used (never a bot UA; it would be a giveaway). |
| `respect_robots` | `false` | Whether to honor `robots.txt`. Default is **false** (do not respect). |
| `force_render` | `false` | Always use the browser for extraction (skip the static attempt). Can also be set per request. |
| `wait_for` | `null` | CSS selector to wait for before extracting (browser mode). |
| `min_content_chars` | `200` | If static extraction yields less text than this, Forage falls back to the browser. |
| `raw_content_markdown` | `true` | `true`: `raw_content` mirrors the clean markdown (Firecrawl-style contract; what Hermes' `web_extract_tool` reads first). `false`: `raw_content` keeps the raw HTML. |
| `prefer_markdown` | `true` | Negotiate `Accept: text/markdown` on the static fetch. When the server implements markdown negotiation (e.g. via `.htaccess` / `Vary: Accept`) and serves `text/markdown`, Forage uses the body directly as markdown (`method: "markdown"`), skipping trafilatura conversion. Servers without negotiation ignore the Accept and return HTML, so the normal hybrid flow continues. |
| `domain_overrides` | `{}` | Per-pattern extraction overrides. The YAML key is a pattern (www-insensitive, case-insensitive): `x.com` matches the host or any subdomain; `.x.com` is the same with an explicit leading dot; `amazon.*` is a wildcard on a host label (fnmatch) that matches the host and any subdomain suffix; `reddit.com/r/` requires an exact host plus a path prefix. Supported keys per override: `force_render` (bool), `full_text` (bool), `engine` (str: `trafilatura` or `readability`), `wait_for` (str), `url_rewrite` (str, format `host[/path]`), `scroll` (bool), `timeout` (int 1-120), `network_idle_timeout` (int 0-60), `challenge_timeout` (int 0-120). Request-level `force_render`/`wait_for`/`timeout`/`engine` are absolute and override the domain override. |

Example: serve Reddit threads/profiles from the classic UI (comments are server-side there) and keep the Amazon buybox (price arrives via JS; trafilatura drops it as non-main):

```yaml
extract:
  domain_overrides:
    ".amazon.*":              # all Amazon TLDs + subdomains
      force_render: true      # price arrives via JS after load
      engine: readability     # Readability.js keeps the buybox AND returns
                              # structured markdown (full_text gives plain text)
    reddit.com/r/:            # subreddits / threads
      url_rewrite: "old.reddit.com/r/"
    reddit.com/u/:            # user profiles (old format)
      url_rewrite: "old.reddit.com/u/"
    reddit.com/user/:         # user profiles (new UI uses /user/)
      url_rewrite: "old.reddit.com/u/"
    reddit.com:               # after rewrite, keep comments
      full_text: true
    youtube.com:
      force_render: true
      scroll: true            # comments mount on scroll
```

`https://www.reddit.com/r/selfhosted/comments/abc` → fetched as `https://old.reddit.com/r/selfhosted/comments/abc`; the envelope reports `url` as the original and `rewritten_url` as the fetched one.

### Hybrid decision flow

```
domain override force_render | request force_render | wait_for → browser
fetch statically
status 401/403/429                                        → browser
HTML looks like a SPA (#root, __NEXT_DATA__, data-reactroot…) → browser
trafilatura text < min_content_chars                      → browser
otherwise                                                 → static result
```

## `browser`

| Key | Default | Description |
|---|---|---|
| `engine` | `scrapling` | Browser engine: `scrapling` (default; fingerprint impersonation + Cloudflare Turnstile bypass, strongest anti-bot), `playwright` (vanilla), `patchright` (anti-detection fork of Playwright, same API) or `chrome-local` (the host's real desktop Chrome via CDP, used to pass anti-bot that blocks every headless browser, e.g. Google's "unusual traffic" challenge). Switching engine only needs a config change and `docker compose restart`. |
| `cdp_url` | `""` | CDP endpoint for `engine: chrome-local` (or the experimental `obscura`). For the host Chrome it is `http://172.20.0.1:9222` (the Docker gateway, see `docs/CHROME_LOCAL.md`). |
| `search_engine_overrides` | `{}` | Provider=forage only. Maps a search engine id (`google`, `bing`, `yahoo`, `duckduckgo`) to a browser engine id or an **ordered list** used as a fallback chain: if one browser is answered with an anti-bot page, the next one is tried. Only `playwright`/`patchright` are honored (they are the engines that can be launched per search); `scrapling` keeps a session of its own and `obscura`/`chrome-local` are CDP endpoints, configured for search as a whole through `browser.search.cdp_url`. Example `google: [playwright, patchright]`. |
| `min_idle` | `1` | Browsers kept warm at boot (standby). `0` = lazy (launch on demand). |
| `max_instances` | `5` | Pool ceiling; also the browser concurrency bound for parallel URL extraction. |
| `idle_timeout` | `60` | Seconds an idle instance stays alive before it is closed. |
| `headless` | `true` | **Extract** browser: run Chromium headless. The search browser has its own `browser.search.headless`. |
| `launch_timeout` | `30` | Seconds to launch a new instance. |
| `stealth` | `true` | Hide automation signals (anti-bot). Adds `--disable-blink-features=AutomationControlled`, an init script masking `navigator.webdriver`/`chrome`/`languages`/`plugins`, and a real Chrome UA. |
| `network_idle_timeout` | `5` | Seconds cap for the `networkidle` wait during render. Pages with streaming/websockets (e.g. X) never go idle, so this cap bounds the render time; lower it for faster worst-case extraction, raise it if pages need more time to hydrate via XHR. |
| `scroll_steps` | `0` | Scroll-to-bottom passes in browser mode before extracting. Triggers lazy-loaded content (YouTube/Reddit comments mount only when scrolled into view). **Off by default**: it adds ~6s on browser pages that don't grow; enable per-instance only when lazy comments are needed. |
| `challenge_timeout` | `15` | Max seconds to wait for a Cloudflare/Turnstile challenge to auto-resolve after load. Only used by the `scrapling` engine (polling inside `page_action`). |
| `solve_cloudflare` | `false` | `scrapling` engine only. `false` (default) uses Forage's own title-poll in `page_action`, which resolves non-interactive challenges with no fixed cost. `true` uses Scrapling's built-in solver, which handles interactive challenges but waits ~5s for networkidle on every page before detecting. |
| `fallback_solver` | `true` | On any anti-bot failure (challenge detected, any engine), retry the page with the scrapling built-in solver as a last resort. The ~5s/page solver cost is paid only when a challenge is actually detected, turning would-be failures into successes. |

### `browser.search` (provider=forage)

Renders the SERP pages. Deliberately separate from the extract browser, and
never touched by `/extract`:

- **headful by default.** Headless Chromium (playwright, patchright, scrapling)
  is answered with Google's anti-bot page on the first request; a headful
  browser renders normally. When `headless: false`, the app starts Xvfb itself
  (see `app/display.py`); no Docker flags or extra services are needed.
- **opened per search, closed at the end of it.** There is no idle search
  browser holding memory between searches, and all engines of one search share
  the same session, so cookies and history accumulate.
- **rate limited.** `min_interval` serializes renders process-wide with a
  jitter, and a render classified as an anti-bot page is retried in place.

| Key | Default | Description |
|---|---|---|
| `headless` | `false` | Run the search browser headless. `false` (default) needs a display: Xvfb is started automatically. |
| `local` | `true` | Allow the browser shipped in the container (playwright/patchright launched by Forage). |
| `cdp_url` | `""` | CDP endpoint to render through (e.g. `http://172.20.0.1:9222`). Falls back to `browser.cdp_url`. **When set, CDP wins over `local`.** Pages opened by Forage are closed at the end; the remote browser is never closed. |
| `engine` | `playwright` | Local mode only: `playwright` or `patchright`. |
| `humanize` | `true` | Mouse move, a small scroll and a short dwell before navigating to the next SERP. Renders that jump straight to the next URL with no pointer movement are the pattern search engines flag. |
| `min_interval` | `5.0` | Seconds between SERP renders, process-wide, plus up to 25% jitter. Search engines answer a burst with an anti-bot page even when the browser looks fine. `0` disables the pacing. |
| `retries` | `2` | Per-engine retries when a render is classified as an anti-bot page, before moving to the next engine of the chain. |
| `retry_backoff` | `6.0` | Seconds between those retries. |
| `settle_ms` | `1200` | Extra wait after the results start rendering (the render also waits for the first `h3`). |
| `user_agent` | `null` | Override the browser UA (`null` = the built-in desktop Chrome UA). |

## `auth`

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | Require `Authorization: Bearer <key>` on `/search`, `/extract` and `/admin/*`. `/health` stays open (healthcheck). |

Keys come from the `FORAGE_API_KEYS` env var (comma-separated) and are compared in constant time.

## Environment variables

| Variable | Where | Purpose |
|---|---|---|
| `FORAGE_API_KEYS` | service `.env` | Comma-separated Bearer API keys (used when `auth.enabled: true`). |
| `FORAGE_CONFIG` | service `.env` | Config file path inside the container (default `/etc/forage/config.yaml`). |
| `TZ` | service `.env` | Container timezone. |
| `FORAGE_URL` | Hermes `.env` | Base URL the Hermes plugin calls (e.g. `http://localhost:3672`). |
| `FORAGE_API_KEY` | Hermes `.env` | Key the plugin sends when auth is enabled. |
| `FORAGE_BYPASS_CACHE` | Hermes `.env` | `true` makes the plugin always send `Cache-Control: no-cache`. |
