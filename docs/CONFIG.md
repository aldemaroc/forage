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
| `searxng_url` | `http://searxng:8080` | Base URL of the SearXNG instance. On Docker, use the service name on the shared network (see docs/SEARXNG.md). |
| `default_lang` | `pt-BR` | Language passed to SearXNG. |
| `engines` | `[google, bing, brave, startpage]` | Optional engine filter sent to SearXNG. |
| `timeout` | `15` | Timeout in seconds per search request. |

## `extract`

| Key | Default | Description |
|---|---|---|
| `timeout` | `30` | Total seconds budget per URL (applies to static fetch and browser render). |
| `max_content_chars` | `100000` | Cap on extracted content size. |
| `only_main_content` | `true` | Strip navigation/ads/footer (trafilatura main-content extraction). |
| `user_agent` | `ForageBot/0.1 (+https://github.com/aldemaroc/forage)` | User-Agent for the **static** fetch (httpx). |
| `browser_user_agent` | `null` (commented) | User-Agent for the **browser** (Playwright). When unset, a real Chrome desktop UA is used (never a bot UA; it would be a giveaway). |
| `respect_robots` | `false` | Whether to honor `robots.txt`. Default is **false** (do not respect). |
| `force_render` | `false` | Always use the browser for extraction (skip the static attempt). Can also be set per request. |
| `wait_for` | `null` | CSS selector to wait for before extracting (browser mode). |
| `min_content_chars` | `200` | If static extraction yields less text than this, Forage falls back to the browser. |
| `raw_content_markdown` | `true` | `true`: `raw_content` mirrors the clean markdown (Firecrawl-style contract; what Hermes' `web_extract_tool` reads first). `false`: `raw_content` keeps the raw HTML. |
| `force_render_domains` | `[x.com, twitter.com, instagram.com, linkedin.com, tiktok.com, youtube.com, youtu.be]` | Domains that always use the browser (SPAs, strict anti-bot). |
| `url_rewrites` | `[]` | List of prefix rewrite rules applied **before** fetching. Each rule has `match` ("host/path-prefix", www-insensitive) and `replace` ("host[/path]"). Scheme (http/https), the remaining path, query and fragment are preserved. Useful when a site's modern UI hides content behind JS (e.g. Reddit lazy-loads comments) but a classic UI serves everything server-side. The envelope keeps the original `url` and adds `rewritten_url` when a rule fires. |
| `full_text_domains` | `[]` | Domains where extraction uses the **whole page text** instead of `only_main_content`; keeps forum comments, which trafilatura drops as non-main content. |

Example: serve Reddit threads and profiles from the classic UI so comments are extracted without a browser:

```yaml
extract:
  url_rewrites:
    - match: "reddit.com/r/"    # subreddits / threads
      replace: "old.reddit.com/r/"
    - match: "reddit.com/u/"    # user profiles (old format)
      replace: "old.reddit.com/u/"
    - match: "reddit.com/user/" # user profiles (new UI uses /user/)
      replace: "old.reddit.com/u/"
  full_text_domains:
    - reddit.com                # keep comments (trafilatura drops them otherwise)
```

`https://www.reddit.com/r/selfhosted/comments/abc` → fetched as `https://old.reddit.com/r/selfhosted/comments/abc`; the envelope reports `url` as the original and `rewritten_url` as the fetched one.

### Hybrid decision flow

```
domain in force_render_domains | force_render | wait_for  → browser
fetch statically
status 401/403/429                                        → browser
HTML looks like a SPA (#root, __NEXT_DATA__, data-reactroot…) → browser
trafilatura text < min_content_chars                      → browser
otherwise                                                 → static result
```

## `browser`

| Key | Default | Description |
|---|---|---|
| `engine` | `playwright` | Browser engine: `playwright` (default), `patchright` (anti-detection fork of Playwright, same API) or `scrapling` (fingerprint impersonation + Cloudflare Turnstile bypass). Switching engine only needs a config change and `docker compose restart`. |
| `min_idle` | `1` | Browsers kept warm at boot (standby). `0` = lazy (launch on demand). |
| `max_instances` | `5` | Pool ceiling; also the browser concurrency bound for parallel URL extraction. |
| `idle_timeout` | `60` | Seconds an idle instance stays alive before it is closed. |
| `headless` | `true` | Run Chromium headless. |
| `launch_timeout` | `30` | Seconds to launch a new instance. |
| `stealth` | `true` | Hide automation signals (anti-bot). Adds `--disable-blink-features=AutomationControlled`, an init script masking `navigator.webdriver`/`chrome`/`languages`/`plugins`, and a real Chrome UA. |
| `network_idle_timeout` | `5` | Seconds cap for the `networkidle` wait during render. Pages with streaming/websockets (e.g. X) never go idle, so this cap bounds the render time; lower it for faster worst-case extraction, raise it if pages need more time to hydrate via XHR. |
| `scroll_steps` | `0` | Scroll-to-bottom passes in browser mode before extracting. Triggers lazy-loaded content (YouTube/Reddit comments mount only when scrolled into view). **Off by default**: it adds ~6s on browser pages that don't grow; enable per-instance only when lazy comments are needed. |
| `challenge_timeout` | `15` | Max seconds to wait for a Cloudflare/Turnstile challenge to auto-resolve after load. Only used by the `scrapling` engine (polling inside `page_action`). |
| `solve_cloudflare` | `false` | `scrapling` engine only. `false` (default) uses Forage's own title-poll in `page_action`, which resolves non-interactive challenges with no fixed cost. `true` uses Scrapling's built-in solver, which handles interactive challenges but waits ~5s for networkidle on every page before detecting. |
| `fallback_solver` | `true` | On any anti-bot failure (challenge detected, any engine), retry the page with the scrapling built-in solver as a last resort. The ~5s/page solver cost is paid only when a challenge is actually detected, turning would-be failures into successes. |

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
